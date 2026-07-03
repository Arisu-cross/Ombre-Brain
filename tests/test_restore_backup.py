# ============================================================
# 备份→恢复 往返测试
# Backup/restore roundtrip: build_export → restore_from_export
# into a fresh store must reproduce every bucket verbatim.
#
# 验证:
#   - 全类型桶(dynamic/pinned/feel/archived)往返后 id/元数据/正文一致
#   - 默认跳过已存在的桶,--overwrite 覆盖
#   - dry-run 不写任何文件
#   - 不认识的 schema 拒绝恢复
# ============================================================

import os
import pytest

from backup_engine import BackupEngine
from bucket_manager import BucketManager
from restore_from_backup import restore_from_export


def _fresh_store(tmp_path, name="restored"):
    buckets_dir = str(tmp_path / name)
    for d in ["permanent", "dynamic", "archive", "feel"]:
        os.makedirs(os.path.join(buckets_dir, d), exist_ok=True)
    return buckets_dir


async def _seed_all_types(bucket_mgr):
    ids = {}
    ids["dynamic"] = await bucket_mgr.create(
        content="动态记忆", name="动态", domain=["日常"], importance=6,
        valence=0.7, arousal=0.4,
    )
    ids["pinned"] = await bucket_mgr.create(
        content="核心准则", name="准则", domain=["自省"], pinned=True,
    )
    ids["feel"] = await bucket_mgr.create(
        content="我的感受", name="feel1", bucket_type="feel",
    )
    ids["archived"] = await bucket_mgr.create(
        content="要归档的", name="旧事", domain=["日常"],
    )
    assert await bucket_mgr.archive(ids["archived"])
    return ids


@pytest.mark.asyncio
async def test_roundtrip_reproduces_all_buckets(test_config, bucket_mgr, tmp_path):
    """备份→恢复到全新目录后，所有桶的 id/元数据/正文一字不差。"""
    ids = await _seed_all_types(bucket_mgr)

    engine = BackupEngine(test_config, bucket_mgr)
    export = await engine.build_export()
    assert export["bucket_count"] == 4

    restored_dir = _fresh_store(tmp_path)
    result = restore_from_export(export, restored_dir)
    assert result == {"restored": 4, "skipped": 0, "overwritten": 0, "failed": 0}

    restored_cfg = dict(test_config, buckets_dir=restored_dir)
    restored_mgr = BucketManager(restored_cfg)
    originals = {b["id"]: b for b in await bucket_mgr.list_all(include_archive=True)}
    restored = {b["id"]: b for b in await restored_mgr.list_all(include_archive=True)}

    assert set(restored) == set(originals) == set(ids.values())
    for bid, orig in originals.items():
        assert restored[bid]["content"] == orig["content"], f"content mismatch: {bid}"
        assert restored[bid]["metadata"] == orig["metadata"], f"metadata mismatch: {bid}"

    # 归档桶要落在 archive/ 目录，钉选落在 permanent/，feel 落在 feel/
    assert "/archive/" in restored[ids["archived"]]["path"]
    assert "/permanent/" in restored[ids["pinned"]]["path"]
    assert "/feel/" in restored[ids["feel"]]["path"]


@pytest.mark.asyncio
async def test_restore_skips_existing_unless_overwrite(test_config, bucket_mgr, tmp_path):
    """已存在的桶默认跳过；overwrite=True 时用备份版本覆盖。"""
    bid = await bucket_mgr.create(content="原始内容", name="原始", domain=["日常"], importance=5)
    engine = BackupEngine(test_config, bucket_mgr)
    export = await engine.build_export()

    # 备份后修改桶
    await bucket_mgr.update(bid, importance=9)

    # 默认：跳过，改动保留
    result = restore_from_export(export, test_config["buckets_dir"])
    assert result["skipped"] == 1 and result["restored"] == 0
    bucket = await bucket_mgr.get(bid)
    assert int(bucket["metadata"]["importance"]) == 9

    # overwrite：回滚到备份版本
    result = restore_from_export(export, test_config["buckets_dir"], overwrite=True)
    assert result["overwritten"] == 1
    bucket = await bucket_mgr.get(bid)
    assert int(bucket["metadata"]["importance"]) == 5
    assert bucket["content"] == "原始内容"


@pytest.mark.asyncio
async def test_dry_run_writes_nothing(test_config, bucket_mgr, tmp_path):
    """dry-run 报告数量但不落盘。"""
    await _seed_all_types(bucket_mgr)
    engine = BackupEngine(test_config, bucket_mgr)
    export = await engine.build_export()

    restored_dir = _fresh_store(tmp_path)
    result = restore_from_export(export, restored_dir, dry_run=True)
    assert result["restored"] == 4

    md_files = [
        f for root, _, files in os.walk(restored_dir) for f in files if f.endswith(".md")
    ]
    assert md_files == [], "dry-run must not write files"


def test_unknown_schema_rejected(tmp_path):
    """schema 不认识时直接拒绝，避免误恢复不兼容格式。"""
    with pytest.raises(ValueError):
        restore_from_export({"schema": "something-else/v9", "buckets": []}, str(tmp_path))
