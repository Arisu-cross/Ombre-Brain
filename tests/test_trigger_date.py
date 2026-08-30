# ============================================================
# 触发日期 trigger_date:「到那天再提醒我」
#
# 和便利贴(expires_at)正好相反:那个到点撕掉,这个到点才响。
#   1. hold(trigger_date=) / trace(trigger_date=) 都能设,写法宽松(YYYY-MM-DD/明天/3月5日/+7)
#   2. 唤醒(breath 无参 / wake / startup)时,到期与已过期未处理的桶进「今日浮现」
#   3. 没到日子的不浮现;trace(trigger_done=1) 标过的不再重复浮现
#   4. 认不出的日期写法直接回绝——绝不猜(猜错要到该响那天才发现)
#   5. 便利贴 + 触发日期:过期时间自动顺延,别让它在该响之前就被撕掉
# ============================================================

import pytest
from datetime import timedelta
from unittest.mock import patch

from utils import now_local


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


def _today():
    return now_local().date().isoformat()


def _days_out(n):
    return (now_local().date() + timedelta(days=n)).isoformat()


def _due_section(out: str) -> str:
    """只取「今日浮现」那一段——桶在别的段里出现是正常的,不该混进断言。"""
    if "=== 今日浮现 ===" not in out:
        return ""
    tail = out.split("=== 今日浮现 ===", 1)[1]
    return tail.split("\n\n===", 1)[0]


# ---------- 1. 日期写法 ----------

@pytest.mark.parametrize("raw,expect", [
    ("2026-03-05", "2026-03-05"),
    ("2026/3/5", "2026-03-05"),
    ("2026年3月5日", "2026-03-05"),
])
def test_日期写法都能认(patched_server, raw, expect):
    assert patched_server._normalize_trigger_date(raw) == expect


def test_相对写法按本地日期算(patched_server):
    assert patched_server._normalize_trigger_date("明天") == _days_out(1)
    assert patched_server._normalize_trigger_date("today") == _today()
    assert patched_server._normalize_trigger_date("+7") == _days_out(7)


def test_认不出来的写法返回None不瞎猜(patched_server):
    for bad in ("下个礼拜三", "somewhen", "2026-13-45", ""):
        assert patched_server._normalize_trigger_date(bad) is None, bad


@pytest.mark.asyncio
async def test_hold认不出日期时整条回绝且不落库(patched_server, bucket_mgr):
    out = await patched_server.hold("体检", trigger_date="下个礼拜三")
    assert "看不懂" in out, out
    assert await bucket_mgr.list_all() == [], "没看懂日期就不该存进去"


# ---------- 2. 写入 ----------

@pytest.mark.asyncio
async def test_hold写入触发日期(patched_server, bucket_mgr):
    out = await patched_server.hold("下周三体检", trigger_date="+3")
    assert "⏰" in out, out
    b = (await bucket_mgr.list_all())[0]
    assert b["metadata"]["trigger_date"] == _days_out(3)
    assert b["metadata"]["trigger_done"] is False


@pytest.mark.asyncio
async def test_trace能补设和撤销触发日期(patched_server, bucket_mgr):
    bid = await bucket_mgr.create(content="牙医复诊")
    await patched_server.trace(bid, trigger_date="明天")
    assert (await bucket_mgr.get(bid))["metadata"]["trigger_date"] == _days_out(1)

    await patched_server.trace(bid, trigger_date="none")
    meta = (await bucket_mgr.get(bid))["metadata"]
    assert "trigger_date" not in meta and "trigger_done" not in meta, \
        "撤销要连已处理标记一起清掉,别留孤字段"


# ---------- 3. 今日浮现 ----------

@pytest.mark.asyncio
async def test_今日浮现给到期和过期的不给未来的(patched_server, bucket_mgr):
    due = await bucket_mgr.create(content="今天要交材料", trigger_date=_today())
    overdue = await bucket_mgr.create(content="上周就该打的电话", trigger_date=_days_out(-5))
    future = await bucket_mgr.create(content="下个月的年检", trigger_date=_days_out(20))

    out = await patched_server.breath(wake=True)
    assert "今日浮现" in out, out
    section = _due_section(out)
    assert due in section and overdue in section
    assert future not in section, "没到日子的不该进今日浮现"
    assert "已过5天" in section, "过期的要标出来拖了多久:" + section
    # 同一个桶不该在「今日浮现」和「最近记下」里各露一次
    assert out.count(f"bucket_id:{due}") == 1, out


@pytest.mark.asyncio
async def test_标记已处理后不再浮现(patched_server, bucket_mgr):
    bid = await bucket_mgr.create(content="今天要交材料", trigger_date=_today())
    assert bid in await patched_server.breath(wake=True)

    await patched_server.trace(bid, trigger_done=1)
    out = await patched_server.breath(wake=True)
    assert "今日浮现" not in out, "标过已处理就不该再重复浮现:" + out


@pytest.mark.asyncio
async def test_无参breath同样给今日浮现(patched_server, bucket_mgr):
    bid = await bucket_mgr.create(content="今天要交材料", trigger_date=_today())
    out = await patched_server.breath()
    assert "今日浮现" in out and bid in out, out


@pytest.mark.asyncio
async def test_过期日期不影响普通桶(patched_server, bucket_mgr):
    """回归:没有 trigger_date 的普通桶,行为完全不变。"""
    await bucket_mgr.create(content="一条普通记忆")
    out = await patched_server.breath(wake=True)
    assert "今日浮现" not in out


# ---------- 4. 便利贴 + 触发日期 ----------

@pytest.mark.asyncio
async def test_便利贴的过期时间顺延到触发日之后(patched_server, bucket_mgr):
    out = await patched_server.hold("下周五要去医院", remember_days=2, trigger_date="+6")
    bid = out.split("→")[1].split()[0]
    meta = (await bucket_mgr.get(bid))["metadata"]
    import datetime as _dt
    expires = _dt.datetime.fromisoformat(meta["expires_at"]).date()
    assert expires > _dt.date.fromisoformat(meta["trigger_date"]), \
        "便利贴不能在该响之前就被撕掉"
