# ============================================================
# Module: Restore From Backup (restore_from_backup.py)
# 模块：从备份恢复全库
#
# Reads a backup JSON produced by backup_engine (schema
# ombre-brain-backup/v1) and writes every bucket back into the
# store as .md files, preserving all metadata (ids, timestamps,
# emotion coordinates, pinned flags, archived_at, ...).
# 读取 backup_engine 导出的 JSON（schema ombre-brain-backup/v1），
# 把每个桶原样写回存储目录，完整保留元数据（id、时间戳、情绪坐标、
# 钉选标记、archived_at 等）。
#
# Usage / 用法:
#   python restore_from_backup.py backups/backup-2026-07-01.json --dry-run
#   python restore_from_backup.py backups/backup-2026-07-01.json
#   python restore_from_backup.py backups/backup-2026-07-01.json --overwrite
#
# Behavior / 行为:
#   - Default: skip buckets whose id already exists in the store
#     (safe incremental restore).
#     默认跳过库里已存在的同 id 桶（安全增量恢复）。
#   - --overwrite: replace existing buckets with the backup version.
#     --overwrite 时用备份版本覆盖已存在的桶。
#   - --dry-run: report what would happen, write nothing.
#     --dry-run 只报告将发生什么，不写任何文件。
#   - Embeddings are NOT restored (they're a derived cache); run
#     backfill_embeddings.py afterwards if embedding is enabled.
#     不恢复向量（属于派生缓存）；开启 embedding 时恢复后跑一次
#     backfill_embeddings.py 即可。
# ============================================================

import os
import sys
import json
import argparse
import logging

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import frontmatter

from utils import load_config, sanitize_name, safe_path

logger = logging.getLogger("ombre_brain.restore")

SUPPORTED_SCHEMAS = ("ombre-brain-backup/v1",)


def _target_dir(buckets_dir: str, metadata: dict) -> str:
    """Mirror bucket_manager's directory layout rules.
    镜像 bucket_manager 的目录规则：类型 + 主域决定落盘位置。"""
    btype = metadata.get("type", "dynamic")
    domain = metadata.get("domain") or []
    if btype == "feel":
        subdir, primary = "feel", "沉淀物"
    elif btype == "archived":
        subdir = "archive"
        primary = sanitize_name(domain[0]) if domain else "未分类"
    elif btype == "permanent" or metadata.get("pinned"):
        subdir = "permanent"
        primary = sanitize_name(domain[0]) if domain else "未分类"
    else:
        subdir = "dynamic"
        primary = sanitize_name(domain[0]) if domain else "未分类"
    return os.path.join(buckets_dir, subdir, primary)


def _find_existing(buckets_dir: str, bucket_id: str) -> str | None:
    """Search the whole store for a file belonging to bucket_id.
    在整个存储目录里找该 id 的既有文件。"""
    for root, _, files in os.walk(buckets_dir):
        # 跳过备份仓库工作区等隐藏目录
        if os.sep + "." in root + os.sep:
            continue
        for fn in files:
            if not fn.endswith(".md"):
                continue
            if fn == f"{bucket_id}.md" or fn.endswith(f"_{bucket_id}.md"):
                return os.path.join(root, fn)
    return None


def restore_from_export(export: dict, buckets_dir: str,
                        overwrite: bool = False, dry_run: bool = False) -> dict:
    """
    Restore all buckets from an export payload into buckets_dir.
    把导出数据里的所有桶恢复到 buckets_dir。

    Returns / 返回: {"restored": n, "skipped": n, "overwritten": n, "failed": n}
    """
    schema = export.get("schema", "")
    if schema not in SUPPORTED_SCHEMAS:
        raise ValueError(f"不支持的备份格式: {schema!r}（支持: {SUPPORTED_SCHEMAS}）")

    buckets = export.get("buckets", [])
    result = {"restored": 0, "skipped": 0, "overwritten": 0, "failed": 0}

    for b in buckets:
        bucket_id = str(b.get("id", "")).strip()
        metadata = b.get("metadata") or {}
        content = b.get("content", "") or ""
        if not bucket_id:
            result["failed"] += 1
            logger.warning("跳过缺少 id 的桶记录")
            continue

        try:
            existing = _find_existing(buckets_dir, bucket_id)
            if existing and not overwrite:
                result["skipped"] += 1
                continue

            target_dir = _target_dir(buckets_dir, metadata)
            name = metadata.get("name", "")
            if name and name != bucket_id:
                filename = f"{sanitize_name(name)}_{bucket_id}.md"
            else:
                filename = f"{bucket_id}.md"

            if dry_run:
                result["overwritten" if existing else "restored"] += 1
                continue

            os.makedirs(target_dir, exist_ok=True)
            file_path = safe_path(target_dir, filename)
            post = frontmatter.Post(content, **metadata)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(frontmatter.dumps(post))

            # 覆盖时旧文件可能在别的目录/文件名下，写完新文件后移除旧的
            if existing and os.path.normpath(existing) != os.path.normpath(str(file_path)):
                os.remove(existing)
            result["overwritten" if existing else "restored"] += 1
        except Exception as e:
            result["failed"] += 1
            logger.error(f"恢复桶失败 {bucket_id}: {e}")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="从备份 JSON 恢复 Ombre Brain 全库")
    parser.add_argument("backup_file", help="backup_engine 导出的 JSON 文件路径")
    parser.add_argument("--buckets-dir", default="",
                        help="存储目录（默认读 config 的 buckets_dir）")
    parser.add_argument("--overwrite", action="store_true",
                        help="覆盖库里已存在的同 id 桶（默认跳过）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只报告将发生什么，不写任何文件")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    with open(args.backup_file, encoding="utf-8") as f:
        export = json.load(f)

    buckets_dir = args.buckets_dir or load_config()["buckets_dir"]
    print(f"备份文件: {args.backup_file}")
    print(f"  schema: {export.get('schema')}  导出时间: {export.get('exported_at')}")
    print(f"  桶数量: {export.get('bucket_count', len(export.get('buckets', [])))}")
    print(f"恢复到: {buckets_dir}  (overwrite={args.overwrite} dry_run={args.dry_run})")

    result = restore_from_export(
        export, buckets_dir, overwrite=args.overwrite, dry_run=args.dry_run,
    )

    prefix = "[dry-run] 将" if args.dry_run else "已"
    print(
        f"{prefix}恢复 {result['restored']} 个桶，"
        f"覆盖 {result['overwritten']} 个，跳过 {result['skipped']} 个，"
        f"失败 {result['failed']} 个。"
    )
    if not args.dry_run and result["restored"] + result["overwritten"] > 0:
        print("提示：向量索引不随备份恢复，开启 embedding 时请再跑一次 backfill_embeddings.py")
    return 1 if result["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
