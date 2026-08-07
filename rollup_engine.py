# ============================================================
# Module: Archive Rollup Engine (rollup_engine.py)
# 模块：归档分层引擎（周记 / 月记）
#
# 问题：archive_session 每天写一个日档，唤醒时只浮现最近几条。
# 于是「上上周发生的事」既不会自己浮上来，也没有一个粗一点的替身——
# 时间一长，连续性就断在最近这几天。
#
# 做法（照人的记忆来）：越久越粗，但不断线。
#   · 最近 7 天         → 日档原样浮现（archive_session 写的那些）
#   · 超过 7 天         → 这一周**按事件拆成几条**「周记 YYYY-Www · 某件事」
#   · 周记超过 30 天    → 这个月**按线索拆成几条**「月记 YYYY-MM · 某条线」
#
# 为什么是几条不是一条：一坨流水账没法被单独想起来。拆成事件之后，
# 「上个月复查那件事」和「上个月工作那摊」各是一条记忆，检索才找得准。
# 这些桶都是**直接新建**的，不走 hold 的合并判定——不受合并阈值影响，
# 也绝不会去动已有的普通记忆桶（只认 type: archived 的归档当原料）。
#
# 原始档案**永远保留**（手册红线：绝不丢记忆），只是被标记 rolled_up，
# 不再单独浮现；搜索照样搜得到，周记里也记着它们的 id。
#
# LLM 调用默认复用脱水那套配置，也可以用 OMBRE_ROLLUP_* 单独指向另一家
# （比如让周记/月记走 DeepSeek，而脱水仍走原来的）。
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import os
import json
import asyncio
import logging
from datetime import datetime, timedelta

from openai import AsyncOpenAI

from utils import now_local, now_iso, strip_wikilinks

logger = logging.getLogger("ombre_brain.rollup")


# 输出格式说明（周记/月记共用）：按事件拆条，而不是把一周揉成一坨。
# 一个周期产出 1~N 条，每条是一件事——这样它们才像正常记忆一样能被单独想起来。
_ITEM_FORMAT = """输出格式（纯 JSON 数组，不要任何其他内容）：
[
  {
    "name": "这件事叫什么（12字以内）",
    "content": "这件事的完整记述",
    "domain": ["主题域"],
    "valence": 0.5,
    "arousal": 0.3,
    "tags": ["核心词", "关联词"],
    "importance": 5
  }
]

主题域可选（选最精确的 1~2 个）：
  日常: ["饮食", "穿搭", "出行", "居家", "购物"]
  人际: ["家庭", "恋爱", "友谊", "社交"]
  成长: ["工作", "学习", "考试", "求职"]
  身心: ["健康", "心理", "睡眠", "运动"]
  兴趣: ["游戏", "影视", "音乐", "阅读", "创作", "手工"]
  数字: ["编程", "AI", "硬件", "网络"]
  事务: ["财务", "计划", "待办"]
  内心: ["情绪", "回忆", "梦境", "自省"]
valence 0~1（0=消极 0.5=中性 1=积极）；arousal 0~1（0=平静 1=激动）；importance 1~10。
对人名、地名、专有名词用 [[双链]] 标记，普通词汇不要加。"""


WEEK_PROMPT = """你在帮一个人整理他自己的记忆。

下面是他这一周里每天写下的归档（每天一段，按时间顺序）。
请把这一周**按事件拆成几条独立的记忆**——不要合成一大坨流水账。

拆分规则：
1. 一条 = 一件事/一条线索（同一件事跨了好几天，合成一条，把过程写清楚）
2. 用第一人称写，就像他自己回头记下这件事
3. 保留具体的人、时间、承诺、还没了结的事——他以后要靠这些想起来
4. 保留情绪：这件事对他来说是什么感觉，有没有转折
5. 不要评价、不要升华、不要写鸡汤
6. 零碎的日常并进最相关的那条，或者合成一条「这周的日常」
7. **这一周有几件事就写几条**：通常 2~5 条；确实只有一件事就只写一条，
   不要为了凑数硬拆，也不要把明显不同的事塞进同一条
8. 每条 100~400 字

""" + _ITEM_FORMAT

MONTH_PROMPT = """你在帮一个人整理他自己的记忆。

下面是他这个月的几条周记，按时间顺序。
请把这个月**按线索拆成几条独立的记忆**——不要合成一大坨。

拆分规则：
1. 一条 = 一条贯穿性的线索（反复出现的人和事、一个变化的过程、一件悬着的事）
2. 用第一人称写
3. 具体的日常细节可以舍掉，但**具体的承诺、决定、变化、没了结的事**必须留下
4. 保留情绪的走向和转折点
5. 不要评价、不要升华
6. **通常 2~4 条**；这个月确实只有一条主线就只写一条，不要硬凑
7. 每条 150~500 字

""" + _ITEM_FORMAT


class RollupEngine:
    """
    归档分层引擎：把旧日档卷成周记，把旧周记卷成月记。

    环境变量：
      OMBRE_ROLLUP_ENABLED    默认 **false**（opt-in）；设 true/1 才真的开始分层
      OMBRE_ROLLUP_DAILY_DAYS 日档保留几天不卷（默认 7）
      OMBRE_ROLLUP_WEEKLY_DAYS 周记满多少天卷成月记（默认 30）
      OMBRE_ROLLUP_MODEL / _BASE_URL / _API_KEY
                              单独指定写周记/月记的模型；不设则沿用脱水配置
      OMBRE_ROLLUP_INTERVAL_H 巡查间隔小时（默认 24）
    """

    def __init__(self, config: dict, bucket_mgr, dehydrator=None, embedding_engine=None):
        self.bucket_mgr = bucket_mgr
        self.dehydrator = dehydrator
        self.embedding_engine = embedding_engine

        env = os.environ.get
        # 默认**关**（改成 opt-in）。原来默认开有个坑：ROLLUP_API_KEY 不设时会
        # 回落到脱水那把 key，而线上那把是设了的 —— 于是一部署上去、第一次
        # /health 命中，就会把积压的全部历史归档一口气卷完，没人来得及看一眼。
        # 分层是半单向的（已生成的周记和已打的 rolled_up 标记不会自己撤销），
        # 这种事必须是她明确点头才发生。
        self.enabled = (env("OMBRE_ROLLUP_ENABLED", "false") or "false").strip().lower() in (
            "1", "true", "yes", "on",
        )
        self.daily_days = max(1, int(env("OMBRE_ROLLUP_DAILY_DAYS", "7") or "7"))
        self.weekly_days = max(1, int(env("OMBRE_ROLLUP_WEEKLY_DAYS", "30") or "30"))
        self.interval_h = max(1, int(env("OMBRE_ROLLUP_INTERVAL_H", "24") or "24"))

        # --- 写周记/月记用的模型：默认沿用脱水那套，可单独覆盖 ---
        dehy = config.get("dehydration", {})
        self.model = (env("OMBRE_ROLLUP_MODEL", "") or dehy.get("model", "deepseek-chat")).strip()
        self.base_url = (
            env("OMBRE_ROLLUP_BASE_URL", "") or dehy.get("base_url", "https://api.deepseek.com/v1")
        ).strip()
        self.api_key = (env("OMBRE_ROLLUP_API_KEY", "") or dehy.get("api_key", "")).strip()
        self.max_tokens = int(env("OMBRE_ROLLUP_MAX_TOKENS", "2048") or "2048")
        # 一个周期最多拆成几条。拆得太碎会把唤醒时"最近归档"的名额吃光。
        self.max_items = max(1, int(env("OMBRE_ROLLUP_MAX_ITEMS", "5") or "5"))

        self.client = (
            AsyncOpenAI(api_key=self.api_key, base_url=self.base_url, timeout=120.0)
            if self.api_key else None
        )

        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()
        self._last_result: dict | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def configured(self) -> bool:
        """有 key 才能真的写周记/月记。"""
        return bool(self.client)

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    # ---------------------------------------------------------
    # 时间分组
    # ---------------------------------------------------------
    @staticmethod
    def _period_of(ts: str, kind: str) -> str | None:
        """把归档时刻映射到它所属的周/月标签。

        周用 ISO 周（周一起算）：2026-W32；月：2026-08。
        """
        try:
            dt = datetime.fromisoformat(str(ts))
        except (ValueError, TypeError):
            return None
        if kind == "week":
            y, w, _ = dt.isocalendar()
            return f"{y}-W{w:02d}"
        return f"{dt.year}-{dt.month:02d}"

    @staticmethod
    def _period_is_over(period: str, kind: str, now: datetime) -> bool:
        """这个周期是不是已经整个过去了。

        只卷"已经结束"的周期——否则本周还没过完就写周记，后面几天没处放。
        """
        try:
            if kind == "week":
                y, w = period.split("-W")
                # ISO 周的周一 + 7 天 = 下周一；now 过了下周一才算这周结束
                monday = datetime.fromisocalendar(int(y), int(w), 1)
                end = monday + timedelta(days=7)
            else:
                y, m = period.split("-")
                y, m = int(y), int(m)
                end = datetime(y + (m // 12), (m % 12) + 1, 1)
            if now.tzinfo and end.tzinfo is None:
                end = end.replace(tzinfo=now.tzinfo)
            return now >= end
        except (ValueError, TypeError):
            return False

    def _sort_ts(self, b: dict) -> str:
        m = b["metadata"]
        return str(m.get("archived_at") or m.get("created") or m.get("last_active") or "")

    # ---------------------------------------------------------
    # 一轮巡查
    # ---------------------------------------------------------
    async def run_cycle(self, dry_run: bool = False) -> dict:
        """跑一轮：先卷周记，再卷月记。返回统计。

        dry_run=True：只报"会整理哪几个周期、各吃几份原料"，**不调 LLM、不写任何文件**。
        第一次开启分层时，积压的历史会在一轮里全部补齐——先空跑一次看看规模，
        再决定要不要真跑。
        """
        if not self.enabled:
            return {"skipped": "disabled"}
        if not self.configured and not dry_run:
            logger.warning("Rollup 没配 API key，跳过（设 OMBRE_ROLLUP_API_KEY 或沿用脱水 key）")
            return {"skipped": "no_api_key"}

        async with self._lock:
            now = now_local()
            weeks = await self._roll(
                kind="week",
                source_pred=lambda m: m.get("type") == "archived"
                and not m.get("rolled_up")
                and m.get("rollup_kind") is None,
                age_days=self.daily_days,
                now=now,
                dry_run=dry_run,
            )
            # 空跑时周记还没真生成，没法接着算月记：这里只报周记那一层，
            # 免得给出一个"月记 0 条"的假答案。
            months = (
                {"created": 0, "periods": 0, "sources": 0, "errors": 0, "plan": []}
                if dry_run else
                await self._roll(
                    kind="month",
                    source_pred=lambda m: m.get("type") == "archived"
                    and not m.get("rolled_up")
                    and m.get("rollup_kind") == "week",
                    age_days=self.weekly_days,
                    now=now,
                )
            )
            result = {
                "ran_at": now_iso(),
                "dry_run": dry_run,
                "weeks_created": weeks["created"],          # 新建的周记桶数
                "weeks_periods": weeks["periods"],           # 整理了几周
                "weeks_source_buckets": weeks["sources"],    # 吃进了几份日档
                "months_created": months["created"],
                "months_periods": months["periods"],
                "months_source_buckets": months["sources"],
                "errors": weeks["errors"] + months["errors"],
            }
            if dry_run:
                # 空跑的结果只回给调用方看，不覆盖"上次真跑"的记录
                result["plan"] = weeks["plan"]
                result["note"] = "空跑：没调 LLM、没写任何文件；月记要等周记真生成后才算得出来"
                logger.info(f"Rollup dry-run / 分层空跑: {result}")
                return result
            self._last_result = result
            logger.info(f"Rollup cycle done / 分层完成: {result}")
            return result

    async def _roll(self, kind: str, source_pred, age_days: int, now: datetime,
                    dry_run: bool = False) -> dict:
        """把够老的源档案按周期分组，每组合成一条。"""
        created = 0      # 新建的桶数(一个周期可能好几条)
        periods = 0      # 处理了几个周期
        sources = 0
        errors = 0
        cutoff = now - timedelta(days=age_days)

        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            logger.error(f"Rollup 列桶失败: {e}")
            return {"created": 0, "periods": 0, "sources": 0, "errors": 1, "plan": []}

        # 已经存在的周记/月记标签，避免重复生成
        existing = {
            b["metadata"].get("rollup_period")
            for b in all_buckets
            if b["metadata"].get("rollup_kind") == kind
        }

        groups: dict[str, list] = {}
        for b in all_buckets:
            meta = b["metadata"]
            if not source_pred(meta):
                continue
            ts = self._sort_ts(b)
            try:
                if datetime.fromisoformat(ts) >= cutoff:
                    continue  # 还太新，先留着原样浮现
            except (ValueError, TypeError):
                continue
            period = self._period_of(ts, kind)
            if not period or period in existing:
                continue
            if not self._period_is_over(period, kind, now):
                continue
            groups.setdefault(period, []).append(b)

        plan = []
        for period, items in sorted(groups.items()):
            items.sort(key=self._sort_ts)
            if dry_run:
                plan.append({
                    "period": period,
                    "sources": len(items),
                    "span": f"{self._sort_ts(items[0])[:10]} ~ {self._sort_ts(items[-1])[:10]}",
                })
                periods += 1
                sources += len(items)
                continue
            try:
                new_ids = await self._write_rollup(kind, period, items)
            except Exception as e:
                logger.error(f"写{kind}记失败 / rollup write failed ({period}): {e}")
                errors += 1
                continue
            if not new_ids:
                errors += 1
                continue
            periods += 1
            created += len(new_ids)      # 一个周期可能产出好几条(按事件拆)
            sources += len(items)

        return {"created": created, "periods": periods, "sources": sources,
                "errors": errors, "plan": plan}

    def _parse_items(self, raw: str) -> list[dict]:
        """解析 LLM 返回的事件数组。

        解析失败**不当作没产出**——退回单条，用整段文本兜底。
        宁可多一条粗糙的周记，也不能让这一周静悄悄地消失。
        """
        def _normalize(item: dict) -> dict | None:
            """把一条(可能缺字段、类型也不一定对的)结果补成完整的事件条目。

            兜底路径也走这里——否则少个 name 就会在落盘时 KeyError，
            等于"解析失败"变成"这一周直接丢了"。
            """
            if not isinstance(item, dict) or not str(item.get("content", "")).strip():
                return None
            try:
                importance = max(1, min(10, int(item.get("importance", 5))))
            except (TypeError, ValueError):
                importance = 5
            try:
                valence = max(0.0, min(1.0, float(item.get("valence", 0.5))))
                arousal = max(0.0, min(1.0, float(item.get("arousal", 0.3))))
            except (TypeError, ValueError):
                valence, arousal = 0.5, 0.3
            domain = item.get("domain") or []
            if not isinstance(domain, list):
                domain = [str(domain)]
            tags = item.get("tags") or []
            if not isinstance(tags, list):
                tags = [str(tags)]
            return {
                "name": str(item.get("name", "")).strip()[:20],
                "content": str(item["content"]).strip(),
                "domain": [str(d) for d in domain[:2]],
                "tags": [str(t) for t in tags[:15]],
                "valence": valence,
                "arousal": arousal,
                "importance": importance,
            }

        cleaned = (raw or "").strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0]
        try:
            parsed = json.loads(cleaned)
        except (json.JSONDecodeError, ValueError):
            logger.warning(f"Rollup JSON 解析失败，退回单条兜底: {cleaned[:160]}")
            parsed = [{"content": cleaned}] if cleaned else []

        if isinstance(parsed, dict):
            parsed = [parsed]
        if not isinstance(parsed, list):
            parsed = [{"content": cleaned}] if cleaned else []

        return [x for x in (_normalize(i) for i in parsed) if x]

    async def _write_rollup(self, kind: str, period: str, items: list) -> list[str]:
        """让 LLM 把这一周期**按事件拆成几条**，各自落成一个桶，
        然后把源档案标记为已卷起。返回新建的桶 id 列表。

        为什么是几条而不是一条：一条大杂烩没法被单独想起来。拆成事件之后，
        「上个月复查那件事」和「上个月工作那摊」各是一条记忆，检索才找得准。
        这些桶都是**直接新建**的，不走 hold 的合并判定——不受合并阈值影响。
        """
        label = "周记" if kind == "week" else "月记"
        prompt = WEEK_PROMPT if kind == "week" else MONTH_PROMPT

        chunks = []
        for b in items:
            head = b["metadata"].get("name", b["id"])
            body = strip_wikilinks(b.get("content", "")).strip()
            chunks.append(f"【{head}】\n{body}")
        source_text = "\n\n".join(chunks)[:24000]

        response = await self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": source_text},
            ],
            max_tokens=self.max_tokens,
            temperature=0.3,
        )
        if not response.choices:
            return []
        events = self._parse_items(response.choices[0].message.content or "")
        if not events:
            return []
        events = events[: self.max_items]

        src_ids = [b["id"] for b in items]
        span = f"{self._sort_ts(items[0])[:10]} ~ {self._sort_ts(items[-1])[:10]}"
        source_kind = "日档" if kind == "week" else "周记"
        period_end = self._sort_ts(items[-1])

        new_ids = []
        for ev in events:
            title = ev["name"] or period
            content = (
                f"# {label} {period} · {title}\n"
                f"（{span}，从 {len(items)} 份{source_kind}整理而来；"
                f"原档都还在，id：{', '.join(src_ids)}）\n\n"
                f"{ev['content']}"
            )
            tags = list(dict.fromkeys([label, period, *ev["tags"]]))
            bucket_id = await self.bucket_mgr.create(
                content=content,
                tags=tags,
                importance=ev["importance"],
                domain=ev["domain"] or ["归档"],
                valence=ev["valence"],
                arousal=ev["arousal"],
                # 用 "-" 不用 "·"：sanitize_name 会把 · 洗掉，留下两个空格的怪名字
                name=f"{label} {period} - {title}",
                bucket_type="dynamic",
            )
            # 补上分层记账，再挪进归档区（和日档走同一条浮现通道）
            await self.bucket_mgr.set_system_fields(
                bucket_id, rollup_kind=kind, rollup_period=period
            )
            await self.bucket_mgr.set_related(bucket_id, src_ids)
            try:
                await self.bucket_mgr.archive(bucket_id)
                # archive 会重写 archived_at；对齐到这段时间的末尾，
                # 否则一条讲上个月的月记会因为"刚归档"排到最新的日档前面。
                await self.bucket_mgr.touch_archived_at(bucket_id, period_end)
            except Exception as e:
                logger.warning(f"{label}归档移动失败: {e}")

            if self.embedding_engine and getattr(self.embedding_engine, "enabled", False):
                try:
                    await self.embedding_engine.generate_and_store(bucket_id, content)
                except Exception:
                    pass
            new_ids.append(bucket_id)

        # 源档案标记为已卷起：不再单独浮现，但文件、内容、检索全都保留
        for b in items:
            try:
                await self.bucket_mgr.set_system_fields(
                    b["id"], rolled_up=kind, rolled_into=new_ids
                )
            except Exception as e:
                logger.warning(f"标记源档案失败 {b['id']}: {e}")

        logger.info(
            f"{label}已生成 / rollup created: {label} {period} → {len(new_ids)} 条 "
            f"← {len(items)} 份{source_kind}"
        )
        return new_ids

    # ---------------------------------------------------------
    # 后台循环
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        if not self._running:
            await self.start()

    async def start(self) -> None:
        if self._running or not self.enabled:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(f"Rollup engine started, interval {self.interval_h}h / 分层引擎已启动")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _background_loop(self) -> None:
        while self._running:
            try:
                await self.run_cycle()
            except Exception as e:
                logger.error(f"Rollup cycle error / 分层出错: {e}")
            try:
                await asyncio.sleep(self.interval_h * 3600)
            except asyncio.CancelledError:
                break
