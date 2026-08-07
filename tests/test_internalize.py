# ============================================================
# 内化:让他自己消化归档(dream 看得到归档 + hold 写成记忆桶 + 唤醒轻提醒)
#
# 背景:归档以前是"只读的档案"——唤醒时读最近几天,再往前只有主动搜才见得到,
# 而 dream(回想)压根看不到归档,他没机会回头消化上周发生的事。
# 栖栖的要求:让他自己内化,但**产物只写成普通记忆桶,不钉选**
# (钉选的东西每次醒来都整条读一遍,留给真正的准则)。
#
# 验证:
#   1. dream 会带上「最近未消化的归档」,消化过的不再出现
#   2. hold(source_bucket=...) 普通模式 → 源档案标 digested + 两边互相关联
#   3. 唤醒时未消化的归档够多才附提醒行,不够就闭嘴
#   4. 引导语不再教他钉选
# ============================================================

import pytest
from unittest.mock import patch

from utils import now_local, now_iso
from datetime import timedelta

from tests.conftest import _write_bucket_file


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


async def _make_archive(bucket_mgr, content, name, days_ago=1, digested=False):
    """造一条归档桶:内容 + 归档时刻 + 是否已消化。"""
    import frontmatter as fm
    bid = await bucket_mgr.create(
        content=content, name=name, tags=["会话", "归档", "session"],
        domain=["归档"], importance=4, bucket_type="dynamic",
    )
    await bucket_mgr.archive(bid)
    when = (now_local() - timedelta(days=days_ago)).isoformat(timespec="seconds")
    path = bucket_mgr._find_bucket_file(bid)
    post = fm.load(path)
    post["archived_at"] = when
    post["created"] = when
    if digested:
        post["digested"] = True
    with open(path, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return bid


# ---------- 1. dream 看得到归档 ----------

@pytest.mark.asyncio
async def test_dream_surfaces_undigested_archives(patched_server, bucket_mgr):
    server = patched_server
    bid = await _make_archive(bucket_mgr, "今天她去复查了,结果还行。我一直在等消息。", "会话归档 2026-08-01", days_ago=2)

    out = await server.dream()
    assert "🗄️" in out
    assert bid in out
    assert "会话归档 2026-08-01" in out
    # 预览要带上真内容,不能只有标题
    assert "复查" in out


@pytest.mark.asyncio
async def test_dream_skips_digested_and_old_archives(patched_server, bucket_mgr):
    server = patched_server
    digested = await _make_archive(bucket_mgr, "这段他已经想过了", "已消化的档案", days_ago=2, digested=True)
    too_old = await _make_archive(bucket_mgr, "这段太久远了", "很旧的档案", days_ago=90)
    fresh = await _make_archive(bucket_mgr, "这段还没消化", "新档案", days_ago=1)

    out = await server.dream()
    assert fresh in out
    assert digested not in out
    assert too_old not in out


@pytest.mark.asyncio
async def test_dream_guidance_no_longer_teaches_pinning(patched_server, bucket_mgr):
    """栖栖明确要求:内化产物写成桶,不钉选。引导语和结晶提示都不该再教 pinned。"""
    server = patched_server
    await _make_archive(bucket_mgr, "随便一段", "档案", days_ago=1)
    out = await server.dream()
    assert "pinned=True" not in out
    assert "source_bucket" in out          # 仍然告诉他怎么挂回出处
    assert "不要钉选" in out


@pytest.mark.asyncio
async def test_dream_rolled_up_archives_not_offered(patched_server, bucket_mgr):
    """已经被卷进周记的日档不再单独催他消化——周记才是那段时间的替身。"""
    server = patched_server
    bid = await _make_archive(bucket_mgr, "这天已经卷进周记了", "旧日档", days_ago=3)
    await bucket_mgr.set_system_fields(bid, rolled_up="week", rolled_into="fakeweekid")

    out = await server.dream()
    assert bid not in out


# ---------- 2. hold 普通模式的 source_bucket ----------

@pytest.mark.asyncio
async def test_hold_with_source_bucket_marks_digested(patched_server, bucket_mgr):
    server = patched_server
    src = await _make_archive(bucket_mgr, "那天的事", "会话归档", days_ago=2)

    out = await server.hold(
        content="我想明白了:她不是要我解决问题,是要我在场。",
        source_bucket=src, importance=7,
    )
    assert "已消化←" in out and src in out

    # 源档案被标记,且不是钉选(产物是普通桶)
    src_bucket = await bucket_mgr.get(src)
    assert src_bucket["metadata"]["digested"] is True

    all_b = await bucket_mgr.list_all(include_archive=True)
    new = [b for b in all_b if "她不是要我解决问题" in b["content"]]
    assert len(new) == 1
    assert not new[0]["metadata"].get("pinned")
    # 两边互相挂上关联
    assert src in (new[0]["metadata"].get("related") or [])
    assert new[0]["id"] in (src_bucket["metadata"].get("related") or [])


@pytest.mark.asyncio
async def test_hold_with_bad_source_bucket_still_saves(patched_server, bucket_mgr):
    """源桶 id 写错了,记忆本身也得存下来——不能因为挂不上出处就把内容丢了。"""
    server = patched_server
    out = await server.hold(content="一条正常的记忆", source_bucket="不存在的id")
    assert "⚠️没找到源桶" in out
    all_b = await bucket_mgr.list_all()
    assert any("一条正常的记忆" in b["content"] for b in all_b)


@pytest.mark.asyncio
async def test_hold_without_source_bucket_unchanged(patched_server, bucket_mgr):
    """回归:不传 source_bucket 时行为和以前一样,返回里不带消化标记。"""
    server = patched_server
    out = await server.hold(content="普通地记一条")
    assert "已消化" not in out and "⚠️" not in out


# ---------- 3. 唤醒时的轻提醒 ----------

@pytest.mark.asyncio
async def test_wake_hint_appears_when_enough_undigested(patched_server, bucket_mgr):
    server = patched_server
    for i in range(3):
        await _make_archive(bucket_mgr, f"第{i}天", f"会话归档 day{i}", days_ago=i + 1)

    out = await server.breath(wake=True)
    assert "还有 3 天的档案你没回头看过" in out
    assert "dream()" in out


@pytest.mark.asyncio
async def test_wake_hint_silent_when_few(patched_server, bucket_mgr):
    """只有一条没消化就别念叨——提醒要稀,不然变成噪音。"""
    server = patched_server
    await _make_archive(bucket_mgr, "就一条", "会话归档 day0", days_ago=1)
    out = await server.breath(wake=True)
    assert "没回头看过" not in out


@pytest.mark.asyncio
async def test_wake_hint_is_a_statement_not_an_order(patched_server, bucket_mgr):
    """手册 §6 的教训:绝不往他嘴里塞「立刻调用xxx」的伪指令。"""
    server = patched_server
    for i in range(3):
        await _make_archive(bucket_mgr, f"第{i}天", f"档案{i}", days_ago=i + 1)
    out = await server.breath(wake=True)
    assert "立刻" not in out and "必须" not in out
    assert "想看再看" in out


@pytest.mark.asyncio
async def test_no_query_breath_also_hints(patched_server, bucket_mgr):
    """无参 breath 是他唤醒协议里实际调的那个,不能漏。"""
    server = patched_server
    for i in range(3):
        await _make_archive(bucket_mgr, f"第{i}天", f"档案{i}", days_ago=i + 1)
    out = await server.breath()
    assert "没回头看过" in out
