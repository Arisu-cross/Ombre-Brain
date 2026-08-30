# ============================================================
# 心境共鸣检索:只给情绪坐标、不给关键词,按坐标距离排序返回
#
# 背景:情感共鸣一直是四维评分里的一维,但 search() 没有 query 就直接返回空 ——
# 情绪坐标只能当配料,没法单独当主菜。而「现在这个心情让我想起过什么」
# 本来就是不带关键词的问法。
#
#   1. breath(valence=/arousal=) 无 query → 按距离排序,近的在前
#   2. 只给一个坐标 → 只在那一个轴上比
#   3. 可与 domain / date_from / date_to / importance_min 组合
#   4. 钉选桶与 feel 不进这条通道(各有各的入口)
#   5. 有 query 时行为不变(回归:仍走四维评分)
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


async def _seed(bucket_mgr):
    """三个情绪坐标拉开的桶。"""
    happy = await bucket_mgr.create(content="领证那天下午的太阳", valence=0.95, arousal=0.8)
    calm = await bucket_mgr.create(content="周日下午在阳台看书", valence=0.6, arousal=0.15)
    sad = await bucket_mgr.create(content="搬走那天空掉的房间", valence=0.1, arousal=0.6)
    return happy, calm, sad


# ---------- 1. 排序 ----------

@pytest.mark.asyncio
async def test_按情绪坐标距离排序(bucket_mgr):
    happy, calm, sad = await _seed(bucket_mgr)
    out = await bucket_mgr.search_by_mood(query_valence=0.05, query_arousal=0.65)
    assert [b["id"] for b in out][0] == sad, "离得最近的该排第一"
    assert out[0]["mood_distance"] < out[-1]["mood_distance"]
    assert out[0]["score"] > out[-1]["score"]


@pytest.mark.asyncio
async def test_只给一个坐标只比那一个轴(bucket_mgr):
    happy, calm, sad = await _seed(bucket_mgr)
    out = await bucket_mgr.search_by_mood(query_arousal=0.15)
    assert out[0]["id"] == calm, "只给唤醒度就只比唤醒度,不该被效价带偏"


@pytest.mark.asyncio
async def test_两个坐标都不给返回空(bucket_mgr):
    await _seed(bucket_mgr)
    assert await bucket_mgr.search_by_mood() == []


# ---------- 2. 谁不进这条通道 ----------

@pytest.mark.asyncio
async def test_钉选桶和feel不进心境通道(bucket_mgr):
    pin = await bucket_mgr.create(content="真名绝对不喊", valence=0.5, arousal=0.3, pinned=True)
    fl = await bucket_mgr.create(content="我今天有点想她", valence=0.5, arousal=0.3,
                                 bucket_type="feel")
    normal = await bucket_mgr.create(content="普通记忆", valence=0.5, arousal=0.3)
    ids = [b["id"] for b in await bucket_mgr.search_by_mood(query_valence=0.5, query_arousal=0.3)]
    assert normal in ids
    assert pin not in ids, "钉选桶每次都在核心准则里,再按心境排一次只会占满名额"
    assert fl not in ids, "feel 有自己的入口"


# ---------- 3. breath 层 ----------

@pytest.mark.asyncio
async def test_breath无query只给坐标走心境模式(patched_server, bucket_mgr):
    happy, calm, sad = await _seed(bucket_mgr)
    out = await patched_server.breath(valence=0.05, arousal=0.65, mode="summary")
    assert "心境共鸣" in out, out
    assert out.index(sad) < out.index(happy), "近的该排在前面"
    assert "距离" in out


@pytest.mark.asyncio
async def test_心境模式可与重要度和主题域组合(patched_server, bucket_mgr):
    low = await bucket_mgr.create(content="随手记的小事", valence=0.1, arousal=0.6, importance=2)
    high = await bucket_mgr.create(content="很重要的难过事", valence=0.15, arousal=0.6,
                                   importance=9, domain=["情感"])
    out = await patched_server.breath(valence=0.1, arousal=0.6, importance_min=8, mode="summary")
    assert high in out and low not in out, "importance_min 该当过滤器用:" + out

    out2 = await patched_server.breath(valence=0.1, arousal=0.6, domain="情感", mode="summary")
    assert high in out2 and low not in out2, "domain 也要能组合:" + out2


@pytest.mark.asyncio
async def test_没有接近的记忆时明说(patched_server, bucket_mgr):
    out = await patched_server.breath(valence=0.5, arousal=0.5, mode="summary")
    assert "没有情绪坐标接近的记忆" in out


@pytest.mark.asyncio
async def test_有query时仍走老的四维评分(patched_server, bucket_mgr):
    """回归:带关键词时情绪坐标还是那一维配料,不该被心境模式截胡。"""
    await _seed(bucket_mgr)
    out = await patched_server.breath(query="阳台", valence=0.95, arousal=0.8)
    assert "心境共鸣" not in out, "有 query 就不该走心境模式:" + out
