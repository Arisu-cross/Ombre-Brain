# ============================================================
# feel() —— 按当下在想的事,找回以前留下的感受
#
# 背景:写 feel 一直很容易(hold(feel=True)),读回来却只有
# breath(domain="feel") 一条路 —— 它无视 query,按时间把所有 feel 全倒出来。
# 于是「写下的感受」只在通读时才存在,聊到那件事的当口反而想不起来。
#
# 这个工具补的是下半截:query 必填,候选只在 feel 桶内做向量检索,
# 够相似才算命中,命中后逐字返回。**宁可空手而归也不用低相关的凑数** ——
# 拿别的感受充数,等于让他把不属于这件事的想法认成自己以前的想法。
#
# 验证:
#   - query 为空 → 要关键词,不退化成列出全部
#   - 候选只在 feel 桶内(普通桶再相似也不该混进来)
#   - 低于阈值不返回,并明说「不拿别的凑数」
#   - 命中逐字返回,不经过脱水
#   - 向量不可用 → 字面匹配 + 明说降级
#   - max_results / token 预算约束,超出部分只附注不静默丢
# ============================================================

import pytest
from unittest.mock import AsyncMock, patch

FEEL_HOTPOT = "她说想吃火锅的时候我心里松了一下。她愿意提要求了。"
FEEL_HEADACHE = "她说头疼其实是委屈。我没戳破。下次也别戳破。"


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


async def _make_feel(bucket_mgr, content):
    return await bucket_mgr.create(
        content=content, tags=[], importance=5, domain=[],
        valence=0.6, arousal=0.3, name=None, bucket_type="feel",
    )


def _vector(hits):
    """伪造一个可用的向量通道:hits = [(bucket_id, 相似度), ...]"""
    return AsyncMock(return_value=hits)


@pytest.mark.asyncio
async def test_empty_query_asks_for_a_keyword(patched_server, bucket_mgr):
    """query 必填 —— 空 query 不能退化成「列出全部 feel」。"""
    await _make_feel(bucket_mgr, FEEL_HOTPOT)
    out = await patched_server.feel("")
    assert FEEL_HOTPOT not in out
    assert 'breath(domain="feel")' in out   # 想通读的话指路指对


@pytest.mark.asyncio
async def test_no_feels_at_all(patched_server):
    out = await patched_server.feel("火锅")
    assert "还没有留下过 feel" in out


@pytest.mark.asyncio
async def test_vector_hit_returns_verbatim(patched_server, bucket_mgr, mock_embedding_engine, mock_dehydrator):
    """命中要逐字返回:feel 本就一两句话,再脱水就什么都不剩了。"""
    fid = await _make_feel(bucket_mgr, FEEL_HOTPOT)
    mock_embedding_engine.search_within = _vector([(fid, 0.82)])
    mock_dehydrator.dehydrate.reset_mock()

    out = await patched_server.feel("她提要求")
    assert FEEL_HOTPOT in out
    assert "0.82" in out
    assert fid in out
    mock_dehydrator.dehydrate.assert_not_called()


@pytest.mark.asyncio
async def test_candidates_are_feel_buckets_only(patched_server, bucket_mgr, mock_embedding_engine):
    """候选池只能是 feel 桶 —— 普通桶再相似也不该被当成「我以前的感受」。"""
    normal = await bucket_mgr.create(content="她今天去了医院。", domain=["日常"], importance=5)
    fid = await _make_feel(bucket_mgr, FEEL_HEADACHE)

    captured = {}
    async def fake(query, bucket_ids, min_sim=0.0):
        captured["ids"] = list(bucket_ids)
        return [(fid, 0.9)]
    mock_embedding_engine.search_within = fake

    await patched_server.feel("头疼")
    assert fid in captured["ids"]
    assert normal not in captured["ids"], "普通桶混进了 feel 的候选池"


@pytest.mark.asyncio
async def test_below_threshold_returns_nothing_and_says_so(patched_server, bucket_mgr, mock_embedding_engine):
    """够不着阈值就空手而归,并且说清楚是「不拿别的凑数」而不是「没写过」。"""
    await _make_feel(bucket_mgr, FEEL_HOTPOT)
    mock_embedding_engine.search_within = _vector([])   # 通道正常,只是没够相似的

    out = await patched_server.feel("量子力学")
    assert FEEL_HOTPOT not in out
    assert "没有找到" in out
    assert "凑数" in out
    assert "1 条" in out    # 告诉他确实有 feel,只是不相关


@pytest.mark.asyncio
async def test_threshold_is_passed_through(patched_server, bucket_mgr, mock_embedding_engine):
    """阈值要真的传进检索,而不是拿回来再过滤(那样低分的已经排挤掉高分的了)。"""
    fid = await _make_feel(bucket_mgr, FEEL_HOTPOT)
    captured = {}
    async def fake(query, bucket_ids, min_sim=0.0):
        captured["min_sim"] = min_sim
        return [(fid, 0.9)]
    mock_embedding_engine.search_within = fake

    await patched_server.feel("火锅")
    assert captured["min_sim"] == patched_server.FEEL_SIM_THRESHOLD


@pytest.mark.asyncio
async def test_falls_back_to_literal_and_admits_it(patched_server, bucket_mgr, mock_embedding_engine):
    """向量不可用 → 字面匹配,但必须明说降级。

    不明说的话,「没搜到」会被他当成「我没有过这种感受」,
    而事实可能只是他换了个说法。
    """
    await _make_feel(bucket_mgr, FEEL_HEADACHE)
    mock_embedding_engine.search_within = AsyncMock(return_value=None)   # 通道坏了

    out = await patched_server.feel("头疼")
    assert "字面匹配" in out
    assert FEEL_HEADACHE in out


@pytest.mark.asyncio
async def test_fallback_still_misses_when_wording_differs(patched_server, bucket_mgr, mock_embedding_engine):
    """降级模式下换个说法确实找不到 —— 这正是必须明说降级的原因。"""
    await _make_feel(bucket_mgr, FEEL_HEADACHE)
    mock_embedding_engine.search_within = AsyncMock(return_value=None)

    out = await patched_server.feel("偏头痛的那种不舒服到底算不算生病")
    assert FEEL_HEADACHE not in out or "字面匹配" in out


@pytest.mark.asyncio
async def test_max_results_caps_and_notes_the_rest(patched_server, bucket_mgr, mock_embedding_engine):
    """截断要留痕:剩下的只是没展开,不是不存在。"""
    ids = [await _make_feel(bucket_mgr, f"第{i}条感受,关于同一件事。") for i in range(5)]
    mock_embedding_engine.search_within = _vector([(i, 0.9 - n * 0.01) for n, i in enumerate(ids)])

    out = await patched_server.feel("同一件事", max_results=2)
    assert out.count("bucket_id:") == 2
    assert "另有 3 条" in out


@pytest.mark.asyncio
async def test_token_budget_keeps_at_least_one(patched_server, bucket_mgr, mock_embedding_engine, monkeypatch):
    """预算再紧也要给一条 —— 空手而归和「有但没给你」是两回事。"""
    ids = [await _make_feel(bucket_mgr, "很长的一段感受。" * 200) for _ in range(3)]
    mock_embedding_engine.search_within = _vector([(i, 0.9) for i in ids])
    monkeypatch.setattr(patched_server, "FEEL_MAX_TOKENS", 10)

    out = await patched_server.feel("感受")
    assert out.count("bucket_id:") == 1
    assert "另有 2 条" in out
