# ============================================================
# 归档分层:日档 → 周记 → 月记
#
# 背景:archive_session 每天写一个日档,唤醒只浮现最近几条。上上周的事既不会
# 自己浮上来,也没有一个粗一点的替身——时间一长连续性就断在最近这几天。
# 栖栖拍板:日档留一周,超过一周合成「这周大概是这样」,一个月后再合成
# 「这个月大概是这样」。原档一律保留(手册红线:绝不丢记忆),只是不再单独浮现。
#
# 验证:
#   1. 还没满 7 天的日档不动
#   2. 满 7 天且那一周已经过完 → 生成周记,原档标 rolled_up 但文件还在
#   3. 本周还没过完不卷(否则后面几天没处放)
#   4. 跑第二遍不会重复生成
#   5. 周记满 30 天 → 卷成月记
#   6. 浮现层:唤醒读到的是周记,不是被卷起的那几天日档
#   7. 没配 API key 就跳过,不炸
# ============================================================

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter as fm

from utils import now_local


# ---------- 假的 LLM 客户端 ----------
def _fake_client(text="这周大概是这样:她去了医院,我一直在等消息。"):
    client = MagicMock()
    msg = MagicMock()
    msg.message.content = text
    resp = MagicMock()
    resp.choices = [msg]
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


@pytest.fixture
def rollup_eng(test_config, bucket_mgr, mock_embedding_engine):
    from rollup_engine import RollupEngine
    eng = RollupEngine(test_config, bucket_mgr, None, mock_embedding_engine)
    eng.client = _fake_client()
    return eng


async def _archive_on(bucket_mgr, day: datetime, content="今天的事", name=None):
    """造一条指定日期的日档。"""
    name = name or f"会话归档 {day.strftime('%Y-%m-%d')}"
    bid = await bucket_mgr.create(
        content=content, name=name, tags=["会话", "归档", "session"],
        domain=["归档"], importance=4, bucket_type="dynamic",
    )
    await bucket_mgr.archive(bid)
    path = bucket_mgr._find_bucket_file(bid)
    post = fm.load(path)
    stamp = day.isoformat(timespec="seconds")
    post["archived_at"] = stamp
    post["created"] = stamp
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return bid


def _meta(bucket_mgr, bid):
    path = bucket_mgr._find_bucket_file(bid)
    return dict(fm.load(path).metadata)


# ---------- 1. 太新的不卷 ----------

@pytest.mark.asyncio
async def test_recent_dailies_are_left_alone(rollup_eng, bucket_mgr):
    ids = [await _archive_on(bucket_mgr, now_local() - timedelta(days=d)) for d in (1, 2, 3)]
    result = await rollup_eng.run_cycle()

    assert result["weeks_created"] == 0
    for bid in ids:
        assert "rolled_up" not in _meta(bucket_mgr, bid)


# ---------- 2. 够老的按周合成 ----------

@pytest.mark.asyncio
async def test_old_dailies_roll_into_week(rollup_eng, bucket_mgr):
    # 上上周的周一到周三(保证那一周整个已经过完,且都超过 7 天)
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    ids = [await _archive_on(bucket_mgr, base + timedelta(days=i), f"第{i}天的事") for i in range(3)]

    result = await rollup_eng.run_cycle()
    assert result["weeks_created"] == 1
    assert result["weeks_source_buckets"] == 3

    # 周记生成了,内容里带 LLM 的产出和源档案 id
    all_b = await bucket_mgr.list_all(include_archive=True)
    weeks = [b for b in all_b if b["metadata"].get("rollup_kind") == "week"]
    assert len(weeks) == 1
    wk = weeks[0]
    assert wk["metadata"]["type"] == "archived"
    assert "这周大概是这样" in wk["content"]
    for bid in ids:
        assert bid in wk["content"]          # 原档 id 写在周记里,查得回去

    # 原档一个都没少,只是被标记了
    for bid in ids:
        m = _meta(bucket_mgr, bid)
        assert m["rolled_up"] == "week"
        assert m["rolled_into"] == wk["id"]
        assert (await bucket_mgr.get(bid)) is not None    # 文件还在,搜得到


@pytest.mark.asyncio
async def test_week_archived_at_aligns_to_period_not_now(rollup_eng, bucket_mgr):
    """周记的归档时刻要对齐到那段时间的末尾,否则一条讲上个月的周记会排到最新日档前面。"""
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    await _archive_on(bucket_mgr, base)
    await _archive_on(bucket_mgr, base + timedelta(days=2))
    await rollup_eng.run_cycle()

    all_b = await bucket_mgr.list_all(include_archive=True)
    wk = next(b for b in all_b if b["metadata"].get("rollup_kind") == "week")
    assert wk["metadata"]["archived_at"][:10] == (base + timedelta(days=2)).strftime("%Y-%m-%d")


# ---------- 3. 没过完的周不卷 ----------

@pytest.mark.asyncio
async def test_current_week_not_rolled(rollup_eng, bucket_mgr):
    """本周还在进行中:哪怕有条目够老(跨周的周一),也不能现在就把这周封盘。"""
    monday = now_local() - timedelta(days=now_local().weekday())
    await _archive_on(bucket_mgr, monday)
    rollup_eng.daily_days = 1        # 放宽年龄门槛,单独考"这周结束了没"
    result = await rollup_eng.run_cycle()
    assert result["weeks_created"] == 0


# ---------- 4. 幂等 ----------

@pytest.mark.asyncio
async def test_second_run_creates_nothing_new(rollup_eng, bucket_mgr):
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    await _archive_on(bucket_mgr, base)
    await _archive_on(bucket_mgr, base + timedelta(days=1))

    first = await rollup_eng.run_cycle()
    second = await rollup_eng.run_cycle()

    assert first["weeks_created"] == 1
    assert second["weeks_created"] == 0
    all_b = await bucket_mgr.list_all(include_archive=True)
    assert len([b for b in all_b if b["metadata"].get("rollup_kind") == "week"]) == 1


# ---------- 5. 周记 → 月记 ----------

@pytest.mark.asyncio
async def test_old_weeks_roll_into_month(rollup_eng, bucket_mgr):
    # 造两周的日档,都落在两个月前,保证周记生成后立刻满足"满 30 天"
    today = now_local()
    first_of_this_month = today.replace(day=1)
    last_month_end = first_of_this_month - timedelta(days=1)
    target_month_end = last_month_end.replace(day=1) - timedelta(days=1)  # 上上个月最后一天

    d1 = target_month_end - timedelta(days=20)
    d2 = target_month_end - timedelta(days=6)
    await _archive_on(bucket_mgr, d1, "上上个月上旬")
    await _archive_on(bucket_mgr, d2, "上上个月下旬")

    # 一轮就能追平:先卷周记,同一轮里够老的周记接着被卷成月记
    # (第一次上线时积压的历史档案也是这样一次补齐的)
    r1 = await rollup_eng.run_cycle()
    assert r1["weeks_created"] == 2
    assert r1["months_created"] == 1

    r2 = await rollup_eng.run_cycle()          # 再跑一遍不该再生出东西
    assert r2["weeks_created"] == 0 and r2["months_created"] == 0

    all_b = await bucket_mgr.list_all(include_archive=True)
    months = [b for b in all_b if b["metadata"].get("rollup_kind") == "month"]
    assert len(months) == 1
    assert months[0]["metadata"]["rollup_period"] is not None
    # 周记被卷起来了,但没删
    weeks = [b for b in all_b if b["metadata"].get("rollup_kind") == "week"]
    assert len(weeks) == 2
    assert all(w["metadata"].get("rolled_up") == "month" for w in weeks)


# ---------- 6. 浮现层 ----------

@pytest.mark.asyncio
async def test_wake_surfaces_week_not_rolled_dailies(
    rollup_eng, bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine
):
    import server
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    old_ids = [await _archive_on(bucket_mgr, base + timedelta(days=i), f"旧的第{i}天") for i in range(3)]
    fresh = await _archive_on(bucket_mgr, now_local() - timedelta(days=1), "昨天的事")
    await rollup_eng.run_cycle()

    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        out = await server.breath(wake=True)

    assert f"[bucket_id:{fresh}]" in out      # 最近 7 天的日档照常浮现
    for bid in old_ids:
        # 不再作为独立条目占名额(id 仍会出现在周记正文的"原档都还在"那一行里,
        # 那是留给他查回去的线索,不是一条浮现结果)
        assert f"[bucket_id:{bid}]" not in out
    assert "周记" in out                       # 那段时间没断线,有周记顶上


# ---------- 7. 没 key 不炸 ----------

@pytest.mark.asyncio
async def test_no_api_key_skips_gracefully(test_config, bucket_mgr):
    from rollup_engine import RollupEngine
    cfg = dict(test_config)
    cfg["dehydration"] = {**test_config["dehydration"], "api_key": ""}
    with patch.dict("os.environ", {"OMBRE_ROLLUP_API_KEY": ""}, clear=False):
        eng = RollupEngine(cfg, bucket_mgr)
    assert eng.configured is False
    assert (await eng.run_cycle()) == {"skipped": "no_api_key"}


@pytest.mark.asyncio
async def test_disabled_by_env(test_config, bucket_mgr):
    from rollup_engine import RollupEngine
    with patch.dict("os.environ", {"OMBRE_ROLLUP_ENABLED": "false"}, clear=False):
        eng = RollupEngine(test_config, bucket_mgr)
    assert (await eng.run_cycle()) == {"skipped": "disabled"}
