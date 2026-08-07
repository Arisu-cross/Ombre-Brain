# ============================================================
# 面板上删改记忆(dashboard 编辑 + 回收站)
#
# 背景:面板一直只读 —— 记错的、重复的、不想留的,只能等衰减,或者绕一圈让沈渡自己改。
# 现在栖栖能直接改内容/元数据,也能删。删是软删除:桶挪进 base_dir/trash,
# 那个目录不在 _find_bucket_file / list_all 的扫描范围里,所以对 breath/search/decay
# 而言这段记忆已经不存在了 —— 但文件还在,能放回去。手册红线:宁可留着,绝不误删。
#
# 验证:
#   1. update 白名单:能改的字段真的改了
#   2. soft_delete:桶从检索里消失、进回收站、原路径记下来了
#   3. restore_from_trash:放回原目录,检索能再找到,回收站清空
#   4. purge_trash / purge_all_trash:文件真的没了
#   5. HTTP 层:POST 改、DELETE 删、GET /api/trash、restore、非法字段被挡
# ============================================================

import json
import os

import pytest
from unittest.mock import patch


# ---------- 一个够用的假 Request(starlette 的 Request 要 scope+receive) ----------
class FakeRequest:
    def __init__(self, path_params=None, body=None, query=None):
        self.path_params = path_params or {}
        self._body = body
        self.query_params = query or {}
        self.cookies = {}
        self.headers = {}

    async def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def patched_server(bucket_mgr, decay_eng, mock_dehydrator, mock_embedding_engine):
    import server
    with patch.object(server, "bucket_mgr", bucket_mgr), \
         patch.object(server, "decay_engine", decay_eng), \
         patch.object(server, "dehydrator", mock_dehydrator), \
         patch.object(server, "embedding_engine", mock_embedding_engine), \
         patch.object(server, "_require_auth", lambda request: None):
        yield server


def _payload(response):
    return json.loads(bytes(response.body).decode())


# ---------- 1. 改 ----------

@pytest.mark.asyncio
async def test_update_edits_content_and_meta(bucket_mgr):
    bid = await bucket_mgr.create(content="原来的内容", name="旧名字", tags=["a"], importance=4)

    ok = await bucket_mgr.update(
        bid, content="改过的内容", name="新名字", tags=["b", "c"], importance=9
    )
    assert ok

    b = await bucket_mgr.get(bid)
    assert b["content"] == "改过的内容"
    assert b["metadata"]["name"] == "新名字"
    assert b["metadata"]["tags"] == ["b", "c"]
    assert b["metadata"]["importance"] == 9


# ---------- 2. 删(软) ----------

@pytest.mark.asyncio
async def test_soft_delete_hides_bucket_but_keeps_file(bucket_mgr):
    bid = await bucket_mgr.create(content="想删掉的一段", name="待删")

    info = await bucket_mgr.soft_delete(bid)
    assert info and info["id"] == bid
    assert info["trashed_from"].startswith("dynamic" + os.sep)

    # 检索侧:彻底看不见了
    assert await bucket_mgr.get(bid) is None
    assert bid not in [b["id"] for b in await bucket_mgr.list_all(include_archive=True)]

    # 回收站里躺着,内容没丢
    trash = await bucket_mgr.list_trash()
    assert [t["id"] for t in trash] == [bid]
    assert "想删掉的一段" in trash[0]["content_preview"]
    assert trash[0]["trashed_at"]


@pytest.mark.asyncio
async def test_soft_delete_missing_bucket_returns_none(bucket_mgr):
    assert await bucket_mgr.soft_delete("nope-not-here") is None


# ---------- 3. 恢复 ----------

@pytest.mark.asyncio
async def test_restore_puts_bucket_back(bucket_mgr):
    bid = await bucket_mgr.create(content="删错了", name="后悔了", domain=["生活"])
    before = bucket_mgr._find_bucket_file(bid)

    await bucket_mgr.soft_delete(bid)
    assert await bucket_mgr.restore_from_trash(bid)

    b = await bucket_mgr.get(bid)
    assert b is not None
    assert b["content"] == "删错了"
    # 放回原处,且回收站的记号擦干净了
    assert bucket_mgr._find_bucket_file(bid) == before
    assert "trashed_from" not in b["metadata"]
    assert "trashed_at" not in b["metadata"]
    assert await bucket_mgr.list_trash() == []


@pytest.mark.asyncio
async def test_restore_when_original_dir_is_gone(bucket_mgr, test_config):
    """原目录被删了也要能落地,不能因为少个文件夹就把记忆卡在回收站里。"""
    bid = await bucket_mgr.create(content="域目录没了", domain=["生活"])
    path = bucket_mgr._find_bucket_file(bid)
    await bucket_mgr.soft_delete(bid)
    os.rmdir(os.path.dirname(path))

    assert await bucket_mgr.restore_from_trash(bid)
    assert (await bucket_mgr.get(bid))["content"] == "域目录没了"


# ---------- 4. 彻底删 ----------

@pytest.mark.asyncio
async def test_purge_removes_file_for_good(bucket_mgr):
    bid = await bucket_mgr.create(content="不要了", name="真删")
    await bucket_mgr.soft_delete(bid)

    assert await bucket_mgr.purge_trash(bid)
    assert await bucket_mgr.list_trash() == []
    assert await bucket_mgr.restore_from_trash(bid) is False
    assert os.listdir(bucket_mgr.trash_dir) == []


@pytest.mark.asyncio
async def test_purge_all(bucket_mgr):
    ids = []
    for i in range(3):
        bid = await bucket_mgr.create(content=f"第{i}段")
        await bucket_mgr.soft_delete(bid)
        ids.append(bid)

    purged = await bucket_mgr.purge_all_trash()
    assert sorted(purged) == sorted(ids)
    assert await bucket_mgr.list_trash() == []


# ---------- 5. HTTP 层 ----------

@pytest.mark.asyncio
async def test_api_edit_and_delete_and_restore(patched_server, bucket_mgr):
    server = patched_server
    bid = await bucket_mgr.create(content="面板要改的", name="面板桶", importance=3)

    # 改:标签用逗号串传(前端就是这么发的),中文逗号也认
    resp = await server.api_bucket_edit(FakeRequest(
        path_params={"bucket_id": bid},
        body={"content": "面板改完的", "tags": "甲，乙", "importance": 8, "resolved": True},
    ))
    assert resp.status_code == 200
    data = _payload(resp)
    assert data["ok"] and set(data["updated"]) == {"content", "tags", "importance", "resolved"}
    assert data["metadata"]["tags"] == ["甲", "乙"]
    assert data["metadata"]["importance"] == 8
    assert data["metadata"]["resolved"] is True
    assert data["content"] == "面板改完的"

    # 删:软删除,进回收站
    resp = await server.api_bucket_delete(FakeRequest(path_params={"bucket_id": bid}))
    assert resp.status_code == 200 and _payload(resp)["hard"] is False
    assert await bucket_mgr.get(bid) is None

    resp = await server.api_trash_list(FakeRequest())
    assert [t["id"] for t in _payload(resp)] == [bid]

    # 恢复
    resp = await server.api_trash_restore(FakeRequest(path_params={"bucket_id": bid}))
    assert resp.status_code == 200
    assert (await bucket_mgr.get(bid))["content"] == "面板改完的"


@pytest.mark.asyncio
async def test_api_edit_rejects_bad_input(patched_server, bucket_mgr):
    server = patched_server
    bid = await bucket_mgr.create(content="别被改坏", name="守门")

    # 不在白名单的字段:不认,当成"没有可改的字段"
    resp = await server.api_bucket_edit(FakeRequest(
        path_params={"bucket_id": bid}, body={"id": "hack", "created": "2020-01-01"}))
    assert resp.status_code == 400

    # 空内容:挡掉(要清空就删整个桶)
    resp = await server.api_bucket_edit(FakeRequest(
        path_params={"bucket_id": bid}, body={"content": "   "}))
    assert resp.status_code == 400

    # 名字空:挡掉
    resp = await server.api_bucket_edit(FakeRequest(
        path_params={"bucket_id": bid}, body={"name": "  "}))
    assert resp.status_code == 400

    # 值不合法:挡掉,不写坏文件
    resp = await server.api_bucket_edit(FakeRequest(
        path_params={"bucket_id": bid}, body={"importance": "很重要"}))
    assert resp.status_code == 400

    # 不存在的桶
    resp = await server.api_bucket_edit(FakeRequest(
        path_params={"bucket_id": "nope"}, body={"content": "x"}))
    assert resp.status_code == 404

    b = await bucket_mgr.get(bid)
    assert b["content"] == "别被改坏" and b["metadata"]["name"] == "守门"


@pytest.mark.asyncio
async def test_api_hard_delete_skips_trash(patched_server, bucket_mgr):
    server = patched_server
    bid = await bucket_mgr.create(content="直接抹掉")

    resp = await server.api_bucket_delete(FakeRequest(
        path_params={"bucket_id": bid}, query={"hard": "1"}))
    assert resp.status_code == 200 and _payload(resp)["hard"] is True
    assert await bucket_mgr.get(bid) is None
    assert await bucket_mgr.list_trash() == []


@pytest.mark.asyncio
async def test_api_delete_missing_returns_404(patched_server):
    server = patched_server
    resp = await server.api_bucket_delete(FakeRequest(path_params={"bucket_id": "nope"}))
    assert resp.status_code == 404
    resp = await server.api_trash_restore(FakeRequest(path_params={"bucket_id": "nope"}))
    assert resp.status_code == 404
    resp = await server.api_trash_purge(FakeRequest(path_params={"bucket_id": "nope"}))
    assert resp.status_code == 404
