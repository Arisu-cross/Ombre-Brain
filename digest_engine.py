# ============================================================
# Module: Auto-digest / Sediment (digest_engine.py)
# 模块：自动消化 —— 把长期没被想起的低重要度碎片沉淀成摘要
#
# 做什么：
#   定期扫描「重要度低 + 很久没被访问 + 非钉选」的桶，按语义分组，
#   每组提炼成一条「沉淀摘要」桶，原桶归档（不删）。
#
# 为什么要先有演习模式：
#   备份实测 250 个桶里 importance<=4 的有 88 个（35%）。扫描口径稍微一宽，
#   一轮就能吞掉三分之一个记忆库。所以默认只出计划、不动手，
#   execute=True 是显式的一次决定，且每轮有组数上限。
#
# 不做什么（这几条是刻意的）：
#   - 不删除任何东西：原桶只归档，随时能捞回来
#   - 钉选/保护/permanent/feel/便利贴/带未处理触发日期的桶一律不碰
#   - 提炼失败就整组放弃，绝不拿拼接原文冒充摘要落库
#     （那只是把碎片换个地方堆着，还多一个桶）
#
# 依赖：bucket_manager（读写桶）、embedding_engine（语义分组）、dehydrator（提炼）
# ============================================================

import os
import asyncio
import logging
from datetime import datetime

import frontmatter

from utils import now_local, now_iso

logger = logging.getLogger("ombre_brain.digest")


class DigestEngine:
    def __init__(self, config: dict, bucket_mgr, embedding_engine=None, dehydrator=None):
        cfg = (config or {}).get("digest", {}) or {}
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.embedding_engine = embedding_engine
        self.dehydrator = dehydrator

        # --- 候选口径（宁可窄，别一轮吞掉半个库）---
        self.importance_max = int(os.environ.get("DIGEST_IMPORTANCE_MAX",
                                                 cfg.get("importance_max", 4)))
        self.min_idle_days = int(os.environ.get("DIGEST_MIN_IDLE_DAYS",
                                                cfg.get("min_idle_days", 90)))
        self.sim_threshold = float(os.environ.get("DIGEST_SIM_THRESHOLD",
                                                  cfg.get("sim_threshold", 0.62)))
        self.min_group = int(os.environ.get("DIGEST_MIN_GROUP", cfg.get("min_group", 3)))
        self.max_group = int(os.environ.get("DIGEST_MAX_GROUP", cfg.get("max_group", 8)))
        # 一轮最多整理几组 —— 出事也只出这么大
        self.max_groups_per_run = int(os.environ.get("DIGEST_MAX_GROUPS",
                                                     cfg.get("max_groups_per_run", 3)))
        # 定期扫描间隔（小时）；0 = 不自动扫描
        self.scan_interval = float(os.environ.get("DIGEST_SCAN_HOURS",
                                                  cfg.get("scan_interval_hours", 168)))
        # 自动执行开关：默认**只演习**。真要放手，显式设 1。
        self.auto_execute = (os.environ.get("DIGEST_AUTO_EXECUTE",
                                            str(cfg.get("auto_execute", 0))) == "1")

        self._running = False
        self._task = None

    # ---------------------------------------------------------
    # 谁可以被消化
    # ---------------------------------------------------------
    def _is_candidate(self, meta: dict) -> bool:
        if meta.get("pinned") or meta.get("protected"):
            return False
        if meta.get("type") in ("permanent", "feel", "archived"):
            return False
        if meta.get("expires_at"):
            return False           # 便利贴自己会到点撕掉，不用消化
        if meta.get("sediment"):
            return False           # 沉淀桶不再被二次沉淀
        # 还没处理的触发日期 = 一个未兑现的承诺，不能把它揉进摘要里
        if meta.get("trigger_date") and not meta.get("trigger_done"):
            return False
        try:
            if int(meta.get("importance", 5)) > self.importance_max:
                return False
        except (TypeError, ValueError):
            return False
        return self._idle_days(meta) >= self.min_idle_days

    @staticmethod
    def _idle_days(meta: dict) -> float:
        ts = str(meta.get("last_active", meta.get("created", "")) or "")
        try:
            return (now_local() - datetime.fromisoformat(ts)).total_seconds() / 86400
        except (ValueError, TypeError):
            return 0.0             # 时间戳坏了当「刚活跃过」，宁可不消化

    async def candidates(self) -> list[dict]:
        buckets = await self.bucket_mgr.list_all(include_archive=False)
        return [b for b in buckets if self._is_candidate(b.get("metadata", {}))]

    # ---------------------------------------------------------
    # 分组：优先 embedding 语义聚类，没有向量就退化到标签/主题域重合度
    # ---------------------------------------------------------
    async def _group(self, buckets: list[dict]) -> tuple[list[list[dict]], str]:
        vectors = {}
        if self.embedding_engine is not None and getattr(self.embedding_engine, "enabled", False):
            for b in buckets:
                try:
                    emb = await self.embedding_engine.get_embedding(b["id"])
                except Exception:
                    emb = None
                if emb:
                    vectors[b["id"]] = emb

        if len(vectors) >= self.min_group:
            grouped = self._greedy_cluster(
                [b for b in buckets if b["id"] in vectors],
                lambda x, y: self.embedding_engine._cosine_similarity(
                    vectors[x["id"]], vectors[y["id"]]
                ),
            )
            return grouped, "语义(embedding)"

        grouped = self._greedy_cluster(buckets, self._tag_similarity)
        return grouped, "标签/主题域重合(向量不可用时的退化通道)"

    @staticmethod
    def _tag_similarity(a: dict, b: dict) -> float:
        """标签+主题域的 Jaccard 相似度。效果弱于向量，但总比按目录瞎归堆强。"""
        def keys(x):
            m = x.get("metadata", {})
            return {str(t).lower() for t in (m.get("tags") or [])} | {
                f"域:{str(d).lower()}" for d in (m.get("domain") or [])
            }
        ka, kb = keys(a), keys(b)
        if not ka or not kb:
            return 0.0
        return len(ka & kb) / len(ka | kb)

    def _greedy_cluster(self, buckets: list[dict], sim_fn) -> list[list[dict]]:
        """贪心聚类：拿一个种子，把够像的都收进来，收满 max_group 为止。

        不用 kmeans 之类：这里要的不是「把全库切干净」，而是「找出几堆明显
        属于同一件事的碎片」。剩下配不上对的就该留在原地，不该被硬塞进某一组。
        """
        remaining = list(buckets)
        groups = []
        while remaining:
            seed = remaining.pop(0)
            group = [seed]
            rest = []
            for other in remaining:
                if len(group) < self.max_group and sim_fn(seed, other) >= self.sim_threshold:
                    group.append(other)
                else:
                    rest.append(other)
            remaining = rest
            if len(group) >= self.min_group:
                groups.append(group)
        return groups

    # ---------------------------------------------------------
    # 演习：只出计划，不动任何数据
    # ---------------------------------------------------------
    async def plan(self) -> dict:
        cands = await self.candidates()
        if len(cands) < self.min_group:
            return {
                "candidates": len(cands), "groups": [], "method": "-",
                "note": f"够条件的桶只有 {len(cands)} 个，不到一组的下限（{self.min_group}）",
            }
        groups, method = await self._group(cands)
        groups = groups[: self.max_groups_per_run]
        return {
            "candidates": len(cands),
            "method": method,
            "groups": [
                {
                    "size": len(g),
                    "domains": sorted({d for b in g for d in (b["metadata"].get("domain") or [])}),
                    "tags": sorted({t for b in g for t in (b["metadata"].get("tags") or [])})[:10],
                    "buckets": [
                        {
                            "id": b["id"],
                            "name": b["metadata"].get("name", b["id"]),
                            "importance": b["metadata"].get("importance", "?"),
                            "idle_days": round(self._idle_days(b["metadata"])),
                        }
                        for b in g
                    ],
                }
                for g in groups
            ],
            "note": "演习模式：以上只是计划，没有改动任何记忆。",
        }

    # ---------------------------------------------------------
    # 实际整理
    # ---------------------------------------------------------
    async def execute(self, max_groups: int = None) -> dict:
        """按计划整理。返回每组的结果；提炼失败的组整组跳过，不落库。"""
        if self.dehydrator is None:
            return {"error": "没有可用的提炼器（dehydrator），不执行"}

        cands = await self.candidates()
        if len(cands) < self.min_group:
            return {"candidates": len(cands), "digested": 0, "groups": [],
                    "note": "够条件的桶不到一组的下限，什么都没做"}

        groups, method = await self._group(cands)
        limit = max_groups or self.max_groups_per_run
        groups = groups[:limit]

        done = []
        for g in groups:
            try:
                result = await self._digest_group(g)
            except Exception as e:
                logger.error(f"Digest group failed / 沉淀一组失败: {e}")
                result = {"ok": False, "reason": str(e),
                          "sources": [b["id"] for b in g]}
            done.append(result)

        return {
            "candidates": len(cands), "method": method,
            "digested": sum(1 for d in done if d.get("ok")),
            "groups": done,
        }

    async def _digest_group(self, group: list[dict]) -> dict:
        contents = [(b.get("content") or "").strip() for b in group]
        contents = [c for c in contents if c]
        distilled = await self.dehydrator.sediment(contents)
        if not distilled:
            # 提炼不出来就整组放弃：拿拼接原文冒充摘要只是把碎片换个地方堆着
            return {"ok": False, "reason": "提炼失败，整组原样保留",
                    "sources": [b["id"] for b in group]}

        domains = [d for b in group for d in (b["metadata"].get("domain") or [])]
        primary = max(set(domains), key=domains.count) if domains else "未分类"
        tags = list(dict.fromkeys(
            (distilled.get("tags") or [])
            + [t for b in group for t in (b["metadata"].get("tags") or [])]
        ))[:15]
        importance = max(
            [int(b["metadata"].get("importance", 3) or 3) for b in group] + [1]
        )
        valence = sum(float(b["metadata"].get("valence", 0.5) or 0.5) for b in group) / len(group)
        arousal = sum(float(b["metadata"].get("arousal", 0.3) or 0.3) for b in group) / len(group)

        source_ids = [b["id"] for b in group]
        body = distilled["summary"] + "\n\n---\n沉淀自 " + str(len(group)) + " 条旧记忆:" + ", ".join(
            f"{b['metadata'].get('name', b['id'])}({b['id']})" for b in group
        )
        new_id = await self.bucket_mgr.create(
            content=body,
            tags=tags,
            importance=importance,
            domain=[primary],
            valence=round(valence, 2),
            arousal=round(arousal, 2),
            name=distilled.get("name") or "沉淀摘要",
        )
        await self._mark_sediment(new_id, source_ids)
        try:
            await self.bucket_mgr.set_related(new_id, source_ids, overwrite=True)
        except Exception:
            pass
        if self.embedding_engine is not None:
            try:
                await self.embedding_engine.generate_and_store(new_id, body)
            except Exception:
                pass

        archived = []
        for b in group:
            try:
                await self._mark_digested_into(b["id"], new_id)
                if await self.bucket_mgr.archive(b["id"]):
                    archived.append(b["id"])
            except Exception as e:
                logger.warning(f"Archive source failed / 归档源桶失败 {b['id']}: {e}")

        logger.info(f"Sediment created / 沉淀完成: {new_id} ← {source_ids}")
        return {"ok": True, "sediment_id": new_id, "name": distilled.get("name"),
                "sources": source_ids, "archived": archived}

    async def _mark_sediment(self, bucket_id: str, source_ids: list[str]) -> None:
        await self._write_fields(bucket_id, {
            "sediment": True, "sediment_sources": source_ids, "sediment_at": now_iso(),
        })

    async def _mark_digested_into(self, bucket_id: str, sediment_id: str) -> None:
        await self._write_fields(bucket_id, {
            "digested": True, "sediment_of": sediment_id,
        })

    async def _write_fields(self, bucket_id: str, fields: dict) -> bool:
        """直接写 frontmatter，**不刷新 last_active**。

        走 update() 的话，整理动作本身会把这批桶的「上次活跃」刷成现在 ——
        等于把「它们很久没被想起」这件事抹掉，下一轮扫描就再也看不见它们了。
        """
        async with self.bucket_mgr._lock_for(bucket_id):
            path = self.bucket_mgr._find_bucket_file(bucket_id)
            if not path:
                return False
            try:
                post = frontmatter.load(path)
                for k, v in fields.items():
                    post[k] = v
                with open(path, "w", encoding="utf-8") as f:
                    f.write(frontmatter.dumps(post))
                return True
            except Exception as e:
                logger.warning(f"Write sediment fields failed / 写沉淀字段失败 {bucket_id}: {e}")
                return False

    # ---------------------------------------------------------
    # 后台定期扫描：默认只演习 + 记日志
    # ---------------------------------------------------------
    @property
    def is_running(self) -> bool:
        return self._running

    async def ensure_started(self) -> None:
        if not self._running and self.scan_interval > 0:
            await self.start()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        logger.info(
            f"Digest engine started, every {self.scan_interval}h, "
            f"auto_execute={self.auto_execute} / 消化引擎已启动"
        )

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
                await asyncio.sleep(self.scan_interval * 3600)
            except asyncio.CancelledError:
                break
            try:
                plan = await self.plan()
                logger.info(f"Digest scan / 消化扫描: 候选 {plan.get('candidates')}, "
                            f"可整理 {len(plan.get('groups', []))} 组")
                if self.auto_execute and plan.get("groups"):
                    result = await self.execute()
                    logger.info(f"Digest auto-execute / 自动整理: {result.get('digested')} 组")
            except Exception as e:
                logger.error(f"Digest scan error / 消化扫描出错: {e}")
