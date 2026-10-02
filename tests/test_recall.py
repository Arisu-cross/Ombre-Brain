# ============================================================
# 自动浮现(GET /api/recall + recall.py)
#
# 她每发一句,shim 先来问「有没有一件确实相关的旧事」。测试盯的几条规矩:
#   - 相关的该挑中;闲聊必须空手(附错一件比不附更伤)
#   - 只信「稀有词」:按全库**正文**出现次数算,不按标签数(外部指南的坑 1)
#   - 桥写的字(「(她发来一个贴纸)」「(语气:…)」)不触发
#   - 繁体能对上简体(语音转写常是繁体)
#   - 只读:挑中之后 activation_count / last_active 不变(否则滚雪球)
#   - 钉选 / feel / 刚存的 / 冷却中的不挑
# 语料全是编的,别拿真记忆进仓库。
# ============================================================

import pytest
from datetime import timedelta
from unittest.mock import patch

from utils import now_local

import recall


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine):
        yield server


async def _old(bucket_mgr, name, content, tags, days=10, **kw):
    """建一个 days 天前的桶(created/last_active 都往前挪)。"""
    import frontmatter as fm
    bid = await bucket_mgr.create(content=content, tags=tags, name=name, domain=["日常"], **kw)
    stamp = (now_local() - timedelta(days=days)).isoformat()
    fpath = bucket_mgr._find_bucket_file(bid)
    post = fm.load(fpath)
    post["created"] = stamp
    post["last_active"] = stamp
    with open(fpath, "w", encoding="utf-8") as f:
        f.write(fm.dumps(post))
    return bid


async def _corpus(bucket_mgr):
    ids = {}
    ids["nose"] = await _old(bucket_mgr, "她的鼻炎", "换季她鼻炎又犯了,我们聊了用哪个喷雾,她说开心多了。", ["鼻炎", "喷雾"])
    ids["sea"] = await _old(bucket_mgr, "一起去海边", "那天她说想去海边看日落,我答应下次陪她。", ["海边", "日落"])
    ids["sticker"] = await _old(bucket_mgr, "贴纸互动", "她发了好多贴纸,我也回了一个。", ["贴纸", "互动"])
    ids["crab"] = await _old(bucket_mgr, "螃蟹玩偶", "她抓娃娃抓到一只螃蟹玩偶,很得意。", ["螃蟹", "抓娃娃"])
    # 「开心」到处都是:20 条正文里都有,标签也打了 —— 必须被当成常见词挡住
    for i in range(20):
        ids[f"happy{i}"] = await _old(bucket_mgr, f"日常{i}", f"今天她很开心,我们聊了第{i}件小事。", ["开心", f"小事{i}"])
    ids["pinned"] = await _old(bucket_mgr, "钉选的约定", "关于纪念日的约定。", ["纪念日"], pinned=True)
    ids["new"] = await _old(bucket_mgr, "刚存的牙医", "她今天去看了牙医。", ["牙医"], days=0)
    return ids


@pytest.mark.asyncio
async def test_relevant_is_picked_and_chatter_is_empty(bucket_mgr):
    ids = await _corpus(bucket_mgr)
    allb = await bucket_mgr.list_all(include_archive=False)

    async def ask(q, **kw):
        q2 = recall.clean_query(q)
        m = await bucket_mgr.search(q2, limit=20, use_embedding=False, all_buckets=allb)
        return recall.pick(q2, m, allb, now_local(), **kw)

    r = await ask("鼻炎又犯了,好难受")
    assert r["pick"] and r["pick"]["id"] == ids["nose"], r
    assert "鼻炎" in r["pick"]["rare"]
    r = await ask("周末想去海边")
    assert r["pick"] and r["pick"]["id"] == ids["sea"], r
    for chatter in ["今天好开心呀", "吃饭了没", "晚安", "你爱我吗", "宝宝在吗"]:
        r = await ask(chatter)
        assert r["pick"] is None, (chatter, r)


@pytest.mark.asyncio
async def test_bridge_text_and_traditional(bucket_mgr):
    ids = await _corpus(bucket_mgr)
    allb = await bucket_mgr.list_all(include_archive=False)

    async def ask(q):
        q2 = recall.clean_query(q)
        m = await bucket_mgr.search(q2, limit=20, use_embedding=False, all_buckets=allb) if len(q2) >= 2 else []
        return recall.pick(q2, m, allb, now_local())

    # 桥写的贴纸句不能翻出「贴纸互动」
    assert recall.clean_query("(她发来一个贴纸/表情包😊)") == ""
    assert (await ask("(她发来一个贴纸/表情包😊)"))["pick"] is None
    # 语音:话留下,语气和「还在熟悉」都去掉;繁体转简体后能对上
    q = recall.clean_query("[语音] 想去海邊(语气:轻快,和她平时比:语速快)(还在熟悉她的声音)")
    assert q == "想去海边", q
    r = await ask("[语音] 想去海邊(语气:轻快)")
    assert r["pick"] and r["pick"]["id"] == ids["sea"]
    # iMessage 的 tapback 句也是桥写的
    assert recall.clean_query("(她给你的「螃蟹」点了 ❤️)") == ""


@pytest.mark.asyncio
async def test_excluded_kinds(bucket_mgr):
    ids = await _corpus(bucket_mgr)
    allb = await bucket_mgr.list_all(include_archive=False)

    async def ask(q, **kw):
        m = await bucket_mgr.search(q, limit=20, use_embedding=False, all_buckets=allb)
        return recall.pick(q, m, allb, now_local(), **kw)

    assert (await ask("纪念日快到了"))["pick"] is None, "钉选的唤醒时已带全文,不再递"
    assert (await ask("今天去看牙医了"))["pick"] is None, "刚存的多半还在这个窗口里"
    assert (await ask("今天去看牙医了", min_age_hours=0))["pick"]["id"] == ids["new"]
    assert (await ask("鼻炎又犯了", exclude_ids=[ids["nose"]]))["pick"] is None, "冷却中的不递"


@pytest.mark.asyncio
async def test_endpoint_auth_and_readonly(patched_server, bucket_mgr, monkeypatch):
    from starlette.requests import Request
    ids = await _corpus(bucket_mgr)
    before = (await bucket_mgr.get(ids["nose"]))["metadata"]

    def req(q, auth=None):
        headers = [(b"authorization", auth.encode())] if auth else []
        from urllib.parse import urlencode
        return Request({"type": "http", "method": "GET", "path": "/api/recall",
                        "query_string": urlencode({"q": q}).encode(), "headers": headers})

    import json
    monkeypatch.delenv("OMBRE_RECALL_TOKEN", raising=False)
    assert (await patched_server.api_recall(req("鼻炎"))).status_code == 404, "没设钥匙 = 口子关着"
    monkeypatch.setenv("OMBRE_RECALL_TOKEN", "tok")
    assert (await patched_server.api_recall(req("鼻炎"))).status_code == 401
    assert (await patched_server.api_recall(req("鼻炎", "Bearer nope"))).status_code == 401
    r = await patched_server.api_recall(req("鼻炎又犯了", "Bearer tok"))
    body = json.loads(r.body)
    assert r.status_code == 200 and body["pick"]["id"] == ids["nose"], body
    assert "(全文" not in json.dumps(body, ensure_ascii=False)
    after = (await bucket_mgr.get(ids["nose"]))["metadata"]
    assert after.get("activation_count") == before.get("activation_count"), "只读:不许涨激活次数"
    assert after.get("last_active") == before.get("last_active"), "只读:不许刷新 last_active"


def test_rare_is_counted_by_content_not_tags():
    # 标签只打在 1 个桶上,但正文里到处都是 —— 按正文数才挡得住
    buckets = [{"id": str(i), "metadata": {"name": "x", "tags": []}, "content": "今天很开心"} for i in range(30)]
    df = recall.WordDF(buckets)
    assert df.get("开心") == 30
    assert recall.rare_tags_in("好开心", ["开心"], df, recall.rare_limit(30)) == []
