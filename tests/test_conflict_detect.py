# ============================================================
# 存入时的矛盾检测:新内容和旧记忆在同一件事上对不上
#
#   1. 相似旧桶 + 日期对不上 → 返回里附冲突警告,指明旧桶和具体矛盾点
#   2. **不自动改任何东西**:旧桶原样,新内容独立成桶(不被揉进去)
#   3. 没有可对照的硬信号(日期/数字)就不问 LLM —— 那正是误报的来源
#   4. 核对失败/API 不可用 = 当作没冲突,绝不让附加提醒把存入弄失败
#   5. 关掉开关(CONFLICT_CHECK_N=0)完全不走这条路(回归)
# ============================================================

import pytest
from unittest.mock import patch, AsyncMock


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


OLD = "体检约在 2026-03-05 上午,带上身份证"
NEW = "体检约在 2026-03-08 上午,带上身份证"


@pytest.mark.asyncio
async def test_日期对不上时给出冲突警告(patched_server, bucket_mgr, mock_dehydrator):
    old_id = await bucket_mgr.create(content=OLD, name="体检安排")
    mock_dehydrator.check_conflict = AsyncMock(
        return_value=["旧:2026-03-05 / 新:2026-03-08 —— 体检日期对不上"]
    )

    out = await patched_server.hold(NEW)
    assert "⚠️" in out and "对不上" in out, out
    assert old_id in out, "要指明是哪个旧桶:" + out
    assert "没有自动改任何东西" in out


@pytest.mark.asyncio
async def test_冲突时不合并也不改旧桶(patched_server, bucket_mgr, mock_dehydrator):
    old_id = await bucket_mgr.create(content=OLD, name="体检安排")
    mock_dehydrator.check_conflict = AsyncMock(return_value=["日期对不上"])

    await patched_server.hold(NEW)

    old = await bucket_mgr.get(old_id)
    assert old["content"] == OLD, "旧桶正文必须原样不动"
    assert "合并" not in (mock_dehydrator.merge.call_args_list and "x" or ""), ""
    mock_dehydrator.merge.assert_not_called()

    ids = [b["id"] for b in await bucket_mgr.list_all()]
    assert len(ids) == 2, "新内容该独立成桶,两个说法都留着"


@pytest.mark.asyncio
async def test_没有硬信号就不问LLM(patched_server, bucket_mgr, mock_dehydrator):
    """没有日期/数字可对照时,LLM 只会去揣摩语气 —— 那正是误报的来源。"""
    await bucket_mgr.create(content="她说她喜欢阳台上那盆薄荷", name="薄荷")
    mock_dehydrator.check_conflict = AsyncMock(return_value=["瞎报一个"])

    out = await patched_server.hold("她说她喜欢阳台上那盆薄荷")
    mock_dehydrator.check_conflict.assert_not_called()
    assert "⚠️" not in out, out


@pytest.mark.asyncio
async def test_核对失败当作没冲突不影响存入(patched_server, bucket_mgr, mock_dehydrator):
    await bucket_mgr.create(content=OLD, name="体检安排")
    mock_dehydrator.check_conflict = AsyncMock(side_effect=RuntimeError("API 挂了"))

    out = await patched_server.hold(NEW)
    assert "⚠️" not in out, "核对不了就该安静地当没冲突:" + out
    assert "新建" in out or "合并" in out, out


@pytest.mark.asyncio
async def test_关掉开关就完全不检测(patched_server, bucket_mgr, mock_dehydrator):
    await bucket_mgr.create(content=OLD, name="体检安排")
    mock_dehydrator.check_conflict = AsyncMock(return_value=["日期对不上"])
    with patch.object(patched_server, "CONFLICT_CHECK_N", 0):
        out = await patched_server.hold(NEW)
    mock_dehydrator.check_conflict.assert_not_called()
    assert "⚠️" not in out, out


@pytest.mark.asyncio
async def test_不相干的旧桶不参与比对(patched_server, bucket_mgr, mock_dehydrator):
    """两件不相干的事各有各的日期,不该被凑成一对「矛盾」。"""
    await bucket_mgr.create(content="2026-01-09 去看了海", name="看海")
    mock_dehydrator.check_conflict = AsyncMock(return_value=["瞎报一个"])

    out = await patched_server.hold("2026-07-22 换了新的洗衣机")
    assert "⚠️" not in out, out


# ---------- 本地预筛单测 ----------

def test_硬信号预筛(patched_server):
    f = patched_server._worth_llm_check
    assert f("3月5日体检", "3月8日体检"), "日期不同 → 值得核对"
    assert not f("3月5日体检", "3月5日体检"), "日期相同是复述,不是矛盾"
    assert not f("她喜欢薄荷", "她喜欢薄荷"), "两边都没硬信号 → 不问 LLM"
    assert not f("她喜欢薄荷", "3月5日体检"), "一边没硬信号 → 不问 LLM"
    assert f("花了 200 块", "花了 500 块"), "带单位的数字不同 → 值得核对"
