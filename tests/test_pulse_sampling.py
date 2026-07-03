# ============================================================
# pulse 非钉选桶随机抽样测试
# pulse's non-pinned bucket selection: random sample, not weight-ranked.
#
# 验证:
#   - 钉选桶始终全部展示
#   - 非钉选桶固定抽样 5 条（不足 5 条时全部展示）
#   - 多次调用抽样结果不同（不是按权重的确定性排序）
#   - 末尾统计行格式不变
# ============================================================

import pytest
from unittest.mock import patch


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


@pytest.mark.asyncio
async def test_pulse_shows_all_pinned_and_five_random(patched_server, bucket_mgr):
    """钉选桶全展示；非钉选桶抽样 5 条，统计行反映 6/21。"""
    pinned_id = await bucket_mgr.create(content="核心准则", name="准则", domain=["日常"], pinned=True)
    ids = [
        await bucket_mgr.create(content=f"事情{i}", name=f"n{i}", domain=["日常"], importance=(i % 10) + 1)
        for i in range(20)
    ]

    out = await patched_server.pulse()

    assert pinned_id in out
    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 5, f"expected 5 non-pinned buckets, got {len(shown)}"
    assert "显示 6/21" in out
    assert "=== 统计 ===" in out


@pytest.mark.asyncio
async def test_pulse_random_sample_varies_across_calls(patched_server, bucket_mgr):
    """多次调用应看到不同的非钉选桶集合，证明不是按权重的确定性排序。"""
    ids = [
        await bucket_mgr.create(content=f"事情{i}", name=f"n{i}", domain=["日常"], importance=(i % 10) + 1)
        for i in range(30)
    ]

    samples = set()
    for _ in range(8):
        out = await patched_server.pulse()
        samples.add(frozenset(bid for bid in ids if bid in out))

    assert len(samples) > 1, "pulse non-pinned sampling looks deterministic, not random"


@pytest.mark.asyncio
async def test_pulse_fewer_than_five_non_pinned_shows_all(patched_server, bucket_mgr):
    """非钉选桶不足 5 条时全部展示，不报错。"""
    ids = [
        await bucket_mgr.create(content=f"事情{i}", name=f"n{i}", domain=["日常"])
        for i in range(3)
    ]

    out = await patched_server.pulse()
    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 3
    assert "显示 3/3" in out
