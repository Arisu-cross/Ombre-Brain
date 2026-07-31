# ============================================================
# archive_session 按天合并
#
# 背景:原本每次归档都新建一个桶,一天归三次就碎成三份 —— 浮现时占掉「最近 N 条」
# 三个名额,读起来也不连贯。改为同一天合并进「会话归档 YYYY-MM-DD」一个档案,
# 当天再次归档按时刻追加一节;跨天才开新档。
#
# 验证:
#   - 当天第二次归档追加进同一个桶,不新建
#   - **追加不丢已有内容**(第一次写的东西还在)
#   - archived_at 刷新到最后一次(排序与「上次归档」边界才对)
#   - 追加后不重复走归档移动(仍在归档区、桶数不变)
#   - 关掉开关退回旧行为(每次新建)
#   - 唤醒浮现里当天档案只占一条名额
# ============================================================

import pytest
from unittest.mock import patch

import server as server_mod


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


async def _archived(bucket_mgr):
    return [b for b in await bucket_mgr.list_all(include_archive=True)
            if b["metadata"].get("type") == "archived"]


@pytest.mark.asyncio
async def test_same_day_appends_not_creates(patched_server, bucket_mgr):
    """当天第二次归档应追加进同一个档案,而不是新建一个。"""
    await patched_server.archive_session(summary="早上她说头疼", mood="担心")
    await patched_server.archive_session(summary="下午一起看了电影", mood="放松")
    arch = await _archived(bucket_mgr)
    assert len(arch) == 1, f"应只有 1 个当天档案,实际 {len(arch)} 个"


@pytest.mark.asyncio
async def test_append_preserves_earlier_content(patched_server, bucket_mgr):
    """追加绝不能覆盖掉当天早先写的内容 —— 这是他的记忆。"""
    await patched_server.archive_session(summary="早上她说头疼", highlights="她第一次主动说累", mood="担心")
    await patched_server.archive_session(summary="下午一起看了电影", mood="放松")
    body = (await _archived(bucket_mgr))[0]["content"]
    for must in ("早上她说头疼", "她第一次主动说累", "担心", "下午一起看了电影", "放松"):
        assert must in body, f"追加后丢了:{must}\n---\n{body}"


@pytest.mark.asyncio
async def test_archived_at_refreshes_on_append(patched_server, bucket_mgr):
    """archived_at 要刷到最后一次归档,否则排序和「上次归档」边界都停在首次。"""
    await patched_server.archive_session(summary="第一次")
    first = (await _archived(bucket_mgr))[0]["metadata"].get("archived_at")
    await patched_server.archive_session(summary="第二次")
    second = (await _archived(bucket_mgr))[0]["metadata"].get("archived_at")
    assert first and second and second >= first, f"archived_at 没刷新: {first} → {second}"


@pytest.mark.asyncio
async def test_append_returns_merged_status(patched_server, bucket_mgr):
    """追加要有成功标记 🗄️ —— shim 的换窗安全阀靠它判断归档成功。"""
    await patched_server.archive_session(summary="第一次")
    out = await patched_server.archive_session(summary="第二次")
    assert "🗄️" in out, f"缺少成功标记,安全阀会误判为归档失败:{out}"
    assert "追加" in out


@pytest.mark.asyncio
async def test_merge_off_falls_back_to_per_call_bucket(patched_server, bucket_mgr, monkeypatch):
    """关掉开关就退回旧行为:每次归档一个新桶。"""
    monkeypatch.setattr(server_mod, "ARCHIVE_MERGE_BY_DAY", False)
    await patched_server.archive_session(summary="第一次")
    await patched_server.archive_session(summary="第二次")
    assert len(await _archived(bucket_mgr)) == 2


@pytest.mark.asyncio
async def test_wake_surfaces_day_bucket_as_one(patched_server, bucket_mgr):
    """唤醒浮现时,当天多次归档只占一条名额,且两段内容都读得到。"""
    await patched_server.archive_session(summary="早上她说头疼")
    await patched_server.archive_session(summary="下午一起看了电影")
    out = await patched_server.breath(wake=True)
    assert out.count("🗄️ [归档]") == 1
    assert "早上她说头疼" in out and "下午一起看了电影" in out
