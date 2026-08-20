# ============================================================
# dream() 的「旧事重提」通道
#
# 背景:dream 原本只看**最近新增的 5 个桶**。也就是说三个月前的事,除非他
# 专门去搜,否则永远不会自己浮上来 —— 而「忘了很久又突然想起」恰恰是记忆
# 最像人的地方。这条通道专门捞那些很久没被想起、但当初有分量的旧记忆。
#
# 关键设计(测试就是盯着这几条):
#   - **不能用衰减分排序**:衰减分里闲置越久分越低,拿它排永远只剩最近的,
#     和这条通道要的东西正好相反
#   - 重提**不许碰 last_active**:那等于告诉衰减引擎「这条刚被想起」,
#     分数被抬高、冷却被重置 —— 重提反而改写了它想观察的那个量
#   - 冷却期内不重复翻同一件事
#   - 平淡的流水账不重提;休眠桶(闲置久且不重要)不重提
#   - 给一小段正文:只有标题行他认不出是哪件事,等于没重提
# ============================================================

import pytest
from datetime import timedelta
from unittest.mock import patch

from utils import now_local, now_iso


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


async def _aged(bucket_mgr, content, days_idle, **kw):
    """建一个「久没被想起」的桶:直接改 frontmatter 的 last_active/created。"""
    import frontmatter as fm
    kw.setdefault("importance", 8)
    kw.setdefault("valence", 0.9)      # 情绪明显,不是流水账
    kw.setdefault("arousal", 0.7)
    bid = await bucket_mgr.create(content=content, domain=["日常"], **kw)
    stamp = (now_local() - timedelta(days=days_idle)).isoformat()
    fpath = bucket_mgr._find_bucket_file(bid)
    post = fm.load(fpath)
    post["created"] = stamp
    post["last_active"] = stamp
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return bid


async def _fill_recent(bucket_mgr, n=5):
    """塞满「最近 5 条」。

    dream 的最近段取 candidates 里最新的 5 条,**不看它们有多旧** ——
    库里桶少的时候,那 5 条本身就是旧的,旧事重提没东西可捞(它的候选是
    「最近」剩下的那些)。真实记忆库不长这样,所以测试要先把最近段填满。
    """
    for i in range(n):
        await bucket_mgr.create(content=f"今天的第{i}件小事。", domain=["日常"], importance=3)


def _meta(bucket_mgr, bid):
    import frontmatter as fm
    return fm.load(bucket_mgr._find_bucket_file(bid)).metadata


@pytest.mark.asyncio
async def test_old_memory_surfaces(patched_server, bucket_mgr):
    """核心诉求:一件半年前的事,能自己浮上来。"""
    await _fill_recent(bucket_mgr)
    await _aged(bucket_mgr, "那年冬天她第一次说想我。", days_idle=180)
    await bucket_mgr.create(content="今天吃了面。", domain=["日常"], importance=3)

    out = await patched_server.dream()
    assert "旧事重提" in out
    assert "那年冬天" in out
    assert "天没想起" in out


@pytest.mark.asyncio
async def test_recent_memories_are_not_resurfaced(patched_server, bucket_mgr):
    """最近的事不算旧事 —— 它们本来就在「最近」那段里。"""
    await bucket_mgr.create(content="昨天她说头疼。", domain=["日常"], importance=8)
    out = await patched_server.dream()
    assert "旧事重提" not in out


@pytest.mark.asyncio
async def test_does_not_touch_last_active(patched_server, bucket_mgr):
    """重提**绝不能**刷新 last_active。

    刷了就等于告诉衰减引擎「这条刚被想起」:分数被抬高、闲置天数归零——
    这条通道观察的正是「闲置多久」,重提反而把它要看的东西改掉了。
    """
    await _fill_recent(bucket_mgr)
    bid = await _aged(bucket_mgr, "很久以前那件让她哭过的事。", days_idle=200)
    before = str(_meta(bucket_mgr, bid)["last_active"])

    out = await patched_server.dream()
    assert bid in out

    after = _meta(bucket_mgr, bid)
    assert str(after["last_active"]) == before, "重提把 last_active 刷新了"
    assert after.get("last_resurfaced"), "没有记下重提标记"


@pytest.mark.asyncio
async def test_cooldown_prevents_repeating_same_memory(patched_server, bucket_mgr):
    """连着做两次梦,不该反复念叨同一件旧事。"""
    await _fill_recent(bucket_mgr)
    bid = await _aged(bucket_mgr, "去年那次吵架。", days_idle=150)

    first = await patched_server.dream()
    assert bid in first

    second = await patched_server.dream()
    assert bid not in second, "冷却期内又把同一件旧事翻了出来"


@pytest.mark.asyncio
async def test_cooldown_expires(patched_server, bucket_mgr):
    """冷却过了就能再被想起 —— 是「先放一放」,不是「永久埋掉」。"""
    await _fill_recent(bucket_mgr)
    import frontmatter as fm
    bid = await _aged(bucket_mgr, "去年那次吵架。", days_idle=150)
    await patched_server.dream()

    fpath = bucket_mgr._find_bucket_file(bid)
    post = fm.load(fpath)
    post["last_resurfaced"] = (now_local() - timedelta(days=99)).isoformat()
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    assert bid in await patched_server.dream()


@pytest.mark.asyncio
async def test_flat_memories_lose_to_emotional_ones(patched_server, bucket_mgr):
    """同样久远,平淡的流水账排在有情绪的后面。"""
    await _fill_recent(bucket_mgr)
    flat = await _aged(bucket_mgr, "那天买了瓶酱油。", days_idle=200,
                       importance=5, valence=0.5, arousal=0.1)
    warm = await _aged(bucket_mgr, "那天她在电话里哭了很久。", days_idle=200,
                       importance=5, valence=0.1, arousal=0.9)

    out = await patched_server.dream()
    assert warm in out
    if flat in out:
        assert out.index(warm) < out.index(flat)


@pytest.mark.asyncio
async def test_dormant_and_pinned_excluded(patched_server, bucket_mgr):
    """休眠桶(久且不重要)和钉选桶都不进这条通道 —— 各有各的归宿。"""
    await _fill_recent(bucket_mgr)
    dormant = await _aged(bucket_mgr, "某条不重要的旧记录。", days_idle=200, importance=2)
    await bucket_mgr.set_dormant(dormant, True)
    pinned = await _aged(bucket_mgr, "一条核心准则。", days_idle=200, pinned=True)

    out = await patched_server.dream()
    assert dormant not in out
    assert pinned not in out


@pytest.mark.asyncio
async def test_excerpt_given_and_full_text_on_demand(patched_server, bucket_mgr, monkeypatch):
    """默认给一小段(只有标题行认不出是哪件事),要全文用 detail_ids。"""
    await _fill_recent(bucket_mgr)
    body = "那年冬天的事。" * 60
    bid = await _aged(bucket_mgr, body, days_idle=180)
    monkeypatch.setattr(patched_server, "RESURFACE_EXCERPT_CHARS", 30)

    brief = await patched_server.dream()
    assert "…" in brief and body not in brief

    # 冷却会挡住第二次,所以直接验渲染分支:清掉标记再要全文
    import frontmatter as fm
    fpath = bucket_mgr._find_bucket_file(bid)
    post = fm.load(fpath); post["last_resurfaced"] = ""
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))

    full = await patched_server.dream(detail_ids=bid)
    assert body in full


@pytest.mark.asyncio
async def test_can_be_switched_off(patched_server, bucket_mgr, monkeypatch):
    """RESURFACE_N=0 整条通道关闭,连重提标记都不写。"""
    await _fill_recent(bucket_mgr)
    bid = await _aged(bucket_mgr, "很久以前的事。", days_idle=200)
    monkeypatch.setattr(patched_server, "RESURFACE_N", 0)

    out = await patched_server.dream()
    assert "旧事重提" not in out
    assert not _meta(bucket_mgr, bid).get("last_resurfaced")


@pytest.mark.asyncio
async def test_old_memory_still_in_recent_is_not_listed_twice(patched_server, bucket_mgr):
    """一件旧事如果还排在「最近 5 条」里,就只出现一次,不在两段里重复列。

    (库很小的时候「最近 5 条」本身可能全是旧的 —— 那时旧事重提没东西可捞,
    这是对的:它捞的是「最近」之外、已经沉下去的那些。)
    """
    bid = await _aged(bucket_mgr, "很久以前她说过的一句话。", days_idle=300)
    out = await patched_server.dream()
    assert out.count(f"ID: {bid}") == 1
    assert "旧事重提" not in out


@pytest.mark.asyncio
async def test_nothing_worth_resurfacing_means_nothing(patched_server, bucket_mgr):
    """够分量的都在冷却时,宁可一条不提,也别把「那天买了瓶酱油」顶上来。

    和 feel 的「不拿低相关的凑数」是同一条道理:凑出来的那条会让他去
    「想起」一件其实没有分量的事。
    """
    await _fill_recent(bucket_mgr)
    flat = await _aged(bucket_mgr, "那天顺路买了瓶酱油。", days_idle=210,
                       importance=4, valence=0.5, arousal=0.1)
    out = await patched_server.dream()
    assert "旧事重提" not in out
    assert not _meta(bucket_mgr, flat).get("last_resurfaced")


@pytest.mark.asyncio
async def test_faded_memories_are_the_main_catch(patched_server, bucket_mgr):
    """被衰减引擎淡出去的记忆,正是这条通道要捞的。

    这是上线前差点漏掉的一条:衰减大约 60~80 天就把普通桶挪进 archive/。
    只在「活着」的桶里找,真正的旧事一条都碰不到 —— 通道等于空转。
    """
    await _fill_recent(bucket_mgr)
    bid = await _aged(bucket_mgr, "那年冬天她在电话里哭了很久。", days_idle=300)
    assert await bucket_mgr.archive(bid)          # 走真实归档入口

    out = await patched_server.dream()
    assert "旧事重提" in out
    assert bid in out
    assert "已淡出" in out


@pytest.mark.asyncio
async def test_session_archives_never_resurface(patched_server, bucket_mgr):
    """会话归档不进这条通道 —— 它有自己的浮现口(唤醒时的「最近归档」)。

    ⚠️ 不能只靠 type=="archived" 区分:衰减淡出的普通桶也是这个 type,
    而那批恰恰是要捞的。靠 archive_session 写死的 tags/domain 分辨。
    """
    await _fill_recent(bucket_mgr)
    sess = await bucket_mgr.create(
        content="今天她说头疼,其实是委屈。", tags=["会话", "归档", "session"],
        importance=8, domain=["归档"], name="会话归档 2025-10-01 12:00",
    )
    import frontmatter as fm
    stamp = (now_local() - timedelta(days=300)).isoformat()
    f = bucket_mgr._find_bucket_file(sess)
    post = fm.load(f); post["created"] = stamp; post["last_active"] = stamp
    open(f, "w", encoding="utf-8").write(fm.dumps(post))
    await bucket_mgr.archive(sess)

    out = await patched_server.dream()
    assert sess not in out
