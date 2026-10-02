# recall.py — 「自动浮现」的挑选逻辑(2026-10 按外部实施指南移植,思路借鉴 Latent-memory 的 Passive Recall,代码为指南作者自写、可自由使用)。入口是 server.py 的 GET /api/recall。
# 三条硬规矩:①只读(不 touch);②宁可空手;③只信「稀有词」(按全库正文出现次数算,不按标签数)。
# 几件都合格时,先挑桶名和她这句话最贴的那件,再看分数。

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

# 稀有词的门槛:出现在 ≤ max(RARE_MIN, 全库桶数 × RARE_RATIO) 条记忆里。
# 原型(410 条)上 5% = 21:声音 22 / 第一次 30 / 吃饭 44 / 开心 50 / 我们 119 被挡;海边 5 / 鼻炎 10 / 纪念日 12 / 吵架 14 放行。
RARE_MIN = 3
RARE_RATIO = 0.05
MIN_TAG_LEN = 2

# 永远不算「具体的事」的词。光按出现次数分不开:原型库里「委屈」16、「想你」14,和「代码」14、「纪念日」14 一个量级。
# 情绪词 / 称呼 / 口头语出现在她大半的话里,拿它们去翻,翻出来的只会是「另一次她也委屈了」。
# 系统词是她跟 AI 聊这套系统本身时说的,翻出来的是维护记录,不是共同经历。
STOP_WORDS = {
    # 情绪
    "开心", "高兴", "快乐", "难过", "伤心", "委屈", "难受", "生气", "气死", "烦", "烦死", "焦虑", "害怕", "紧张",
    "心疼", "吃醋", "撒娇", "想你", "想念", "思念", "喜欢", "爱你", "抱抱", "亲亲", "哭", "哭哭", "笑死",
    "孤单", "寂寞", "累", "好累", "无聊", "开心果", "幸福", "安全感", "温暖", "失落", "崩溃", "感动", "担心",
    # 称呼(沈渡系统:她叫栖栖,他叫沈渡)
    "老公", "老婆", "宝宝", "宝贝", "栖栖", "沈渡", "小渡", "你", "我", "我们", "她", "他",
    # 聊天口头语
    "晚安", "早安", "早上好", "在吗", "回来", "到家", "吃饭", "睡觉", "起床", "洗澡", "说话", "聊天", "第一次",
    "今天", "昨天", "明天", "现在", "刚才", "一起", "日常", "记忆", "声音", "消息",
    # 系统 / 工具
    "awaken", "breath", "hold", "trace", "dream", "pulse", "grow", "archive", "ob", "记忆库", "归档", "窗口",
    "压缩", "上下文", "系统", "claude", "模型", "session", "对话归档", "贴纸", "表情包", "语音", "图片",
}

# 繁 → 简:语音转写常把普通话输出成繁体(「海邊」),记忆库是简体,子串一个都对不上。
# 用 opencc-python-reimplemented(Apache-2.0)。别用 zhconv(GPL)。包缺了就原样返回,不报错。
try:
    from opencc import OpenCC as _OpenCC
    _T2S = _OpenCC("t2s")
except Exception:
    _T2S = None


def to_simplified(text: str) -> str:
    if not text or _T2S is None:
        return text or ""
    try:
        return _T2S.convert(text)
    except Exception:
        return text


def clean_query(text: str) -> str:
    """去掉桥自动写的那部分,只留她真说的话。
    - 「(她发来一个贴纸)」「(她发来一张图片…)」这类括号句是桥写的,整段去掉;
    - 语音转写 `[语音] 她说的话(语气:…)`:话留下,语气分析去掉。
    括号冒号半角全角都认。⚠️ 你的桥写的格式不一样就照着改这里。"""
    L, R, C = "[\\(（]", "[\\)）]", "[:：]"
    t = text or ""
    t = re.sub(L + "她[^()（）]*" + R, " ", t)
    t = re.sub(r"\[语音\]\s*", " ", t)
    t = re.sub(L + "语气(?:" + C + "|分析)[^()（）]*" + R, " ", t)
    t = re.sub(L + "还在熟悉她的声音" + R, " ", t)   # 沈渡系统:ears 基线还没学完时 shim 附的一句
    return to_simplified(re.sub(r"\s+", " ", t).strip())


class WordDF:
    """一个词在全库多少条记忆里出现过(名字 + 正文 + 标签,不分大小写)。按需算、缓存。"""
    def __init__(self, buckets: list[dict]):
        self.docs = []
        for b in buckets:
            meta = b.get("metadata", {})
            tags = " ".join(str(t) for t in (meta.get("tags") or []))
            self.docs.append(f"{meta.get('name', '')} {b.get('content', '')} {tags}".lower())
        self._cache: dict = {}

    def get(self, word: str, default: int = 0) -> int:
        w = (word or "").lower()
        if w not in self._cache:
            self._cache[w] = sum(1 for d in self.docs if w in d)
        return self._cache[w]


def rare_limit(n_buckets: int) -> int:
    return max(RARE_MIN, math.ceil(n_buckets * RARE_RATIO))


def rare_tags_in(query: str, tags: list, df, limit: int) -> list[str]:
    """这个桶的标签里,哪些原样出现在她这句话里、且在全库不常见。"""
    q = (query or "").lower()
    hits = []
    for t in tags or []:
        t = str(t).strip().lower()
        if len(t) >= MIN_TAG_LEN and t not in STOP_WORDS and t in q and df.get(t, 0) <= limit:
            hits.append(t)
    return hits


def _parse_dt(raw) -> datetime | None:
    try:
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def excerpt(content: str, max_chars: int) -> str:
    """取开头 max_chars 字;能在句号处断就在句号处断。"""
    text = re.sub(r"\s+", " ", content or "").strip()
    if len(text) <= max_chars:
        return text
    cut = text[:max_chars]
    stop = max(cut.rfind(p) for p in "。!?!?;;")
    if stop >= max_chars * 0.5:
        return cut[: stop + 1]
    return cut + "…"


def excluded_reason(meta: dict, now: datetime, min_age_hours: float, is_expired=None) -> str:
    """不该自动递的桶返回原因,该递返回空串。"""
    if meta.get("pinned") or meta.get("protected"):
        return "pinned"       # 钉选的 awaken 每次都带全文,再递是重复
    if meta.get("type") == "feel":
        return "feel"         # AI 自己的感受,不当「旧事」递
    if meta.get("dormant"):
        return "dormant"
    if int(meta.get("denied", 0) or 0) > 0:
        return "denied"       # 她否认过的(你的 OB 没有否认功能就删掉这两行)
    if is_expired and is_expired(meta):
        return "expired"
    created = _parse_dt(meta.get("created"))
    if created and now - created < timedelta(hours=min_age_hours):
        return "too_new"      # 刚存的多半还在这个窗口里,递了是复读
    return ""


def name_overlap(query: str, name: str, rare: list[str]) -> int:
    """桶名和她这句话共有的最长一段字有多长 —— 只算包含某个命中稀有词的那段。
    几件都合格时靠它挑最贴的那件(见第 5 节坑 12)。只认含稀有词的段,免得称呼这种到处都是的字给桶名白加分。"""
    q, n = (query or "").lower(), (name or "").lower()
    best = 0
    for r in rare or ():
        start = n.find(r)
        while start != -1:
            for i in range(start, -1, -1):
                for j in range(len(n), start + len(r) - 1, -1):
                    if j - i <= best:
                        break
                    if n[i:j] in q:
                        best = j - i
                        break
            start = n.find(r, start + 1)
    return best


def pick(query, matches, all_buckets, now, min_age_hours=24, max_chars=240, exclude_ids=(), is_expired=None) -> dict:
    """从 search() 的结果里挑最多一条。返回 {"pick": {...} | None, "reason": str, "candidates": [...]}。
    candidates 是前三条及各自为什么没选上,给 shim 的观察记录用,不含正文。"""
    df = WordDF(all_buckets)
    limit = rare_limit(len(all_buckets))
    exclude = set(exclude_ids or ())
    candidates, eligible = [], []
    for b in matches:
        meta = b.get("metadata", {})
        why = excluded_reason(meta, now, min_age_hours, is_expired)
        rare = rare_tags_in(query, meta.get("tags", []), df, limit)
        if not why and b["id"] in exclude:
            why = "cooldown"
        if not why and not rare:
            why = "no_rare_word"
        overlap = name_overlap(query, meta.get("name", ""), rare) if not why else 0
        if len(candidates) < 3:
            candidates.append({"id": b["id"], "name": meta.get("name", b["id"]),
                               "score": b.get("score", 0), "rare": rare, "overlap": overlap, "skip": why})
        if not why:
            eligible.append((overlap, len(eligible), b, rare))
    if not eligible:
        return {"pick": None, "reason": "empty" if not matches else "no_trusted_hit", "candidates": candidates}
    # 合格的里面:先比桶名跟她这句话重合多少,一样再按 search() 原来的顺序
    _, _, b, rare = max(eligible, key=lambda e: (e[0], -e[1]))
    meta = b.get("metadata", {})
    return {
        "pick": {"id": b["id"], "name": meta.get("name", b["id"]), "created": str(meta.get("created", ""))[:10],
                 "score": b.get("score", 0), "rare": rare, "excerpt": excerpt(b.get("content", ""), max_chars)},
        "reason": "ok",
        "candidates": candidates,
    }
