# ============================================================
# BucketManager 并发写保护测试
# Per-bucket asyncio.Lock: concurrent read-modify-write must not
# silently drop updates.
#
# 验证:
#   - 并发 touch 同一个桶,activation_count 不丢计数
#   - 并发 update 不同字段,后写不覆盖先写
# ============================================================

import asyncio
import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_concurrent_touch_does_not_lose_counts(bucket_mgr):
    """20 个并发 touch 后 activation_count 应恰好为 20（无锁时会互相覆盖丢计数）。"""
    bid = await bucket_mgr.create(content="并发测试", name="并发", domain=["日常"])

    # 关掉时间涟漪的干扰（它会给相邻桶加 0.3，但这里只有一个桶，自身不受影响）
    async def run_touch():
        await bucket_mgr.touch(bid)

    await asyncio.gather(*(run_touch() for _ in range(20)))

    bucket = await bucket_mgr.get(bid)
    count = bucket["metadata"].get("activation_count", 0)
    assert count == 20, f"lost updates under concurrency: activation_count={count}"


@pytest.mark.asyncio
async def test_concurrent_updates_preserve_both_fields(bucket_mgr):
    """并发 update 两个不同字段，两个改动都必须落盘。"""
    bid = await bucket_mgr.create(content="并发测试", name="并发", domain=["日常"], importance=5)

    await asyncio.gather(
        bucket_mgr.update(bid, importance=9),
        bucket_mgr.update(bid, tags=["并发标签"]),
    )

    bucket = await bucket_mgr.get(bid)
    assert int(bucket["metadata"].get("importance", 0)) == 9
    assert "并发标签" in bucket["metadata"].get("tags", [])


@pytest.mark.asyncio
async def test_update_waits_for_held_lock(bucket_mgr):
    """写操作必须等锁：锁被占用时 update 不得动文件，释放后才执行。"""
    bid = await bucket_mgr.create(content="锁测试", name="锁", domain=["日常"], importance=5)

    lock = bucket_mgr._lock_for(bid)
    await lock.acquire()
    task = asyncio.create_task(bucket_mgr.update(bid, importance=9))
    await asyncio.sleep(0.05)  # give the task a chance to (incorrectly) proceed

    assert not task.done(), "update bypassed the per-bucket lock"
    bucket = await bucket_mgr.get(bid)
    assert int(bucket["metadata"].get("importance", 0)) == 5, "file changed while lock was held"

    lock.release()
    assert await task is True
    bucket = await bucket_mgr.get(bid)
    assert int(bucket["metadata"].get("importance", 0)) == 9


@pytest.mark.asyncio
async def test_lock_is_per_bucket(bucket_mgr):
    """不同桶拿到的是不同的锁，同一桶拿到同一把锁。"""
    a = bucket_mgr._lock_for("bucket-a")
    b = bucket_mgr._lock_for("bucket-b")
    a2 = bucket_mgr._lock_for("bucket-a")
    assert a is a2
    assert a is not b
