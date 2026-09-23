# ============================================================
# 接续三段:压缩前原话(shim 写) / 给下一个窗口的信 / 醒来看到待办
# 见 continuity.py 顶部说明。
# ============================================================

import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import continuity


# ---------- 纯函数 ----------

def test_raw_tail_roundtrip_and_ttl(tmp_path):
    d = str(tmp_path)
    continuity.save_raw_tail(d, "她:还醒着吗\n你:在呢", "2026-09-23T10:00:00")
    now = datetime(2026, 9, 23, 12, 0, 0)
    got = continuity.load_raw_tail(d, now, ttl_hours=12)
    assert got["text"] == "她:还醒着吗\n你:在呢"
    # 过期就不再浮现
    assert continuity.load_raw_tail(d, now + timedelta(hours=11), ttl_hours=12) is None


def test_raw_tail_overwrites_only_latest(tmp_path):
    d = str(tmp_path)
    continuity.save_raw_tail(d, "旧的", "2026-09-23T10:00:00")
    continuity.save_raw_tail(d, "新的", "2026-09-23T11:00:00")
    assert continuity.load_raw_tail(d, datetime(2026, 9, 23, 11, 5), 12)["text"] == "新的"


def test_raw_tail_clip_keeps_newest_and_order(tmp_path):
    lines = [f"她:第{i}句" for i in range(200)]
    body = continuity.clip_tail("\n".join(lines), 300)
    assert body.startswith("…(更早的省略)")
    assert body.rstrip().endswith("她:第199句"), "离压缩最近的一句必须留下"
    kept = [l for l in body.splitlines()[1:]]
    nums = [int(l.split("第")[1].rstrip("句")) for l in kept]
    assert nums == sorted(nums), "顺序不能乱"
    assert all(l.startswith("她:第") for l in kept), "不能从一句话中间切开"


def test_raw_tail_missing_or_corrupt_is_none(tmp_path):
    d = str(tmp_path)
    now = datetime(2026, 9, 23)
    assert continuity.load_raw_tail(d, now) is None
    (tmp_path / continuity.RAW_TAIL_FILE).write_text("{坏的", encoding="utf-8")
    assert continuity.load_raw_tail(d, now) is None


def test_letters_recent_newest_first_and_ttl(tmp_path):
    d = str(tmp_path)
    continuity.append_letter(d, "很早的信", "2026-09-10T08:00:00")
    continuity.append_letter(d, "昨天的信", "2026-09-22T08:00:00")
    continuity.append_letter(d, "今天的信", "2026-09-23T08:00:00")
    continuity.append_letter(d, "   ", "2026-09-23T09:00:00")   # 空信不存
    with open(tmp_path / continuity.LETTERS_FILE, "a", encoding="utf-8") as f:
        f.write("不是json\n")                                      # 坏行跳过
    now = datetime(2026, 9, 23, 12)
    assert [l["text"] for l in continuity.recent_letters(d, now, ttl_days=3, n=5)] == ["今天的信", "昨天的信"]
    assert [l["text"] for l in continuity.recent_letters(d, now, ttl_days=3, n=1)] == ["今天的信"]
    assert continuity.recent_letters(d, now, n=0) == []


def test_render_extras_skips_empty_sections():
    assert continuity.render_wake_extras(None, [], []) == []
    parts = continuity.render_wake_extras(
        {"at": "2026-09-23T10:00:00", "text": "她:晚安"},
        [{"at": "2026-09-23T09:00:00", "text": "记得问她体检结果"}],
        ["买药"],
    )
    assert [p.splitlines()[0][:8] for p in parts] == ["=== 压缩前最", "=== 上一个窗", "=== 没做完的"]


# ---------- 接进 server ----------

class FakeRequest:
    def __init__(self, body=None, headers=None):
        self._body = body
        self.headers = headers or {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def srv(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine, test_config):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine), \
         patch.object(server, "config", test_config), \
         patch.object(server, "RAW_TAIL_KEY", "k123"):
        yield server


def _json(resp):
    return resp.status_code, json.loads(resp.body)


@pytest.mark.asyncio
async def test_api_raw_tail_auth(srv):
    assert (await srv.api_raw_tail(FakeRequest({"text": "x"}))).status_code == 401
    assert (await srv.api_raw_tail(FakeRequest({"text": "x"}, {"x-raw-key": "错的"}))).status_code == 401
    assert (await srv.api_raw_tail(FakeRequest({"text": "  "}, {"x-raw-key": "k123"}))).status_code == 400
    code, body = _json(await srv.api_raw_tail(FakeRequest({"text": "她:抱抱"}, {"x-raw-key": "k123"})))
    assert code == 200 and body["ok"]


@pytest.mark.asyncio
async def test_api_raw_tail_disabled_without_key(srv):
    with patch.object(srv, "RAW_TAIL_KEY", ""):
        assert (await srv.api_raw_tail(FakeRequest({"text": "x"}, {"x-raw-key": ""}))).status_code == 404


@pytest.mark.asyncio
async def test_wake_puts_continuity_first(srv, bucket_mgr):
    await bucket_mgr.create(content="核心准则内容", name="核心准则", domain=["日常"], pinned=True)
    await bucket_mgr.create(content="要做的事\n- [ ] 帮她订周六的票", name="周末", domain=["日常"])
    await srv.api_raw_tail(FakeRequest({"text": "她:你刚才说到哪了\n你:说到海边"}, {"x-raw-key": "k123"}))
    await srv.archive_session(summary="今天聊了海", letter="下个窗口记得问她票订好没")

    out = await srv.breath(wake=True)
    i_raw, i_letter, i_todo, i_pin = (out.index(s) for s in
        ("压缩前最后的原话", "上一个窗口留给你的话", "没做完的事", "核心准则"))
    assert i_raw < i_letter < i_todo < i_pin, "接续三段必须排在最前,顺序固定"
    assert "说到海边" in out
    assert "下个窗口记得问她票订好没" in out
    assert "帮她订周六的票" in out


@pytest.mark.asyncio
async def test_wake_without_any_continuity_is_unchanged(srv, bucket_mgr):
    await bucket_mgr.create(content="核心准则内容", name="核心准则", domain=["日常"], pinned=True)
    out = await srv.breath(wake=True)
    assert "压缩前最后的原话" not in out
    assert "上一个窗口留给你的话" not in out
    assert "没做完的事" not in out
    assert out.startswith("=== 核心准则 ===")


@pytest.mark.asyncio
async def test_archive_letter_optional_and_reported(srv):
    r1 = await srv.archive_session(summary="只写日记")
    assert "信已留" not in r1
    r2 = await srv.archive_session(summary="再归一段", letter="别忘了她怕打雷")
    assert "信已留给下一个窗口" in r2


@pytest.mark.asyncio
async def test_wake_todos_capped(srv, bucket_mgr):
    body = "\n".join(f"- [ ] 事情{i}" for i in range(12))
    await bucket_mgr.create(content=body, name="一堆事", domain=["日常"])
    with patch.object(srv, "WAKE_TODO_N", 3):
        out = await srv.breath(wake=True)
    assert "事情0" in out and "事情2" in out and "事情3" not in out
    assert "…还有 9 项" in out


@pytest.mark.asyncio
async def test_breath_hook_also_gets_extras(srv, bucket_mgr):
    await srv.api_raw_tail(FakeRequest({"text": "她:晚安啦"}, {"x-raw-key": "k123"}))
    resp = await srv.breath_hook(None)
    assert "压缩前最后的原话" in resp.body.decode()
