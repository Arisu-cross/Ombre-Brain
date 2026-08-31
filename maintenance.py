# ============================================================
# Module: One-shot maintenance (maintenance.py)
# 模块：一次性维护 —— 给存量桶补情绪坐标 / 补关联
#
# 为什么单开一个模块:
#   这两件事本来是两个命令行脚本(backfill_mood.py / backfill_related.py),
#   但栖栖只有手机、也进不了容器 —— 让她「去服务器上跑个脚本」等于不可能。
#   所以核心逻辑放这里,两边共用:
#     - 命令行脚本:能进容器的人照旧用,支持 --dry-run
#     - 服务端自跑:开机后自己做一次,做完把结果写进标记文件(见 server.py)
#
# 两条铁律(两个入口都适用):
#   1. **不走 bucket_mgr.update()** —— 它会把 last_active 刷成现在。一次批量
#      补建就能把全库的「上次活跃」抹成同一时刻,时间维排序、衰减分、
#      「最近记下」全部跟着塌掉。所以直接写 frontmatter。
#   2. 每一步都能重复跑:已经补过的跳过,不会补第二次、不会覆盖人工整理过的关联。
# ============================================================

import logging
from datetime import datetime

import frontmatter

from utils import now_local

logger = logging.getLogger("ombre_brain.maintenance")

DEFAULT_VALENCE = 0.5
DEFAULT_AROUSAL = 0.3
EPS = 1e-9


# ---------------------------------------------------------
# 共用:直接写 frontmatter，不碰 last_active
# ---------------------------------------------------------
async def write_fields(bucket_mgr, bucket_id: str, fields: dict) -> bool:
    async with bucket_mgr._lock_for(bucket_id):
        path = bucket_mgr._find_bucket_file(bucket_id)
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
            logger.warning(f"Maintenance write failed / 维护写入失败 {bucket_id}: {e}")
            return False


def looks_default_mood(meta: dict) -> bool:
    """情绪坐标是不是还停在默认值上(或根本没这两个字段)。

    判定故意保守:只认 V0.5/A0.3 这一个组合。真有一条记忆就是不咸不淡,
    重打一次标也就是原样写回,不亏。
    """
    if "valence" not in meta or "arousal" not in meta:
        return True
    try:
        v = float(meta.get("valence"))
        a = float(meta.get("arousal"))
    except (TypeError, ValueError):
        return True
    return abs(v - DEFAULT_VALENCE) < EPS and abs(a - DEFAULT_AROUSAL) < EPS


def is_sealed(meta: dict) -> bool:
    """封存:归档 / 休眠 / feel / 已过期便利贴 —— 不做关联对象,自己也不补。"""
    if meta.get("type") in ("archived", "feel") or meta.get("dormant"):
        return True
    exp = meta.get("expires_at")
    if exp:
        try:
            return now_local() >= datetime.fromisoformat(str(exp))
        except (ValueError, TypeError):
            return False
    return False


# ---------------------------------------------------------
# 一、补情绪坐标
# ---------------------------------------------------------
async def backfill_mood(bucket_mgr, dehydrator, dry_run: bool = False,
                        limit: int = 0, on_item=None) -> dict:
    """给坐标停在默认值的桶重新打标。返回统计。

    feel 桶跳过:它的 valence 是**模型自己的感受**,不是事件效价,
    不能拿打标器去覆盖。
    """
    stats = {"task": "mood", "dry_run": dry_run, "scanned": 0, "targets": 0,
             "changed": 0, "unchanged": 0, "failed": 0}
    if not getattr(dehydrator, "api_available", False):
        stats["error"] = "打标 API 不可用,跳过"
        return stats

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    stats["scanned"] = len(all_buckets)
    targets = [
        b for b in all_buckets
        if b["metadata"].get("type") != "feel"
        and looks_default_mood(b["metadata"])
        and (b.get("content") or "").strip()
    ]
    if limit > 0:
        targets = targets[:limit]
    stats["targets"] = len(targets)

    for b in targets:
        name = b["metadata"].get("name", b["id"])
        try:
            analysis = await dehydrator.analyze(b["content"])
        except Exception as e:
            stats["failed"] += 1
            if on_item:
                on_item(f"  ERROR: {b['id'][:12]} ({name[:24]}): {e}")
            continue

        v = float(analysis.get("valence", DEFAULT_VALENCE))
        a = float(analysis.get("arousal", DEFAULT_AROUSAL))
        if abs(v - DEFAULT_VALENCE) < EPS and abs(a - DEFAULT_AROUSAL) < EPS:
            stats["unchanged"] += 1
            if on_item:
                on_item(f"  = 原样: {b['id'][:12]} ({name[:24]}) V{v:.2f}/A{a:.2f}")
            continue

        if dry_run:
            stats["changed"] += 1
            if on_item:
                on_item(f"  [dry-run] 将改: {b['id'][:12]} ({name[:24]}) "
                        f"V0.50/A0.30 → V{v:.2f}/A{a:.2f}")
            continue

        ok = await write_fields(bucket_mgr, b["id"], {
            "valence": max(0.0, min(1.0, v)), "arousal": max(0.0, min(1.0, a)),
        })
        if ok:
            stats["changed"] += 1
            if on_item:
                on_item(f"  OK: {b['id'][:12]} ({name[:24]}) → V{v:.2f}/A{a:.2f}")
        else:
            stats["failed"] += 1
            if on_item:
                on_item(f"  FAIL: {b['id'][:12]} ({name[:24]})")

    return stats


# ---------------------------------------------------------
# 二、补关联
# ---------------------------------------------------------
async def backfill_related(bucket_mgr, embedding_engine, dry_run: bool = False,
                           top_k: int = 3, min_sim: float = 0.55,
                           limit: int = 0, overwrite: bool = False,
                           on_item=None) -> dict:
    """给还没有 related 的未封存桶按语义补关联。返回统计。"""
    stats = {"task": "related", "dry_run": dry_run, "scanned": 0, "targets": 0,
             "linked": 0, "no_match": 0, "failed": 0}
    if not getattr(embedding_engine, "enabled", False):
        stats["error"] = "向量引擎不可用,跳过(先跑 backfill_embeddings.py)"
        return stats

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    stats["scanned"] = len(all_buckets)
    alive = {b["id"] for b in all_buckets if not is_sealed(b["metadata"])}
    targets = [
        b for b in all_buckets
        if b["id"] in alive and (overwrite or not b["metadata"].get("related"))
    ]
    if limit > 0:
        targets = targets[:limit]
    stats["targets"] = len(targets)

    for b in targets:
        name = b["metadata"].get("name", b["id"])
        try:
            # 多捞一些再过滤:直接 top_k 会出现「前几个全是归档桶」于是一个都不剩
            similar = await embedding_engine.find_similar_buckets(
                b["id"], top_k=max(top_k * 4, 8), min_sim=min_sim
            )
        except Exception as e:
            stats["failed"] += 1
            if on_item:
                on_item(f"  ERROR: {b['id'][:12]} ({name[:24]}): {e}")
            continue

        picked = [bid for bid, _ in similar if bid in alive][:top_k]
        if not picked:
            stats["no_match"] += 1
            if on_item:
                on_item(f"  - 没有够像的: {b['id'][:12]} ({name[:24]})")
            continue

        if dry_run:
            stats["linked"] += 1
            if on_item:
                on_item(f"  [dry-run] {b['id'][:12]} ({name[:24]}) → "
                        f"{', '.join(x[:12] for x in picked)}")
            continue

        ok = await bucket_mgr.set_related(b["id"], picked, overwrite=overwrite)
        if ok:
            stats["linked"] += 1
            if on_item:
                on_item(f"  OK: {b['id'][:12]} ({name[:24]}) → "
                        f"{', '.join(x[:12] for x in picked)}")
        else:
            stats["failed"] += 1
            if on_item:
                on_item(f"  FAIL: {b['id'][:12]} ({name[:24]})")

    return stats
