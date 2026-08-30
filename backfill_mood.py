#!/usr/bin/env python3
"""
Backfill emotion coordinates (valence/arousal) for existing buckets.
为存量桶批量补建情绪坐标。

一次性脚本:心境共鸣检索(breath 只给 valence/arousal)按坐标距离排序,
坐标不准的桶会排到错的位置上。真正缺字段的桶其实很少 —— 绝大多数是
create() 时打过标的;要补的是那批**停在默认值 (V0.5/A0.3) 上**的:
早期导入、打标失败回退、或者手写进去的。

判定「像默认值」故意保守:只认 valence==0.5 且 arousal==0.3 这一个组合。
真有一条记忆情绪就是不好不坏、不咸不淡,重打一次标也就是原样写回,不亏。

Usage:
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_mood.py --dry-run
    OMBRE_BUCKETS_DIR=/data OMBRE_API_KEY=xxx python backfill_mood.py [--batch-size 20] [--limit 50]

--dry-run 只报告会改哪些桶、改成什么,不写任何文件(先跑这个)。
"""

import asyncio
import argparse
import sys

sys.path.insert(0, ".")
from utils import load_config
from bucket_manager import BucketManager
from dehydrator import Dehydrator

DEFAULT_VALENCE = 0.5
DEFAULT_AROUSAL = 0.3
EPS = 1e-9


def _looks_default(meta: dict) -> bool:
    """情绪坐标是不是还停在默认值上(或者根本没有这两个字段)。"""
    if "valence" not in meta or "arousal" not in meta:
        return True
    try:
        v = float(meta.get("valence"))
        a = float(meta.get("arousal"))
    except (TypeError, ValueError):
        return True
    return abs(v - DEFAULT_VALENCE) < EPS and abs(a - DEFAULT_AROUSAL) < EPS


async def backfill(batch_size: int = 20, dry_run: bool = False, limit: int = 0):
    config = load_config()
    bucket_mgr = BucketManager(config)
    dehydrator = Dehydrator(config)

    if not dehydrator.api_available:
        print("ERROR: 打标 API 不可用(缺 OMBRE_API_KEY?),没法重新分析情绪坐标")
        return

    all_buckets = await bucket_mgr.list_all(include_archive=True)
    print(f"总桶数 / total buckets: {len(all_buckets)}")

    # feel 桶的 valence 是**模型自己的感受**,不是事件效价,不能拿打标器去覆盖
    targets = [
        b for b in all_buckets
        if b["metadata"].get("type") != "feel"
        and _looks_default(b["metadata"])
        and (b.get("content") or "").strip()
    ]
    if limit > 0:
        targets = targets[:limit]
    print(f"坐标停在默认值、需要补建 / need backfill: {len(targets)}")
    if not targets:
        return

    if dry_run:
        print("\n[dry-run] 只看不改。逐条重新打标预览:")
    changed = failed = unchanged = 0

    for i in range(0, len(targets), batch_size):
        batch = targets[i : i + batch_size]
        print(f"\n--- 批次 {i // batch_size + 1}/{(len(targets) + batch_size - 1) // batch_size} "
              f"({len(batch)} 桶) ---")
        for b in batch:
            name = b["metadata"].get("name", b["id"])
            try:
                analysis = await dehydrator.analyze(b["content"])
            except Exception as e:
                failed += 1
                print(f"  ERROR: {b['id'][:12]} ({name[:24]}): {e}")
                continue

            v = float(analysis.get("valence", DEFAULT_VALENCE))
            a = float(analysis.get("arousal", DEFAULT_AROUSAL))
            if abs(v - DEFAULT_VALENCE) < EPS and abs(a - DEFAULT_AROUSAL) < EPS:
                unchanged += 1
                print(f"  = 原样: {b['id'][:12]} ({name[:24]}) V{v:.2f}/A{a:.2f}")
                continue

            if dry_run:
                changed += 1
                print(f"  [dry-run] 将改: {b['id'][:12]} ({name[:24]}) "
                      f"V0.50/A0.30 → V{v:.2f}/A{a:.2f}")
                continue

            # 只改这两个字段。注意 update() 会刷新 last_active ——
            # 批量补建把全库的活跃时间刷成同一时刻会毁掉时间维排序,
            # 所以这里走 set_mood(不碰 last_active)。
            ok = await _write_mood(bucket_mgr, b["id"], v, a)
            if ok:
                changed += 1
                print(f"  OK: {b['id'][:12]} ({name[:24]}) → V{v:.2f}/A{a:.2f}")
            else:
                failed += 1
                print(f"  FAIL: {b['id'][:12]} ({name[:24]})")

        if i + batch_size < len(targets):
            await asyncio.sleep(2)   # 给 API 限流留点余量

    verb = "将改" if dry_run else "已改"
    print(f"\n=== 完成:{verb} {changed},原样 {unchanged},失败 {failed} ===")
    if dry_run:
        print("确认无误后去掉 --dry-run 再跑一次。")


async def _write_mood(bucket_mgr: BucketManager, bucket_id: str, valence: float, arousal: float) -> bool:
    """直接写 frontmatter 的 valence/arousal,**不碰 last_active**。

    不能用 update():它会把 last_active 刷成现在。一次批量补建就能把全库的
    「上次活跃」抹成同一时刻 —— 时间维排序、衰减分、最近记下全部跟着塌掉。
    """
    import frontmatter

    async with bucket_mgr._lock_for(bucket_id):
        path = bucket_mgr._find_bucket_file(bucket_id)
        if not path:
            return False
        try:
            post = frontmatter.load(path)
            post["valence"] = max(0.0, min(1.0, float(valence)))
            post["arousal"] = max(0.0, min(1.0, float(arousal)))
            with open(path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))
            return True
        except Exception as e:
            print(f"    写入失败 {bucket_id}: {e}")
            return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-size", type=int, default=20)
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 个(先小批量试)")
    parser.add_argument("--dry-run", action="store_true", help="只报告不写入")
    args = parser.parse_args()
    asyncio.run(backfill(batch_size=args.batch_size, dry_run=args.dry_run, limit=args.limit))
