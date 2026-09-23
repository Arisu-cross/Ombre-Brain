# ============================================================
# continuity.py — 跨窗口/跨压缩的「接续」:原话桶 + 给下一个窗口的信
#
# 为什么不用普通记忆桶:这两样都不是「记忆」——
#   · 原话:压缩前最后那段两个人的原话,只为压缩后能直接接上刚才那句。
#     它会过期、只需要最新一份,进了搜索/做梦/衰减反而是噪音。
#   · 信:他留下的一封信,写什么、写给谁都行(下一个窗口的自己、她、自己心里的话)。
#     日记写「发生了什么」,信是他想留下的话。只追加,不参与衰减。
# 所以都存成 buckets_dir 下的隐藏文件,和记忆桶完全分开;唤醒时单独成段。
#
# 原话由 shim 写(POST /api/raw-tail),不经过模型:不花 token、顺序不会乱、
# 也不用在人设里给「归档不写逐句复述」开豁免。
# ============================================================

import json
import os
from datetime import datetime, timedelta

RAW_TAIL_FILE = ".raw_tail.json"
LETTERS_FILE = ".letters.jsonl"


def _parse(ts: str):
    try:
        return datetime.fromisoformat(str(ts)[:19])
    except (TypeError, ValueError):
        return None


def clip_tail(text: str, max_chars: int) -> str:
    """超长时只留最后 max_chars 字(离压缩最近的最该留),尽量从整行处切开。"""
    text = (text or "").strip()
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    cut = text[-max_chars:]
    nl = cut.find("\n")
    if 0 <= nl < max_chars // 3:   # 行首就在附近 → 从整行开始,别把一句话切半
        cut = cut[nl + 1:]
    return "…(更早的省略)\n" + cut


def _write_atomic(path: str, data: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(data)
    os.replace(tmp, path)


def save_raw_tail(base_dir: str, text: str, at: str, max_chars: int = 4000) -> str:
    """覆盖保存最新一份原话。返回实际存下的文本。"""
    body = clip_tail(text, max_chars)
    _write_atomic(
        os.path.join(base_dir, RAW_TAIL_FILE),
        json.dumps({"at": at, "text": body}, ensure_ascii=False),
    )
    return body


def load_raw_tail(base_dir: str, now: datetime, ttl_hours: float = 12):
    """读原话;不存在、坏了、过期(写入超过 ttl_hours)一律返回 None。"""
    try:
        with open(os.path.join(base_dir, RAW_TAIL_FILE), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    at = _parse(d.get("at"))
    text = (d.get("text") or "").strip()
    if not at or not text:
        return None
    if ttl_hours > 0 and now - at > timedelta(hours=ttl_hours):
        return None
    return {"at": d["at"], "text": text}


def append_letter(base_dir: str, text: str, at: str) -> None:
    text = (text or "").strip()
    if not text:
        return
    with open(os.path.join(base_dir, LETTERS_FILE), "a", encoding="utf-8") as f:
        f.write(json.dumps({"at": at, "text": text}, ensure_ascii=False) + "\n")


def recent_letters(base_dir: str, now: datetime, ttl_days: float = 3, n: int = 1) -> list:
    """最近 n 封没过期的信,新的在前。坏行跳过,不连累别的。"""
    if n <= 0:
        return []
    out = []
    try:
        with open(os.path.join(base_dir, LETTERS_FILE), encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                at = _parse(d.get("at"))
                if not at or not (d.get("text") or "").strip():
                    continue
                if ttl_days > 0 and now - at > timedelta(days=ttl_days):
                    continue
                out.append({"at": d["at"], "text": d["text"].strip()})
    except OSError:
        return []
    out.sort(key=lambda x: x["at"], reverse=True)
    return out[:n]


def render_wake_extras(raw_tail, letters, todo_lines) -> list:
    """唤醒最前面的三段。空的段落不输出。"""
    parts = []
    if raw_tail:
        parts.append(
            f"=== 压缩前最后的原话({raw_tail['at'][:16].replace('T', ' ')})===\n"
            "(上下文刚被压缩过。这是压缩前你们最后说的话,由系统原样留存 —— 从这里直接接上,"
            "不用复述、不用解释你读过它。)\n" + raw_tail["text"]
        )
    if letters:
        body = "\n---\n".join(f"[{l['at'][:16].replace('T', ' ')}] {l['text']}" for l in letters)
        parts.append("=== 上一个窗口留下的信 ===\n" + body)
    if todo_lines:
        parts.append("=== 没做完的事 ===\n" + "\n".join(f"☐ {t}" for t in todo_lines)
                     + "\n(细节用 todos() 看;做完了去对应的桶里勾掉)")
    return parts
