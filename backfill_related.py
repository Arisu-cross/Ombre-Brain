#!/usr/bin/env python3
"""
Backfill 'related' links for existing buckets.
为存量桶批量补建关联(related)。

背景:关联原本只在两个时机写入 —— breath 命中某个桶时补(_ensure_related),
以及新桶入库时自动挂(hold)。**从没被搜到过、又是在自动关联上线前存进来的**
那批桶,一直是孤岛。这个脚本一次性把它们补齐。

封存桶不参与:已归档 / 休眠 / 过期便利贴 / feel。关联是给还活着的记忆用的
路标,指向已经沉下去的东西只会把人带回坟场。(它们**自己**也不补关联。)

Usage:
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_related.py --dry-run
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_related.py [--top-k 3] [--min-sim 0.55] [--overwrite]

--dry-run 只报告会给谁挂上谁,不写任何文件(先跑这个)。
--overwrite 连已有 related 的桶也重算(默认跳过,不覆盖人工整理过的关联)。
"""

import asyncio
import argparse
import sys

sys.path.insert(0, ".")
from utils import load_config
from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine


def _sealed(meta: dict) -> bool:
    """封存:归档 / 休眠 / feel / 已过期便利贴。"""
    from datetime import datetime
    from utils import now_local

    if meta.get("type") in ("archived", "feel") or meta.get("dormant"):
        return True
    exp = meta.get("expires_at")
    if exp:
        try:
            return now_local() >= datetime.fromisoformat(str(exp))
        except (ValueError, TypeError):
            return False
    return False


async def backfill(top_k: int = 3, min_sim: float = 0.55,
                   dry_run: bool = False, overwrite: bool = False, limit: int = 0):
    config = load_config()
    bucket_mgr = BucketManager(config)
    engine = EmbeddingEngine(config)

    if not engine.enabled:
        print("ERROR: 向量引擎不可用(缺 API key?),没法按语义补关联。")
        print("       先跑 backfill_embeddings.py 把 embedding 补上。")
        return

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    alive = {b["id"] for b in all_buckets if not _sealed(b["metadata"])}
    print(f"总桶数 {len(all_buckets)},其中未封存 {len(alive)}")

    targets = [
        b for b in all_buckets
        if b["id"] in alive and (overwrite or not b["metadata"].get("related"))
    ]
    if limit > 0:
        targets = targets[:limit]
    print(f"待补关联 {len(targets)} 个" + ("(含已有关联的,--overwrite)" if overwrite else ""))
    if not targets:
        return

    linked = empty = failed = 0
    for b in targets:
        name = b["metadata"].get("name", b["id"])
        try:
            # 多捞一些再过滤:直接 top_k 会出现「前几个全是归档桶」于是一个都不剩
            similar = await engine.find_similar_buckets(
                b["id"], top_k=max(top_k * 4, 8), min_sim=min_sim
            )
        except Exception as e:
            failed += 1
            print(f"  ERROR: {b['id'][:12]} ({name[:24]}): {e}")
            continue

        picked = [bid for bid, _ in similar if bid in alive][:top_k]
        if not picked:
            empty += 1
            print(f"  - 没有够像的: {b['id'][:12]} ({name[:24]})")
            continue

        if dry_run:
            linked += 1
            print(f"  [dry-run] {b['id'][:12]} ({name[:24]}) → {', '.join(x[:12] for x in picked)}")
            continue

        ok = await bucket_mgr.set_related(b["id"], picked, overwrite=overwrite)
        if ok:
            linked += 1
            print(f"  OK: {b['id'][:12]} ({name[:24]}) → {', '.join(x[:12] for x in picked)}")
        else:
            failed += 1
            print(f"  FAIL: {b['id'][:12]} ({name[:24]})")

    verb = "将挂上" if dry_run else "已挂上"
    print(f"\n=== 完成:{verb} {linked},没有够像的 {empty},失败 {failed} ===")
    if dry_run:
        print("确认无误后去掉 --dry-run 再跑一次。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--min-sim", type=float, default=0.55)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个(先小批量试)")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入")
    parser.add_argument("--overwrite", action="store_true", help="连已有 related 的也重算")
    args = parser.parse_args()
    asyncio.run(backfill(top_k=args.top_k, min_sim=args.min_sim, dry_run=args.dry_run,
                         overwrite=args.overwrite, limit=args.limit))
