# ============================================================
# breath startup=True 一站式启动模式测试
# One-shot session bootstrap: surfacing + dreaming + recent feels.
#
# 验证:
#   - 一次调用返回 浮现 + Dreaming + feel 三个板块
#   - feel 只取最近3条
#   - 无任何记忆时不报错
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
async def test_startup_bundles_all_sections(patched_server, bucket_mgr):
    """startup=True 应同时包含 核心准则/浮现记忆、Dreaming、feel 三个板块。"""
    pinned_id = await bucket_mgr.create(
        content="核心准则", name="准则", domain=["日常"], pinned=True,
    )
    unresolved_id = await bucket_mgr.create(
        content="未解决的事", name="未解决", domain=["日常"], importance=8,
    )
    feel_id = await bucket_mgr.create(
        content="我的一点感受", name="feel1", bucket_type="feel",
    )

    out = await patched_server.breath(startup=True)

    assert "核心准则" in out
    assert pinned_id in out
    assert unresolved_id in out
    assert "Dreaming" in out
    assert "feel" in out
    assert feel_id in out


@pytest.mark.asyncio
async def test_startup_caps_feels_at_three(patched_server, bucket_mgr):
    """feel 板块只带最近 3 条，旧的不带。"""
    ids = []
    for i in range(5):
        bid = await bucket_mgr.create(
            content=f"感受{i}", name=f"feel{i}", bucket_type="feel",
        )
        ids.append(bid)
        # 用 created 时间区分先后
        import frontmatter as fm
        fpath = bucket_mgr._find_bucket_file(bid)
        post = fm.load(fpath)
        post["created"] = f"2024-01-{i + 1:02d}T00:00:00"
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(fm.dumps(post))

    out = await patched_server.breath(startup=True)

    shown = [bid for bid in ids if bid in out]
    assert len(shown) == 3, f"expected 3 recent feels, got {len(shown)}"
    assert ids[-1] in out  # newest present
    assert ids[0] not in out  # oldest trimmed


@pytest.mark.asyncio
async def test_startup_empty_store_does_not_crash(patched_server, bucket_mgr):
    """空库 startup 不报错，返回可读文本。"""
    out = await patched_server.breath(startup=True)
    assert isinstance(out, str) and out.strip()


@pytest.mark.asyncio
async def test_startup_takes_priority_over_search(patched_server, bucket_mgr):
    """startup=True 时忽略 query，不走搜索分支。"""
    await bucket_mgr.create(content="苹果记忆", name="苹果", domain=["日常"], importance=8)
    out = await patched_server.breath(query="苹果", startup=True)
    # 搜索分支的输出没有 Dreaming 板块；startup 有
    assert "Dreaming" in out
