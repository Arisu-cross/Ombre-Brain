# ============================================================
# breath startup=True 唤醒模式测试
# Startup wake-up: pinned + recent archived (2-5) + recent held, no dreaming/feel.
#
# 验证:
#   - startup=True 返回 钉选桶 + 最近归档桶 + 最近记下的动态桶（与 wake 统一）
#   - 不触发 Dreaming 板块
#   - 不带 feel 桶
#   - 无任何记忆时不报错
#   - startup 优先于 query 搜索
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
async def test_startup_returns_pinned_archived_and_recent(patched_server, bucket_mgr):
    """startup=True 包含 钉选桶 + 归档桶 + 最近记下的动态桶。"""
    pinned_id = await bucket_mgr.create(
        content="核心准则", name="准则", domain=["日常"], pinned=True,
    )
    archived_id = await bucket_mgr.create(
        content="上个窗口的会话总结", name="会话总结", domain=["日常"], importance=5,
    )
    assert await bucket_mgr.archive(archived_id)
    ordinary_id = await bucket_mgr.create(
        content="未解决的事", name="未解决", domain=["日常"], importance=8,
    )

    out = await patched_server.breath(startup=True)

    assert pinned_id in out
    assert archived_id in out
    assert ordinary_id in out, "recently held dynamic bucket should surface in 最近记下"
    assert "核心准则" in out
    assert "最近归档" in out
    assert "最近记下" in out


@pytest.mark.asyncio
async def test_startup_does_not_trigger_dreaming(patched_server, bucket_mgr):
    """唤醒不触发 Dreaming：即使有未解决记忆，也不出现 Dreaming 板块。"""
    await bucket_mgr.create(
        content="未解决的事", name="未解决", domain=["日常"], importance=8,
    )
    pinned_id = await bucket_mgr.create(
        content="核心准则", name="准则", domain=["日常"], pinned=True,
    )

    out = await patched_server.breath(startup=True)

    assert pinned_id in out
    assert "Dreaming" not in out, "startup wake-up must not trigger dreaming"


@pytest.mark.asyncio
async def test_startup_does_not_include_feels(patched_server, bucket_mgr):
    """唤醒不带 feel：feel 桶只通过 breath(domain='feel') 显式读取。"""
    pinned_id = await bucket_mgr.create(
        content="核心准则", name="准则", domain=["日常"], pinned=True,
    )
    feel_id = await bucket_mgr.create(
        content="我的一点感受", name="feel1", bucket_type="feel",
    )

    out = await patched_server.breath(startup=True)

    assert pinned_id in out
    assert feel_id not in out, "feel bucket leaked into startup wake-up"

    # feel 仍可通过显式通道读取
    feel_out = await patched_server.breath(query="x", domain="feel")
    assert feel_id in feel_out


@pytest.mark.asyncio
async def test_startup_empty_store_does_not_crash(patched_server, bucket_mgr):
    """空库 startup 不报错，返回可读文本。"""
    out = await patched_server.breath(startup=True)
    assert isinstance(out, str) and out.strip()


@pytest.mark.asyncio
async def test_startup_takes_priority_over_search(patched_server, bucket_mgr):
    """startup=True 时忽略 query，不走搜索分支。"""
    ordinary_id = await bucket_mgr.create(
        content="苹果记忆", name="苹果", domain=["日常"], importance=8,
    )
    out = await patched_server.breath(query="苹果", startup=True)
    # 走的是唤醒浮现(分板块输出),不是搜索分支;桶经由「最近记下」出现
    assert "===" in out, "startup should take the wake branch, not search"
    assert "最近记下" in out
    assert ordinary_id in out
