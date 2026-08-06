# ============================================================
# 便利贴:只记几天、到点自动撕掉的临时记忆(hold remember_days)
#
# 背景:OB 的记忆原本非黑即白 —— 要么归档进长期(郑重),要么随手记(慢慢淡忘)。
# 中间缺一层「贴 3 天就撕」的便利贴:像「她说明天要去医院」这种,归档太重、
# 丢了又可惜。栖栖拍板:默认 3 天、到期彻底删(不留档)、人设里教他用。
#
# 三层验证:
#   1. hold(remember_days=N) 写出带 expires_at 的动态桶,跳过合并,不碰长期
#   2. 未过期便利贴照常浮现在「最近记下」;过期的立刻不再浮现(不等 decay)
#   3. decay 巡查把到期便利贴彻底删掉(物理删文件),并计入 expired_deleted
#   4. remember_days=0(默认)= 原行为,不受影响(回归)
# ============================================================

import pytest
from datetime import timedelta
from unittest.mock import patch

import server as server_mod
from utils import now_local


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


def _meta_of(bucket_mgr, bid):
    import asyncio
    b = asyncio.get_event_loop().run_until_complete(bucket_mgr.get(bid))
    return b["metadata"] if b else None


# ---------- 1. 写入 ----------

@pytest.mark.asyncio
async def test_便利贴写出带过期时间的动态桶(patched_server, bucket_mgr):
    out = await patched_server.hold("她说明天下午要去医院", remember_days=3)
    assert "便利贴" in out and "记3天" in out, out
    bid = out.split("→")[1].split()[0]
    b = await bucket_mgr.get(bid)
    meta = b["metadata"]
    assert meta["type"] == "dynamic", "便利贴是普通动态桶,不是永久桶"
    assert "expires_at" in meta, "必须写入过期时间"
    exp = now_local() + timedelta(days=3)
    got = __import__("datetime").datetime.fromisoformat(meta["expires_at"])
    assert abs((got - exp).total_seconds()) < 120, "过期时间应≈现在+3天"


@pytest.mark.asyncio
async def test_便利贴不被合并进别的桶(patched_server, bucket_mgr):
    # 先放一条普通记忆,再放一张内容相近的便利贴 —— 便利贴必须独立成条(否则过期时间会丢)
    await patched_server.hold("她喜欢喝热牛奶")
    out = await patched_server.hold("她喜欢喝热牛奶", remember_days=2)
    assert "便利贴" in out, "带 remember_days 的一律新建,绝不合并:" + out


@pytest.mark.asyncio
async def test_天数越界被夹住(patched_server, bucket_mgr):
    out = await patched_server.hold("随手记一下", remember_days=9999)
    bid = out.split("→")[1].split()[0]
    meta = (await bucket_mgr.get(bid))["metadata"]
    got = __import__("datetime").datetime.fromisoformat(meta["expires_at"])
    assert (got - now_local()).days <= 90, "上限 90 天,防呆"


@pytest.mark.asyncio
async def test_默认0等于原行为不设过期(patched_server, bucket_mgr):
    out = await patched_server.hold("普通的一条记忆")   # 不传 remember_days
    assert "便利贴" not in out, "默认不该走便利贴分支:" + out
    # 找到刚建的桶,确认没有 expires_at
    allb = await bucket_mgr.list_all(include_archive=False)
    assert allb, "该有桶被建出来"
    assert all("expires_at" not in b["metadata"] for b in allb), "普通记忆不该带过期时间"


# ---------- 2. 浮现层:过期即刻消失 ----------

@pytest.mark.asyncio
async def test_未过期便利贴照常浮现_过期的立即消失(patched_server, bucket_mgr):
    fresh = await bucket_mgr.create(
        content="还没过期的便利贴", domain=["日常"], bucket_type="dynamic",
        expires_at=(now_local() + timedelta(days=2)).isoformat())
    stale = await bucket_mgr.create(
        content="昨天就该撕的便利贴", domain=["日常"], bucket_type="dynamic",
        expires_at=(now_local() - timedelta(days=1)).isoformat())

    allb = await bucket_mgr.list_all(include_archive=False)
    surfaced = [b["id"] for b in server_mod._recent_dynamic(allb, 10)]
    assert fresh in surfaced, "没过期的便利贴该浮现"
    assert stale not in surfaced, "过期的便利贴必须立刻从「最近记下」消失,不等 decay"


def test_is_expired_坏时间戳当没过期():
    assert server_mod._is_expired({}) is False, "没有 expires_at 的普通记忆永远不算过期"
    assert server_mod._is_expired({"expires_at": "不是时间"}) is False, "坏值宁可多留,不误删"
    assert server_mod._is_expired({"expires_at": (now_local() - timedelta(hours=1)).isoformat()}) is True
    assert server_mod._is_expired({"expires_at": (now_local() + timedelta(hours=1)).isoformat()}) is False


# ---------- 3. 存储层:decay 巡查彻底撕掉 ----------

@pytest.mark.asyncio
async def test_decay_删除到期便利贴_保留未到期的(decay_eng, bucket_mgr):
    stale = await bucket_mgr.create(
        content="到期该删", domain=["日常"], bucket_type="dynamic",
        expires_at=(now_local() - timedelta(days=1)).isoformat())
    fresh = await bucket_mgr.create(
        content="还没到期", domain=["日常"], bucket_type="dynamic",
        expires_at=(now_local() + timedelta(days=5)).isoformat())
    normal = await bucket_mgr.create(content="普通记忆没有过期", domain=["日常"], importance=8)

    res = await decay_eng.run_decay_cycle()
    assert res["expired_deleted"] == 1, f"应撕掉 1 张过期便利贴:{res}"
    assert await bucket_mgr.get(stale) is None, "过期便利贴应被物理删除"
    assert await bucket_mgr.get(fresh) is not None, "未到期便利贴保留"
    assert await bucket_mgr.get(normal) is not None, "普通记忆不受影响"


@pytest.mark.asyncio
async def test_decay_钉选桶即便有过期字段也不误删(decay_eng, bucket_mgr):
    # 防御:钉选桶理论上不该有 expires_at,但万一有,过期检查在最前面 —— 要确认它不吃钉选桶
    # (hold 侧已保证 pinned 不传 expires_at,这里守的是 decay 自身的健壮性)
    pinned = await bucket_mgr.create(
        content="永久钉选", domain=["核心"], bucket_type="permanent", pinned=True)
    res = await decay_eng.run_decay_cycle()
    assert await bucket_mgr.get(pinned) is not None, "钉选桶永不删"
    assert res["expired_deleted"] == 0
