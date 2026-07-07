# ============================================================
# breath wake=True 唤醒模式测试
# Triggered-wake mode: only pinned + recent archived buckets.
#
# 验证:
#   - wake=True 只返回钉选桶 + 最近归档桶,普通未解决桶不出现
#   - 归档桶按 archived_at(回退 last_active)降序,默认最多 5 条
#   - archive() 会写入 archived_at,排序优先于 last_active
#   - 无钉选/无归档时给出空态提示
# ============================================================

import frontmatter as fm
import pytest
from unittest.mock import patch


async def _set_meta(bucket_mgr, bucket_id, **fields):
    """Directly patch a bucket's frontmatter fields on disk for deterministic ordering."""
    fpath = bucket_mgr._find_bucket_file(bucket_id)
    post = fm.load(fpath)
    for k, v in fields.items():
        post[k] = v
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


@pytest.mark.asyncio
async def test_wake_returns_pinned_and_archived_only(patched_server, bucket_mgr):
    """wake=True 应只包含钉选桶 + 归档桶，普通未解决桶不应出现。"""
    pinned_id = await bucket_mgr.create(
        content="核心准则内容", name="核心准则", domain=["日常"], pinned=True,
    )
    archived_id = await bucket_mgr.create(
        content="旧的归档内容", name="旧记忆", domain=["日常"], importance=5,
    )
    assert await bucket_mgr.archive(archived_id)
    ordinary_id = await bucket_mgr.create(
        content="普通未解决记忆", name="普通记忆", domain=["日常"], importance=5,
    )

    out = await patched_server.breath(wake=True)

    assert pinned_id in out
    assert archived_id in out
    assert ordinary_id not in out
    assert "核心准则" in out
    assert "最近归档" in out


@pytest.mark.asyncio
async def test_wake_archived_sorted_desc_and_capped(patched_server, bucket_mgr):
    """归档桶按 archived_at 降序，默认最多取 5 条。"""
    ids = []
    for i in range(8):
        bid = await bucket_mgr.create(
            content=f"归档内容{i}", name=f"归档{i}", domain=["日常"], importance=5,
        )
        assert await bucket_mgr.archive(bid)
        await _set_meta(bucket_mgr, bid, archived_at=f"2024-01-{i + 1:02d}T00:00:00")
        ids.append(bid)  # ids[i] archived at day (i+1), later = more recent

    out = await patched_server.breath(wake=True, mode="summary")

    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 5, f"expected default cap of 5, got {len(shown)}"
    # most recent (highest day number) should be present, oldest should be trimmed
    assert ids[-1] in out
    assert ids[0] not in out


@pytest.mark.asyncio
async def test_archive_stamps_archived_at(bucket_mgr):
    """archive() 应写入 archived_at 字段，记录真实归档时刻。"""
    bid = await bucket_mgr.create(content="待归档", name="待归档", domain=["日常"])
    assert await bucket_mgr.archive(bid)
    bucket = await bucket_mgr.get(bid)
    assert bucket["metadata"].get("archived_at"), "archive() did not stamp archived_at"


@pytest.mark.asyncio
async def test_wake_prefers_archived_at_over_last_active(patched_server, bucket_mgr):
    """last_active 被批量操作重写时，wake 排序仍按 archived_at 取最近归档。"""
    old_id = await bucket_mgr.create(content="早归档", name="早归档", domain=["日常"])
    new_id = await bucket_mgr.create(content="晚归档", name="晚归档", domain=["日常"])
    assert await bucket_mgr.archive(old_id)
    assert await bucket_mgr.archive(new_id)
    # 模拟批量操作把两桶 last_active 刷成同一时刻，且早归档的 last_active 更新
    await _set_meta(bucket_mgr, old_id,
                    archived_at="2024-01-01T00:00:00", last_active="2025-12-31T00:00:00")
    await _set_meta(bucket_mgr, new_id,
                    archived_at="2024-06-01T00:00:00", last_active="2025-12-31T00:00:00")

    out = await patched_server.breath(wake=True, max_results=1)
    assert new_id in out, "wake should pick the bucket with the newer archived_at"
    assert old_id not in out


@pytest.mark.asyncio
async def test_wake_respects_explicit_max_results(patched_server, bucket_mgr):
    """显式 max_results 应覆盖 wake 模式的默认归档条数。"""
    ids = []
    for i in range(6):
        bid = await bucket_mgr.create(
            content=f"归档内容{i}", name=f"归档{i}", domain=["日常"], importance=5,
        )
        assert await bucket_mgr.archive(bid)
        ids.append(bid)

    out = await patched_server.breath(wake=True, max_results=2)
    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 2, f"expected explicit cap of 2, got {len(shown)}"


@pytest.mark.asyncio
async def test_wake_keeps_min_two_archived_under_tight_budget(patched_server, bucket_mgr):
    """token 预算极紧时仍保底 2 条最近归档（2-5 条区间的下限）。"""
    ids = []
    for i in range(4):
        bid = await bucket_mgr.create(
            content=f"归档内容{i}" * 50, name=f"归档{i}", domain=["日常"], importance=5,
        )
        assert await bucket_mgr.archive(bid)
        ids.append(bid)

    out = await patched_server.breath(wake=True, max_tokens=1)

    shown = [bid for bid in ids if bid in out]
    assert len(shown) >= 2, f"expected min 2 archived under tight budget, got {len(shown)}"


@pytest.mark.asyncio
async def test_wake_empty_state(patched_server, bucket_mgr):
    """没有钉选也没有归档时给出明确的空态提示。"""
    await bucket_mgr.create(content="普通记忆", name="普通", domain=["日常"], importance=5)
    out = await patched_server.breath(wake=True)
    assert "没有" in out
