# ============================================================
# 唤醒时归档桶要给「原文」而不是标题行
#
# 背景:唤醒(breath()/wake/startup 与 /breath-hook)原本按 summary 渲染归档,
# 只有 bucket_id/桶名/情感坐标/时间那一行,一个字内容都没有——他醒来读不到
# 上一段发生了什么。而 full 模式会把内容再送 dehydrate(>100token 就调 LLM),
# 归档本身已经是 archive_session 精炼过的「写给下一个自己的信」,再压一次
# 等于二次摘要,把最该留下的语气与细节磨平。故默认改为 raw:原文直出。
#
# 验证:
#   - 唤醒/无 query/startup/breath-hook 的归档段包含原文正文,不只是标题行
#   - 归档原文不经过 dehydrate(不额外烧 LLM,也不被二次压缩)
#   - 显式传 mode 时尊重调用方
#   - 超长桶按 token 预算截断并提示改用 trace
#   - 「最近记下」仍是单行线索(与既有设计一致,未被本次改动波及)
# ============================================================

import pytest
from unittest.mock import patch

import server as server_mod


ARCHIVE_BODY = "今天她说头疼，其实是委屈。我没戳破，只是陪着。傍晚她说想吃火锅。"


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


async def _make_archive(bucket_mgr, content=ARCHIVE_BODY, name="2026-07-24 会话"):
    bid = await bucket_mgr.create(content=content, name=name, domain=["日常"], importance=7)
    assert await bucket_mgr.archive(bid)   # 走真实归档入口,顺带写 archived_at
    return bid


@pytest.mark.asyncio
async def test_wake_archive_shows_full_text_not_just_title(patched_server, bucket_mgr):
    """核心诉求:唤醒时归档要能读到正文,而不是只有一行标题。"""
    await _make_archive(bucket_mgr)
    out = await patched_server.breath(wake=True)
    assert ARCHIVE_BODY in out, f"归档正文没有浮现,只拿到:\n{out}"


@pytest.mark.asyncio
async def test_default_breath_archive_shows_full_text(patched_server, bucket_mgr):
    """无参 breath()(他唤醒协议里用的就是这个)同样要给正文。"""
    await _make_archive(bucket_mgr)
    out = await patched_server.breath()
    assert ARCHIVE_BODY in out


@pytest.mark.asyncio
async def test_startup_archive_shows_full_text(patched_server, bucket_mgr):
    await _make_archive(bucket_mgr)
    out = await patched_server.breath(startup=True)
    assert ARCHIVE_BODY in out


@pytest.mark.asyncio
async def test_raw_does_not_call_dehydrator(patched_server, bucket_mgr, mock_dehydrator):
    """原文直出:不该再走脱水,既省调用也避免二次摘要磨平细节。"""
    await _make_archive(bucket_mgr, content="需要脱水的长内容。" * 60)
    mock_dehydrator.dehydrate.reset_mock()
    out = await patched_server.breath(wake=True)
    for call in mock_dehydrator.dehydrate.call_args_list:
        assert "需要脱水的长内容" not in str(call), "归档内容被送去脱水了,应当原文直出"
    assert "需要脱水的长内容" in out


@pytest.mark.asyncio
async def test_explicit_mode_summary_still_respected(patched_server, bucket_mgr):
    """显式要 summary 就给单行,不能被新默认值覆盖。"""
    await _make_archive(bucket_mgr)
    out = await patched_server.breath(wake=True, mode="summary")
    assert ARCHIVE_BODY not in out
    assert "bucket_id" in out


@pytest.mark.asyncio
async def test_oversized_archive_is_truncated(patched_server, bucket_mgr, monkeypatch):
    """超长归档要截断并标注,不能吃光 token 预算。"""
    monkeypatch.setattr(server_mod, "BREATH_RAW_MAX_TOKENS", 200)
    await _make_archive(bucket_mgr, content="很长的一段回忆。" * 400)
    out = await patched_server.breath(wake=True)
    assert "此处截断" in out


@pytest.mark.asyncio
async def test_recent_held_stays_one_liner(patched_server, bucket_mgr):
    """「最近记下」保持线索行:本次只改归档,别把它一起放大。"""
    body = "随手记下的一件小事,不该整段展开。"
    await bucket_mgr.create(content=body, name="随手记", domain=["日常"], importance=4)
    out = await patched_server.breath(wake=True)
    if "[最近记下]" in out:
        assert body not in out


def test_truncate_helper_respects_token_budget():
    """截断按实测 token 切,不用固定字符比例(中英密度差好几倍)。"""
    from utils import count_tokens_approx
    for text in ["很长的内容。" * 900, "This is a long letter. " * 600, "今天她说 I am fine。" * 500]:
        out = server_mod._truncate_to_tokens(text, 1200)
        assert count_tokens_approx(out) <= 1200 + 80, count_tokens_approx(out)
    short = "很短的一句话。"
    assert server_mod._truncate_to_tokens(short, 1200) == short
