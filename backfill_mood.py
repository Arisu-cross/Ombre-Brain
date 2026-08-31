#!/usr/bin/env python3
"""
Backfill emotion coordinates (valence/arousal) for existing buckets.
为存量桶批量补建情绪坐标。

核心逻辑在 maintenance.py(服务端「开机自跑一次」共用同一份代码)。
这个脚本是给能进容器的人用的手动入口。

判定「像默认值」故意保守:只认 valence==0.5 且 arousal==0.3 这一个组合;
feel 桶跳过(它的 valence 是模型自己的感受,不是事件效价)。

Usage:
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_mood.py --dry-run
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_mood.py [--limit 50]
"""

import asyncio
import argparse
import sys

sys.path.insert(0, ".")
from utils import load_config
from bucket_manager import BucketManager
from dehydrator import Dehydrator
from maintenance import backfill_mood


async def main(dry_run: bool, limit: int):
    config = load_config()
    bucket_mgr = BucketManager(config)
    dehydrator = Dehydrator(config)

    stats = await backfill_mood(bucket_mgr, dehydrator, dry_run=dry_run,
                                limit=limit, on_item=print)
    if stats.get("error"):
        print(f"ERROR: {stats['error']}")
        return
    verb = "将改" if dry_run else "已改"
    print(f"\n=== 总桶数 {stats['scanned']},需补 {stats['targets']};"
          f"{verb} {stats['changed']},原样 {stats['unchanged']},失败 {stats['failed']} ===")
    if dry_run:
        print("确认无误后去掉 --dry-run 再跑一次。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个(先小批量试)")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入")
    args = parser.parse_args()
    asyncio.run(main(args.dry_run, args.limit))
