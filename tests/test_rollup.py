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

import json

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import frontmatter as fm

from utils import now_local


# ---------- 假的 LLM 客户端 ----------
# 真引擎要的是「事件数组」:一个周期拆成几条独立记忆,不是一坨流水账。
TWO_EVENTS = json.dumps([
    {"name": "复查那件事", "content": "她去复查了,我一直在等消息。",
     "domain": ["健康"], "tags": ["复查", "医院"], "valence": 0.4, "arousal": 0.6, "importance": 7},
    {"name": "这周的日常", "content": "剩下的日子平平淡淡,吃饭睡觉。",
     "domain": ["日常"], "tags": ["日常"], "valence": 0.6, "arousal": 0.2, "importance": 3},
], ensure_ascii=False)


def _fake_client(text=TWO_EVENTS):
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
    # 分层默认是关的(opt-in),测试里显式打开
    with patch.dict("os.environ", {"OMBRE_ROLLUP_ENABLED": "true"}, clear=False):
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
    assert result["weeks_periods"] == 1          # 整理了一周
    assert result["weeks_created"] == 2          # 这一周拆成了两条事件
    assert result["weeks_source_buckets"] == 3

    # 周记生成了,内容里带 LLM 的产出和源档案 id
    all_b = await bucket_mgr.list_all(include_archive=True)
    weeks = [b for b in all_b if b["metadata"].get("rollup_kind") == "week"]
    assert len(weeks) == 2
    names = sorted(w["metadata"]["name"] for w in weeks)
    assert any("复查那件事" in n for n in names)
    assert any("这周的日常" in n for n in names)
    for wk in weeks:
        assert wk["metadata"]["type"] == "archived"
        assert wk["metadata"]["rollup_period"].startswith("20")
        for bid in ids:
            assert bid in wk["content"]      # 原档 id 写在里面,查得回去
    # 每条事件带着自己的情绪/重要度,不是一刀切的平均值
    fuku = next(w for w in weeks if "复查" in w["metadata"]["name"])
    daily = next(w for w in weeks if "日常" in w["metadata"]["name"])
    assert fuku["metadata"]["importance"] == 7 and daily["metadata"]["importance"] == 3
    assert fuku["metadata"]["domain"] == ["健康"]

    # 原档一个都没少,只是被标记了
    week_ids = {w["id"] for w in weeks}
    for bid in ids:
        m = _meta(bucket_mgr, bid)
        assert m["rolled_up"] == "week"
        assert set(m["rolled_into"]) == week_ids     # 记着卷进了哪几条
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

    assert first["weeks_periods"] == 1
    assert second["weeks_periods"] == 0 and second["weeks_created"] == 0
    all_b = await bucket_mgr.list_all(include_archive=True)
    assert len([b for b in all_b if b["metadata"].get("rollup_kind") == "week"]) == 2


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
    assert r1["weeks_periods"] == 2            # 两周各整理一次
    assert r1["months_periods"] == 1           # 同一轮里接着卷成月记

    r2 = await rollup_eng.run_cycle()          # 再跑一遍不该再生出东西
    assert r2["weeks_periods"] == 0 and r2["months_periods"] == 0

    all_b = await bucket_mgr.list_all(include_archive=True)
    months = [b for b in all_b if b["metadata"].get("rollup_kind") == "month"]
    assert len(months) == 2                    # 月记同样按线索拆条
    assert all(m["metadata"]["rollup_period"] for m in months)
    # 周记被卷起来了,但没删
    weeks = [b for b in all_b if b["metadata"].get("rollup_kind") == "week"]
    assert len(weeks) == 4                     # 两周 × 每周两条
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
    with patch.dict("os.environ", {"OMBRE_ROLLUP_API_KEY": "", "OMBRE_ROLLUP_ENABLED": "true"}, clear=False):
        eng = RollupEngine(cfg, bucket_mgr)
    assert eng.configured is False
    assert (await eng.run_cycle()) == {"skipped": "no_api_key"}


@pytest.mark.asyncio
async def test_disabled_by_env(test_config, bucket_mgr):
    from rollup_engine import RollupEngine
    with patch.dict("os.environ", {"OMBRE_ROLLUP_ENABLED": "false"}, clear=False):
        eng = RollupEngine(test_config, bucket_mgr)
    assert (await eng.run_cycle()) == {"skipped": "disabled"}


@pytest.mark.asyncio
async def test_off_by_default(test_config, bucket_mgr):
    """默认必须是关的:线上脱水那把 key 是设了的,默认开 = 一部署就把全部历史卷完。"""
    from rollup_engine import RollupEngine
    env = {k: v for k, v in __import__("os").environ.items() if not k.startswith("OMBRE_ROLLUP")}
    with patch.dict("os.environ", env, clear=True):
        eng = RollupEngine(test_config, bucket_mgr)
    assert eng.enabled is False
    assert (await eng.run_cycle()) == {"skipped": "disabled"}
    assert (await eng.run_cycle(dry_run=True)) == {"skipped": "disabled"}


# ---------- 8. 只动归档,不碰普通记忆桶 ----------

@pytest.mark.asyncio
async def test_never_touches_normal_buckets(rollup_eng, bucket_mgr):
    """栖栖的红线:现有的桶一根手指都别动。分层只吃 type:archived 的归档。"""
    normal = await bucket_mgr.create(
        content="这是一条很久以前 hold 的普通记忆", name="老记忆",
        domain=["日常"], importance=6,
    )
    # 让它看起来足够老(万一年龄判断写错了,这条会第一个遭殃)
    import frontmatter as fmm
    path = bucket_mgr._find_bucket_file(normal)
    post = fmm.load(path)
    old = (now_local() - timedelta(days=200)).isoformat(timespec="seconds")
    post["created"] = old
    post["last_active"] = old
    with open(path, "w", encoding="utf-8") as f:
        f.write(fmm.dumps(post))
    before = open(path, encoding="utf-8").read()

    base = now_local() - timedelta(days=now_local().weekday() + 14)
    await _archive_on(bucket_mgr, base, "这周的归档")
    await rollup_eng.run_cycle()

    after_path = bucket_mgr._find_bucket_file(normal)
    assert open(after_path, encoding="utf-8").read() == before   # 文件一个字节都没变


# ---------- 9. LLM 返回不是 JSON 时不能把这一周弄丢 ----------

@pytest.mark.asyncio
async def test_bad_json_falls_back_to_single_bucket(rollup_eng, bucket_mgr):
    rollup_eng.client = _fake_client("这周她去复查了,我一直在等消息。")   # 纯文本,不是 JSON
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    src = await _archive_on(bucket_mgr, base, "原始日档")

    result = await rollup_eng.run_cycle()
    assert result["weeks_created"] == 1        # 退回单条兜底,而不是这一周静悄悄消失

    all_b = await bucket_mgr.list_all(include_archive=True)
    wk = next(b for b in all_b if b["metadata"].get("rollup_kind") == "week")
    assert "她去复查了" in wk["content"]
    assert _meta(bucket_mgr, src)["rolled_up"] == "week"


# ---------- 10. 拆太碎要有个上限 ----------

@pytest.mark.asyncio
async def test_max_items_caps_the_split(rollup_eng, bucket_mgr):
    """一周拆出十条会把唤醒时"最近归档"的名额吃光。"""
    many = json.dumps(
        [{"name": f"事件{i}", "content": f"第{i}件事的内容"} for i in range(10)],
        ensure_ascii=False,
    )
    rollup_eng.client = _fake_client(many)
    rollup_eng.max_items = 3
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    await _archive_on(bucket_mgr, base)

    result = await rollup_eng.run_cycle()
    assert result["weeks_created"] == 3


# ---------- 11. 空跑:只报规模,不花钱不写文件 ----------

@pytest.mark.asyncio
async def test_dry_run_reports_plan_without_writing(rollup_eng, bucket_mgr):
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    for i in range(3):
        await _archive_on(bucket_mgr, base + timedelta(days=i))
    before = len(await bucket_mgr.list_all(include_archive=True))

    result = await rollup_eng.run_cycle(dry_run=True)

    assert result["dry_run"] is True
    assert result["weeks_periods"] == 1
    assert result["weeks_created"] == 0            # 空跑不建桶
    assert result["plan"][0]["sources"] == 3
    assert "~" in result["plan"][0]["span"]
    rollup_eng.client.chat.completions.create.assert_not_called()   # 一次 API 都没调
    assert len(await bucket_mgr.list_all(include_archive=True)) == before

    # 空跑之后再真跑,该干的活一件不少
    real = await rollup_eng.run_cycle()
    assert real["weeks_periods"] == 1 and real["weeks_created"] == 2


@pytest.mark.asyncio
async def test_dry_run_works_without_api_key(test_config, bucket_mgr):
    """还没配 key 的时候就该能先空跑看看规模。"""
    from rollup_engine import RollupEngine
    cfg = dict(test_config)
    cfg["dehydration"] = {**test_config["dehydration"], "api_key": ""}
    with patch.dict("os.environ", {"OMBRE_ROLLUP_API_KEY": "", "OMBRE_ROLLUP_ENABLED": "true"}, clear=False):
        eng = RollupEngine(cfg, bucket_mgr)
    base = now_local() - timedelta(days=now_local().weekday() + 14)
    await _archive_on(bucket_mgr, base)

    result = await eng.run_cycle(dry_run=True)
    assert result["dry_run"] is True and result["weeks_periods"] == 1
