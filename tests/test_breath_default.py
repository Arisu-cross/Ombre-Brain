# ============================================================
# breath 无 query 默认分支测试
# Default (no-query) branch: pinned + recent archived session summaries
# + recent hold-written dynamic buckets ("最近记下").
#
# 验证:
#   - 无 query 返回 钉选桶 + 最近归档的会话总结 + 最近记下的动态桶
#   - 归档桶按 archived_at 降序,默认最多 5 条
#   - 「最近记下」按 last_active 降序,默认最多 BREATH_RECENT_N(3) 条
#   - 显式 max_results 覆盖默认归档条数
#   - 什么都没有时给出空态提示
#   - startup=True 的浮现板块同理(带最近归档 + 最近记下)
#   - /breath-hook SessionStart 钩子同理
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
async def test_default_returns_pinned_archived_and_recent(patched_server, bucket_mgr):
    """无 query 应包含钉选桶 + 归档桶 + 最近记下的动态桶(hold 写入的)。"""
    pinned_id = await bucket_mgr.create(
        content="核心准则内容", name="核心准则", domain=["日常"], pinned=True,
    )
    archived_id = await bucket_mgr.create(
        content="上个窗口的会话总结", name="会话总结", domain=["日常"], importance=5,
    )
    assert await bucket_mgr.archive(archived_id)
    ordinary_id = await bucket_mgr.create(
        content="普通动态记忆", name="普通记忆", domain=["日常"], importance=8,
    )

    out = await patched_server.breath()

    assert pinned_id in out
    assert archived_id in out
    assert ordinary_id in out, "recently held dynamic bucket should surface in 最近记下"
    assert "核心准则" in out
    assert "最近归档" in out
    assert "最近记下" in out


@pytest.mark.asyncio
async def test_default_recent_sorted_desc_and_capped(patched_server, bucket_mgr):
    """「最近记下」按 last_active 降序，默认最多 BREATH_RECENT_N(3) 条。"""
    ids = []
    for i in range(6):
        bid = await bucket_mgr.create(
            content=f"动态记忆{i}", name=f"动态{i}", domain=["日常"], importance=5,
        )
        await _set_meta(bucket_mgr, bid, last_active=f"2024-02-{i + 1:02d}T00:00:00")
        ids.append(bid)  # ids[i] active at day (i+1), later = more recent

    out = await patched_server.breath(mode="summary")

    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 3, f"expected recent cap of 3, got {len(shown)}"
    assert ids[-1] in out  # most recent present
    assert ids[0] not in out  # oldest trimmed


@pytest.mark.asyncio
async def test_default_ordinary_bucket_still_searchable(patched_server, bucket_mgr):
    """老的动态桶(不在最近几条里)不出现在默认返回，但语义搜索仍可检索到。"""
    old_id = await bucket_mgr.create(
        content="关于苹果的普通记忆，苹果很重要。", name="苹果记忆",
        domain=["日常"], tags=["苹果"], importance=5,
    )
    await _set_meta(bucket_mgr, old_id, last_active="2020-01-01T00:00:00")
    for i in range(3):  # 挤掉最近记下的名额
        bid = await bucket_mgr.create(
            content=f"新动态记忆{i}", name=f"新动态{i}", domain=["日常"], importance=5,
        )
        await _set_meta(bucket_mgr, bid, last_active=f"2024-02-{i + 1:02d}T00:00:00")

    default_out = await patched_server.breath()
    search_out = await patched_server.breath(query="苹果")

    assert old_id not in default_out
    assert old_id in search_out, "old bucket should remain reachable via search"


@pytest.mark.asyncio
async def test_default_archived_sorted_desc_and_capped(patched_server, bucket_mgr):
    """归档桶按 archived_at 降序，默认最多取 5 条。"""
    ids = []
    for i in range(8):
        bid = await bucket_mgr.create(
            content=f"归档内容{i}", name=f"归档{i}", domain=["日常"], importance=5,
        )
        assert await bucket_mgr.archive(bid)
        await _set_meta(bucket_mgr, bid, archived_at=f"2024-01-{i + 1:02d}T00:00:00")
        ids.append(bid)  # ids[i] archived at day (i+1), later = more recent

    out = await patched_server.breath(mode="summary")

    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 5, f"expected default cap of 5, got {len(shown)}"
    assert ids[-1] in out  # most recent present
    assert ids[0] not in out  # oldest trimmed


@pytest.mark.asyncio
async def test_default_respects_explicit_max_results(patched_server, bucket_mgr):
    """显式 max_results 应覆盖默认归档条数。"""
    ids = []
    for i in range(6):
        bid = await bucket_mgr.create(
            content=f"归档内容{i}", name=f"归档{i}", domain=["日常"], importance=5,
        )
        assert await bucket_mgr.archive(bid)
        ids.append(bid)

    out = await patched_server.breath(max_results=2)
    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 2, f"expected explicit cap of 2, got {len(shown)}"


@pytest.mark.asyncio
async def test_default_empty_state(patched_server, bucket_mgr):
    """完全没有记忆时给出明确的空态提示。"""
    out = await patched_server.breath()
    assert "没有" in out


@pytest.mark.asyncio
async def test_startup_surfaces_recent_archived(patched_server, bucket_mgr):
    """startup=True 的浮现板块同理：带最近归档的会话总结。"""
    archived_id = await bucket_mgr.create(
        content="上个窗口的会话总结", name="会话总结", domain=["日常"],
    )
    assert await bucket_mgr.archive(archived_id)

    out = await patched_server.breath(startup=True)

    assert archived_id in out
    assert "最近归档" in out


@pytest.mark.asyncio
async def test_breath_hook_includes_recent_dynamic(patched_server, bucket_mgr):
    """/breath-hook 钩子同理：钉选桶 + 最近归档 + 最近记下的动态桶。"""
    pinned_id = await bucket_mgr.create(
        content="核心准则内容", name="核心准则", domain=["日常"], pinned=True,
    )
    archived_id = await bucket_mgr.create(
        content="上个窗口的会话总结", name="会话总结", domain=["日常"],
    )
    assert await bucket_mgr.archive(archived_id)
    ordinary_id = await bucket_mgr.create(
        content="普通动态记忆", name="普通记忆", domain=["日常"], importance=8,
    )

    resp = await patched_server.breath_hook(None)
    body = resp.body.decode("utf-8")

    assert pinned_id in body
    assert archived_id in body
    assert ordinary_id in body, "recently held bucket should surface in breath-hook"
    assert "最近记下" in body
