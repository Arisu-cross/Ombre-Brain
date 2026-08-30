# ============================================================
# 自动消化:把长期没被想起的低重要度碎片沉淀成摘要
#
#   1. 候选口径:低重要度 + 长期闲置 + 非钉选;钉选/永久/feel/便利贴/
#      带未处理触发日期的桶一律不碰
#   2. 演习模式(默认):只出计划,一个字都不改
#   3. execute:每组提炼成一条沉淀桶,原桶**归档不删**,互相写上关联
#   4. 提炼失败 → 整组原样保留,绝不拿拼接原文冒充摘要落库
#   5. 整理动作不刷新原桶的 last_active(否则「很久没被想起」这件事会被抹掉)
# ============================================================

import pytest
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from utils import now_local


@pytest.fixture
def digest_eng(test_config, bucket_mgr, mock_embedding_engine, mock_dehydrator):
    from digest_engine import DigestEngine
    eng = DigestEngine(test_config, bucket_mgr, mock_embedding_engine, mock_dehydrator)
    eng.min_idle_days = 60
    eng.importance_max = 4
    eng.min_group = 2
    eng.sim_threshold = 0.3      # 退化通道按标签重合度,门槛调低好造数据
    return eng


async def _stale(bucket_mgr, content, days=200, importance=3, tags=None, **kw):
    bid = await bucket_mgr.create(
        content=content, importance=importance, tags=tags or ["旧事", "杂事"],
        domain=["日常"], **kw
    )
    import frontmatter
    path = bucket_mgr._find_bucket_file(bid)
    post = frontmatter.load(path)
    post["last_active"] = (now_local() - timedelta(days=days)).isoformat()
    with open(path, "w", encoding="utf-8") as f:
        f.write(frontmatter.dumps(post))
    return bid


# ---------- 1. 候选口径 ----------

@pytest.mark.asyncio
async def test_只挑低重要度且长期闲置的(digest_eng, bucket_mgr):
    old_low = await _stale(bucket_mgr, "很久以前买的伞", importance=3)
    old_high = await _stale(bucket_mgr, "很久以前那件大事", importance=9)
    fresh_low = await bucket_mgr.create(content="今天买的伞", importance=3)

    ids = [b["id"] for b in await digest_eng.candidates()]
    assert old_low in ids
    assert old_high not in ids, "重要的不消化"
    assert fresh_low not in ids, "最近还在用的不消化"


@pytest.mark.asyncio
async def test_钉选便利贴和未处理的触发日期都不碰(digest_eng, bucket_mgr):
    pin = await _stale(bucket_mgr, "真名绝对不喊", pinned=True)
    note = await _stale(bucket_mgr, "临时便利贴", expires_at="2099-01-01T00:00:00")
    trig = await _stale(bucket_mgr, "该打的电话", trigger_date="2020-01-01")
    plain = await _stale(bucket_mgr, "很久以前买的伞")

    ids = [b["id"] for b in await digest_eng.candidates()]
    assert plain in ids
    for protected in (pin, note, trig):
        assert protected not in ids


# ---------- 2. 演习 ----------

@pytest.mark.asyncio
async def test_演习只出计划不动数据(digest_eng, bucket_mgr, mock_dehydrator):
    a = await _stale(bucket_mgr, "旧事一:那年冬天的暖气", tags=["冬天", "暖气"])
    b = await _stale(bucket_mgr, "旧事二:那年冬天的水管", tags=["冬天", "暖气"])

    plan = await digest_eng.plan()
    assert plan["candidates"] == 2
    assert len(plan["groups"]) == 1
    assert {x["id"] for x in plan["groups"][0]["buckets"]} == {a, b}
    assert "没有改动" in plan["note"]

    mock_dehydrator.sediment.assert_not_called()
    assert len(await bucket_mgr.list_all(include_archive=True)) == 2, "演习不该多出或少掉桶"


@pytest.mark.asyncio
async def test_候选不够一组时不硬凑(digest_eng, bucket_mgr):
    await _stale(bucket_mgr, "孤零零一条旧事")
    plan = await digest_eng.plan()
    assert plan["groups"] == []


# ---------- 3. 实际整理 ----------

@pytest.mark.asyncio
async def test_整理后原桶归档不删并写上关联(digest_eng, bucket_mgr, mock_dehydrator):
    a = await _stale(bucket_mgr, "旧事一:那年冬天的暖气", tags=["冬天", "暖气"])
    b = await _stale(bucket_mgr, "旧事二:那年冬天的水管", tags=["冬天", "暖气"])
    mock_dehydrator.sediment = AsyncMock(return_value={
        "name": "那年冬天的屋子", "summary": "暖气和水管都出过问题。", "tags": ["冬天"],
    })

    result = await digest_eng.execute()
    assert result["digested"] == 1
    sed_id = result["groups"][0]["sediment_id"]

    sed = await bucket_mgr.get(sed_id)
    assert sed["metadata"]["sediment"] is True
    assert set(sed["metadata"]["sediment_sources"]) == {a, b}
    assert set(sed["metadata"]["related"]) == {a, b}
    assert "暖气和水管" in sed["content"]

    for src in (a, b):
        got = await bucket_mgr.get(src)
        assert got is not None, "原桶必须还在(只归档,不删)"
        assert got["metadata"]["type"] == "archived"
        assert got["metadata"]["sediment_of"] == sed_id


@pytest.mark.asyncio
async def test_整理不刷新原桶的活跃时间(digest_eng, bucket_mgr, mock_dehydrator):
    """刷了的话「它们很久没被想起」这件事就被整理动作自己抹掉了。"""
    a = await _stale(bucket_mgr, "旧事一:那年冬天的暖气", tags=["冬天"])
    b = await _stale(bucket_mgr, "旧事二:那年冬天的水管", tags=["冬天"])
    before = (await bucket_mgr.get(a))["metadata"]["last_active"]
    mock_dehydrator.sediment = AsyncMock(return_value={
        "name": "那年冬天", "summary": "都出过问题。", "tags": [],
    })

    await digest_eng.execute()
    assert (await bucket_mgr.get(a))["metadata"]["last_active"] == before


@pytest.mark.asyncio
async def test_提炼失败整组原样保留(digest_eng, bucket_mgr, mock_dehydrator):
    a = await _stale(bucket_mgr, "旧事一", tags=["冬天"])
    b = await _stale(bucket_mgr, "旧事二", tags=["冬天"])
    mock_dehydrator.sediment = AsyncMock(return_value=None)

    result = await digest_eng.execute()
    assert result["digested"] == 0
    assert result["groups"][0]["ok"] is False
    for src in (a, b):
        got = await bucket_mgr.get(src)
        assert got["metadata"]["type"] == "dynamic", "失败就该原样躺着"
    assert len(await bucket_mgr.list_all(include_archive=True)) == 2, "不该落一个假摘要下来"


# ---------- 4. 工具层 ----------

@pytest.mark.asyncio
async def test_digest工具默认演习(bucket_mgr, decay_eng, mock_dehydrator,
                                  mock_embedding_engine, digest_eng):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine), \
         patch.object(server, "digest_engine", digest_eng):
        await _stale(bucket_mgr, "旧事一", tags=["冬天"])
        await _stale(bucket_mgr, "旧事二", tags=["冬天"])
        out = await server.digest()
        assert "演习" in out and "没有改动任何记忆" in out, out
        mock_dehydrator.sediment.assert_not_called()
