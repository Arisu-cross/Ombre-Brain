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
#   · 超过 7 天         → 按自然周(周一~周日)合成一条「周记 YYYY-Www」
#   · 周记超过 30 天    → 按月合成一条「月记 YYYY-MM」
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
import asyncio
import logging
from datetime import datetime, timedelta

from openai import AsyncOpenAI

from utils import now_local, now_iso, strip_wikilinks

logger = logging.getLogger("ombre_brain.rollup")


WEEK_PROMPT = """你在帮一个人整理他自己的记忆。

下面是他这一周里每天写下的归档（每天一段，按时间顺序）。
请把它们合成一份「这周大概是这样」的周记。

要求：
1. 用第一人称写，就像他自己回头总结这一周
2. 保留具体的人、事、时间、承诺、未完成的事——这些是他以后要靠它想起来的
3. 保留情绪的走向（这周整体是什么状态，有没有转折）
4. 不要评价、不要升华、不要写鸡汤
5. 重复的日常合并成一句，别逐日复述
6. 300~600 字

直接输出周记正文，不要标题，不要任何解释。"""

MONTH_PROMPT = """你在帮一个人整理他自己的记忆。

下面是他这个月的几份周记，按时间顺序。
请把它们合成一份「这个月大概是这样」的月记。

要求：
1. 用第一人称写
2. 保留贯穿整月的线索：反复出现的人和事、变化的过程、还没了结的事
3. 保留情绪的走向和转折点
4. 具体的日常细节可以舍掉，但**具体的承诺、决定、变化**必须留下
5. 不要评价、不要升华
6. 300~600 字

直接输出月记正文，不要标题，不要任何解释。"""


class RollupEngine:
    """
    归档分层引擎：把旧日档卷成周记，把旧周记卷成月记。

    环境变量：
      OMBRE_ROLLUP_ENABLED    默认 true，设 false/0 关掉整个分层
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
        self.enabled = (env("OMBRE_ROLLUP_ENABLED", "true") or "true").strip().lower() not in (
            "0", "false", "no", "off",
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
        self.max_tokens = int(env("OMBRE_ROLLUP_MAX_TOKENS", "1500") or "1500")

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
    async def run_cycle(self) -> dict:
        """跑一轮：先卷周记，再卷月记。返回统计。"""
        if not self.enabled:
            return {"skipped": "disabled"}
        if not self.configured:
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
            )
            months = await self._roll(
                kind="month",
                source_pred=lambda m: m.get("type") == "archived"
                and not m.get("rolled_up")
                and m.get("rollup_kind") == "week",
                age_days=self.weekly_days,
                now=now,
            )
            self._last_result = {
                "ran_at": now_iso(),
                "weeks_created": weeks["created"],
                "weeks_source_buckets": weeks["sources"],
                "months_created": months["created"],
                "months_source_buckets": months["sources"],
                "errors": weeks["errors"] + months["errors"],
            }
            logger.info(f"Rollup cycle done / 分层完成: {self._last_result}")
            return self._last_result

    async def _roll(self, kind: str, source_pred, age_days: int, now: datetime) -> dict:
        """把够老的源档案按周期分组，每组合成一条。"""
        created = 0
        sources = 0
        errors = 0
        cutoff = now - timedelta(days=age_days)

        try:
            all_buckets = await self.bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            logger.error(f"Rollup 列桶失败: {e}")
            return {"created": 0, "sources": 0, "errors": 1}

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

        for period, items in sorted(groups.items()):
            items.sort(key=self._sort_ts)
            try:
                bucket_id = await self._write_rollup(kind, period, items)
            except Exception as e:
                logger.error(f"写{kind}记失败 / rollup write failed ({period}): {e}")
                errors += 1
                continue
            if not bucket_id:
                errors += 1
                continue
            created += 1
            sources += len(items)

        return {"created": created, "sources": sources, "errors": errors}

    async def _write_rollup(self, kind: str, period: str, items: list) -> str | None:
        """让 LLM 合成一条，落盘，然后把源档案标记为已卷起。"""
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
            return None
        text = (response.choices[0].message.content or "").strip()
        if not text:
            return None

        src_ids = [b["id"] for b in items]
        name = f"{label} {period}"
        span = f"{self._sort_ts(items[0])[:10]} ~ {self._sort_ts(items[-1])[:10]}"
        content = (
            f"# {name}\n"
            f"（{span}，由 {len(items)} 份{'日档' if kind == 'week' else '周记'}合成；"
            f"原档都还在，id：{', '.join(src_ids)}）\n\n"
            f"{text}"
        )

        # 情绪坐标取源档案的均值——这条是那段时间的平均色调
        def _avg(field, default):
            vals = []
            for b in items:
                try:
                    vals.append(float(b["metadata"].get(field, default)))
                except (TypeError, ValueError):
                    continue
            return round(sum(vals) / len(vals), 2) if vals else default

        bucket_id = await self.bucket_mgr.create(
            content=content,
            tags=["归档", label, period],
            importance=5 if kind == "week" else 6,
            domain=["归档"],
            valence=_avg("valence", 0.5),
            arousal=_avg("arousal", 0.3),
            name=name,
            bucket_type="dynamic",
        )
        # 写完立刻补上分层元数据，再挪进归档区（这样它和日档走同一条浮现通道）
        await self.bucket_mgr.set_system_fields(bucket_id, rollup_kind=kind, rollup_period=period)
        await self.bucket_mgr.set_related(bucket_id, src_ids)
        try:
            await self.bucket_mgr.archive(bucket_id)
            # archive 会重写 archived_at；对齐到这段时间的末尾，
            # 否则一条讲上个月的月记会因为"刚归档"排到最新的日档前面。
            await self.bucket_mgr.touch_archived_at(bucket_id, self._sort_ts(items[-1]))
        except Exception as e:
            logger.warning(f"{label}归档移动失败: {e}")

        if self.embedding_engine and getattr(self.embedding_engine, "enabled", False):
            try:
                await self.embedding_engine.generate_and_store(bucket_id, content)
            except Exception:
                pass

        # 源档案标记为已卷起：不再单独浮现，但文件、内容、检索全都保留
        for b in items:
            try:
                await self.bucket_mgr.set_system_fields(b["id"], rolled_up=kind, rolled_into=bucket_id)
            except Exception as e:
                logger.warning(f"标记源档案失败 {b['id']}: {e}")

        logger.info(f"{label}已生成 / rollup created: {name} ← {len(items)} 条")
        return bucket_id

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
