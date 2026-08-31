# ============================================================
# 开机自跑一次的存量维护:补情绪坐标 + 补关联
#
# 栖栖只有手机、进不了容器,所以这两件事由服务端自己做一次。
#   1. 补情绪坐标:只补停在默认值 (V0.5/A0.3) 的;feel 桶不碰
#   2. 补关联:只补没有 related 的未封存桶;封存桶不做关联对象
#   3. **一辈子只跑一次**:标记文件在,第二次启动直接跳过
#   4. 两个任务都不刷 last_active(否则「上次活跃」被抹成同一时刻)
#   5. 失败只记日志,不落标记(下次启动再试),更不能影响服务
# ============================================================

import json
import os
import pytest
from unittest.mock import AsyncMock, patch

from maintenance import backfill_mood, backfill_related, looks_default_mood, is_sealed


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine, test_config):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine), \
         patch.dict(server.config, {"buckets_dir": test_config["buckets_dir"]}), \
         patch.object(server, "_maintenance_done", False):
        yield server


# ---------- 1. 补情绪坐标 ----------

@pytest.mark.asyncio
async def test_只补停在默认值的桶(bucket_mgr, mock_dehydrator):
    default = await bucket_mgr.create(content="一条没打过标的旧记忆")   # V0.5/A0.3
    tagged = await bucket_mgr.create(content="打过标的", valence=0.9, arousal=0.7)

    stats = await backfill_mood(bucket_mgr, mock_dehydrator)
    assert stats["targets"] == 1 and stats["changed"] == 1

    got = (await bucket_mgr.get(default))["metadata"]
    assert got["valence"] == 0.7 and got["arousal"] == 0.5   # mock analyze 的值
    untouched = (await bucket_mgr.get(tagged))["metadata"]
    assert untouched["valence"] == 0.9, "已经有坐标的不该被重打"


@pytest.mark.asyncio
async def test_feel桶的坐标不被覆盖(bucket_mgr, mock_dehydrator):
    """feel 的 valence 是模型自己的感受,不是事件效价,不能拿打标器盖掉。"""
    fl = await bucket_mgr.create(content="我今天有点想她", bucket_type="feel")
    await backfill_mood(bucket_mgr, mock_dehydrator)
    assert (await bucket_mgr.get(fl))["metadata"]["valence"] == 0.5


@pytest.mark.asyncio
async def test_补坐标不刷新活跃时间(bucket_mgr, mock_dehydrator):
    bid = await bucket_mgr.create(content="一条旧记忆")
    before = (await bucket_mgr.get(bid))["metadata"]["last_active"]
    await backfill_mood(bucket_mgr, mock_dehydrator)
    assert (await bucket_mgr.get(bid))["metadata"]["last_active"] == before, \
        "批量补建刷 last_active 会把全库的「上次活跃」抹成同一时刻"


@pytest.mark.asyncio
async def test_演习不写入(bucket_mgr, mock_dehydrator):
    bid = await bucket_mgr.create(content="一条旧记忆")
    stats = await backfill_mood(bucket_mgr, mock_dehydrator, dry_run=True)
    assert stats["changed"] == 1
    assert (await bucket_mgr.get(bid))["metadata"]["valence"] == 0.5


@pytest.mark.asyncio
async def test_打标API不可用时安静跳过(bucket_mgr, mock_dehydrator):
    mock_dehydrator.api_available = False
    stats = await backfill_mood(bucket_mgr, mock_dehydrator)
    assert "error" in stats and stats["changed"] == 0


# ---------- 2. 补关联 ----------

@pytest.mark.asyncio
async def test_补关联跳过封存桶(bucket_mgr, mock_embedding_engine):
    a = await bucket_mgr.create(content="活着的一条")
    b = await bucket_mgr.create(content="另一条活的")
    gone = await bucket_mgr.create(content="已归档的")
    await bucket_mgr.archive(gone)

    mock_embedding_engine.enabled = True
    mock_embedding_engine.find_similar_buckets = AsyncMock(
        return_value=[(gone, 0.9), (b, 0.8)]
    )
    stats = await backfill_related(bucket_mgr, mock_embedding_engine)
    assert stats["linked"] >= 1
    rel = (await bucket_mgr.get(a))["metadata"].get("related", [])
    assert gone not in rel, "封存桶不该被挂上来"
    assert b in rel


@pytest.mark.asyncio
async def test_已有关联的不覆盖(bucket_mgr, mock_embedding_engine):
    a = await bucket_mgr.create(content="人工整理过关联的")
    b = await bucket_mgr.create(content="别的")
    await bucket_mgr.set_related(a, ["手工写的"], overwrite=True)

    mock_embedding_engine.enabled = True
    mock_embedding_engine.find_similar_buckets = AsyncMock(return_value=[(b, 0.9)])
    await backfill_related(bucket_mgr, mock_embedding_engine)
    assert (await bucket_mgr.get(a))["metadata"]["related"] == ["手工写的"]


@pytest.mark.asyncio
async def test_向量不可用时安静跳过(bucket_mgr, mock_embedding_engine):
    mock_embedding_engine.enabled = False
    stats = await backfill_related(bucket_mgr, mock_embedding_engine)
    assert "error" in stats and stats["linked"] == 0


# ---------- 3. 只跑一次 ----------

@pytest.mark.asyncio
async def test_跑过一次就不再跑(patched_server, bucket_mgr, mock_dehydrator,
                                mock_embedding_engine, test_config):
    await bucket_mgr.create(content="一条旧记忆")
    mock_embedding_engine.enabled = True      # 两个任务都得能跑完才会落标记

    await patched_server._run_startup_maintenance()
    first = mock_dehydrator.analyze.call_count
    assert first >= 1

    state_path = os.path.join(test_config["buckets_dir"], ".maintenance.json")
    with open(state_path, encoding="utf-8") as f:
        state = json.load(f)
    assert "mood" in state and "related" in state

    # 模拟「重新部署后又启动一次」：标记文件还在 → 一次都不该再跑
    patched_server._maintenance_done = False
    await patched_server._run_startup_maintenance()
    assert mock_dehydrator.analyze.call_count == first, "标记在就不该重跑"


@pytest.mark.asyncio
async def test_关掉开关就完全不跑(patched_server, bucket_mgr, mock_dehydrator):
    await bucket_mgr.create(content="一条旧记忆")
    with patch.object(patched_server, "STARTUP_MAINTENANCE", False):
        await patched_server._run_startup_maintenance()
    mock_dehydrator.analyze.assert_not_called()


@pytest.mark.asyncio
async def test_维护抛错不影响服务(patched_server, bucket_mgr, mock_dehydrator):
    """补坐标整个炸了也只记日志,不抛出去 —— 它不能把服务拖下水。"""
    await bucket_mgr.create(content="一条旧记忆")
    with patch("server.backfill_mood", AsyncMock(side_effect=RuntimeError("炸了"))):
        await patched_server._run_startup_maintenance()   # 不应抛异常


# ---------- 4. 判定函数 ----------

def test_默认值判定保守():
    assert looks_default_mood({"valence": 0.5, "arousal": 0.3})
    assert looks_default_mood({})                      # 字段缺失也算
    assert not looks_default_mood({"valence": 0.5, "arousal": 0.31})
    assert not looks_default_mood({"valence": 0.6, "arousal": 0.3})


def test_封存判定():
    assert is_sealed({"type": "archived"})
    assert is_sealed({"type": "feel"})
    assert is_sealed({"dormant": True})
    assert is_sealed({"expires_at": "2000-01-01T00:00:00"})
    assert not is_sealed({"type": "dynamic"})
    assert not is_sealed({"expires_at": "2099-01-01T00:00:00"})


@pytest.mark.asyncio
async def test_引擎不可用时不落标记下次还会再试(patched_server, bucket_mgr,
                                                mock_dehydrator, test_config):
    """缺 key / embedding 没开 ≠ 做过了。落了标记就永远不会重试,那件事等于永远没做。"""
    await bucket_mgr.create(content="一条旧记忆")
    mock_dehydrator.api_available = False

    await patched_server._run_startup_maintenance()
    state_path = os.path.join(test_config["buckets_dir"], ".maintenance.json")
    state = json.load(open(state_path, encoding="utf-8")) if os.path.exists(state_path) else {}
    assert "mood" not in state, "引擎不可用不该被记成已完成"

    # 恢复可用后再启动一次 —— 这次要真的补上
    mock_dehydrator.api_available = True
    patched_server._maintenance_done = False
    await patched_server._run_startup_maintenance()
    assert mock_dehydrator.analyze.call_count >= 1
    state = json.load(open(state_path, encoding="utf-8"))
    assert state["mood"]["changed"] == 1
