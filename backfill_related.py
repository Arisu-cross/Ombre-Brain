#!/usr/bin/env python3
"""
Backfill 'related' links for existing buckets.
为存量桶批量补建关联(related)。

核心逻辑在 maintenance.py(服务端「开机自跑一次」共用同一份代码)。
这个脚本是给能进容器的人用的手动入口。

背景:关联原本只在两个时机写入 —— breath 命中某个桶时补,以及新桶入库时自动挂。
**从没被搜到过、又是在自动关联上线前存进来的**那批桶,一直是孤岛。
封存桶(归档/休眠/feel/过期便利贴)不做关联对象,自己也不补。

Usage:
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_related.py --dry-run
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_related.py [--top-k 3] [--min-sim 0.55] [--overwrite]
"""

import asyncio
import argparse
import sys

sys.path.insert(0, ".")
from utils import load_config
from bucket_manager import BucketManager
from embedding_engine import EmbeddingEngine
from maintenance import backfill_related


async def main(args):
    config = load_config()
    bucket_mgr = BucketManager(config)
    engine = EmbeddingEngine(config)

    stats = await backfill_related(
        bucket_mgr, engine, dry_run=args.dry_run, top_k=args.top_k,
        min_sim=args.min_sim, limit=args.limit, overwrite=args.overwrite,
        on_item=print,
    )
    if stats.get("error"):
        print(f"ERROR: {stats['error']}")
        return
    verb = "将挂上" if args.dry_run else "已挂上"
    print(f"\n=== 总桶数 {stats['scanned']},待补 {stats['targets']};"
          f"{verb} {stats['linked']},没有够像的 {stats['no_match']},失败 {stats['failed']} ===")
    if args.dry_run:
        print("确认无误后去掉 --dry-run 再跑一次。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--top-k", type=int, default=3)
    parser.add_argument("--min-sim", type=float, default=0.55)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个(先小批量试)")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入")
    parser.add_argument("--overwrite", action="store_true", help="连已有 related 的也重算")
    args = parser.parse_args()
    asyncio.run(main(args))
