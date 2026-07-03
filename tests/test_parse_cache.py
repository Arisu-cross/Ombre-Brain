# ============================================================
# _load_bucket 解析缓存测试
# mtime/size 签名缓存: 未变更文件不重复解析,变更后自动失效,
# 调用方的就地修改不污染缓存。
# ============================================================

import pytest
from unittest.mock import patch


@pytest.mark.asyncio
async def test_cache_hit_skips_reparse(bucket_mgr):
    """同一文件未变更时，第二次 list_all 不再调用 frontmatter.load。"""
    await bucket_mgr.create(content="缓存测试", name="缓存", domain=["日常"])
    await bucket_mgr.list_all()  # warm cache

    import bucket_manager as bm
    with patch.object(bm.frontmatter, "load", side_effect=AssertionError("re-parsed unchanged file")):
        buckets = await bucket_mgr.list_all()
    assert len(buckets) == 1


@pytest.mark.asyncio
async def test_cache_invalidated_on_write(bucket_mgr):
    """update 改写文件后，缓存自动失效，读到新内容。"""
    bid = await bucket_mgr.create(content="旧内容", name="缓存", domain=["日常"], importance=5)
    await bucket_mgr.list_all()  # warm cache

    await bucket_mgr.update(bid, importance=9)
    buckets = await bucket_mgr.list_all()
    assert int(buckets[0]["metadata"]["importance"]) == 9


@pytest.mark.asyncio
async def test_caller_mutation_does_not_pollute_cache(bucket_mgr):
    """调用方就地改返回的 metadata（如 _ensure_related），不能影响下次读取。"""
    await bucket_mgr.create(content="污染测试", name="污染", domain=["日常"])
    first = (await bucket_mgr.list_all())[0]
    first["metadata"]["related"] = ["fake-id"]
    first["score"] = 99

    second = (await bucket_mgr.list_all())[0]
    assert "related" not in second["metadata"]
    assert "score" not in second
