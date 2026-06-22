# ============================================================
# Module: Daily Backup Engine (backup_engine.py)
# 模块：每日全库备份引擎
#
# Exports the entire memory store (all buckets + archive + feel +
# emotion coordinates) to a single JSON file, then commits and pushes
# it to a separate private Git repository, keeping every historical
# version (one file per day, named backup-YYYY-MM-DD.json).
# 将整个记忆库（所有桶 + 归档 + feel + 情绪坐标）导出为单个 JSON
# 文件，commit 并 push 到独立的私有 Git 仓库，保留全部历史版本
# （每天一个文件，命名 backup-YYYY-MM-DD.json）。
#
# Scheduling: a background loop wakes once per day at OMBRE_BACKUP_TIME
# (default 00:10) and runs one backup cycle.
# 调度：后台循环每天在 OMBRE_BACKUP_TIME（默认 00:10）醒来执行一次备份。
#
# IMPORTANT — git add scope:
# 重要 —— git add 范围：
#   Only the backup subdirectory is staged (`git add -- <subdir>`).
#   The workflow files (.github/workflows/*) of the backup repo are
#   never re-staged, otherwise GitHub Actions' default GITHUB_TOKEN
#   (which lacks the `workflow` scope) would reject the push.
#   只暂存备份子目录，绝不把仓库里的 workflow 文件纳入提交，否则
#   GitHub Actions 默认 token（无 workflow 权限）会拒绝 push。
#
# Depended on by: server.py
# 被谁依赖：server.py
# ============================================================

import os
import json
import time
import shutil
import asyncio
import logging
import subprocess
from datetime import datetime, timedelta

logger = logging.getLogger("ombre_brain.backup")


class BackupEngine:
    """
    Daily full-store backup engine.
    每日全库备份引擎。

    Configuration via environment variables / 通过环境变量配置：
      OMBRE_BACKUP_TOKEN    GitHub PAT with `repo` scope (required) / 推送用的
                            GitHub 个人访问令牌（必填，需 repo 权限）。
                            Falls back to GITHUB_TOKEN if unset.
      OMBRE_BACKUP_REPO     "owner/name" of the backup repo (default
                            xinyi010524-blip/ob-backup).
      OMBRE_BACKUP_BRANCH   Target branch (default main).
      OMBRE_BACKUP_SUBDIR   Subdir inside the repo where JSON files live,
                            and the ONLY path staged by git add (default backups).
      OMBRE_BACKUP_TIME     Daily run time "HH:MM" (default 00:10).
      OMBRE_BACKUP_WORKDIR  Local clone working dir (default
                            {buckets_dir}/.ob-backup-repo, which survives
                            restarts on a persistent disk).
      OMBRE_BACKUP_GIT_NAME / OMBRE_BACKUP_GIT_EMAIL  Commit identity.
    """

    def __init__(self, config: dict, bucket_mgr):
        self.config = config
        self.bucket_mgr = bucket_mgr
        self.buckets_dir = config["buckets_dir"]

        self.repo = os.environ.get("OMBRE_BACKUP_REPO", "xinyi010524-blip/ob-backup").strip()
        self.token = (
            os.environ.get("OMBRE_BACKUP_TOKEN", "").strip()
            or os.environ.get("GITHUB_TOKEN", "").strip()
        )
        self.branch = os.environ.get("OMBRE_BACKUP_BRANCH", "main").strip() or "main"
        self.backup_subdir = os.environ.get("OMBRE_BACKUP_SUBDIR", "backups").strip() or "backups"
        self.git_name = os.environ.get("OMBRE_BACKUP_GIT_NAME", "Ombre Brain Backup").strip() or "Ombre Brain Backup"
        self.git_email = (
            os.environ.get("OMBRE_BACKUP_GIT_EMAIL", "").strip()
            or "ombre-backup@users.noreply.github.com"
        )

        default_workdir = os.path.join(self.buckets_dir, ".ob-backup-repo")
        self.workdir = os.environ.get("OMBRE_BACKUP_WORKDIR", "").strip() or default_workdir

        # --- Parse daily run time "HH:MM" / 解析每日运行时间 ---
        run_at = os.environ.get("OMBRE_BACKUP_TIME", "00:10").strip() or "00:10"
        try:
            hh, mm = run_at.split(":")
            self.run_hour = max(0, min(23, int(hh)))
            self.run_minute = max(0, min(59, int(mm)))
        except Exception:
            logger.warning(f"OMBRE_BACKUP_TIME='{run_at}' 不合法，回退到 00:10")
            self.run_hour, self.run_minute = 0, 10

        # --- Background task control / 后台任务控制 ---
        self._task: asyncio.Task | None = None
        self._running = False
        self._lock = asyncio.Lock()
        self._last_result: dict | None = None

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def configured(self) -> bool:
        """True if a token + repo are set, so a push can actually happen.
        是否已配置好令牌和仓库（能真正 push）。"""
        return bool(self.token and self.repo)

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    # ---------------------------------------------------------
    # Build the full export payload
    # 构建全库导出数据
    # ---------------------------------------------------------
    async def build_export(self) -> dict:
        """
        Snapshot the entire store: all buckets (permanent / dynamic / feel),
        the archive, and per-bucket emotion coordinates (valence/arousal live
        in each bucket's metadata), plus aggregate stats.
        全库快照：所有桶（固化/动态/feel）+ 归档 + 每个桶的情绪坐标
        （valence/arousal 在 metadata 里）+ 统计信息。
        """
        all_buckets = await self.bucket_mgr.list_all(include_archive=True)
        try:
            stats = await self.bucket_mgr.get_stats()
        except Exception as e:
            logger.warning(f"get_stats failed during export / 导出统计失败: {e}")
            stats = {}

        buckets_out = []
        for b in all_buckets:
            buckets_out.append({
                "id": b.get("id"),
                "metadata": b.get("metadata", {}),
                "content": b.get("content", ""),
            })

        now = datetime.now()
        return {
            "schema": "ombre-brain-backup/v1",
            "exported_at": now.isoformat(),
            "date": now.strftime("%Y-%m-%d"),
            "bucket_count": len(buckets_out),
            "stats": stats,
            "buckets": buckets_out,
        }

    # ---------------------------------------------------------
    # Public: run one backup cycle (export → commit → push)
    # 对外：执行一次备份（导出 → 提交 → 推送）
    # ---------------------------------------------------------
    async def run_backup(self) -> dict:
        """
        Export the store and push it to the backup repo. Serialized with a
        lock so the scheduler and manual trigger never collide.
        导出并推送到备份仓库。用锁串行化，避免定时任务和手动触发撞车。
        """
        async with self._lock:
            if not self.configured:
                raise RuntimeError(
                    "备份未配置：请设置 OMBRE_BACKUP_TOKEN（以及可选的 OMBRE_BACKUP_REPO）"
                )

            export = await self.build_export()
            date_str = export["date"]
            filename = f"backup-{date_str}.json"
            payload = json.dumps(export, ensure_ascii=False, indent=2, default=str)

            # Git work is blocking (subprocess + network) → run off the event loop.
            # Git 操作是阻塞的（子进程 + 网络）→ 放到线程里跑，不阻塞事件循环。
            result = await asyncio.to_thread(
                self._commit_and_push_sync, filename, payload, date_str
            )
            result["bucket_count"] = export["bucket_count"]
            result["timestamp"] = time.time()
            self._last_result = result
            logger.info(f"Backup complete / 备份完成: {result}")
            return result

    # ---------------------------------------------------------
    # Git helpers (sync, run inside asyncio.to_thread)
    # Git 辅助方法（同步，放在 to_thread 里调用）
    # ---------------------------------------------------------
    def _authed_url(self) -> str:
        return f"https://{self.token}@github.com/{self.repo}.git"

    def _sanitize(self, text: str) -> str:
        """Strip the token out of any text before logging / 日志脱敏，去掉令牌。"""
        if self.token and text:
            return text.replace(self.token, "***")
        return text

    def _run_git(self, args: list[str], cwd: str, timeout: int = 120) -> str:
        proc = subprocess.run(
            ["git"] + args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            detail = (proc.stderr.strip() or proc.stdout.strip())
            raise RuntimeError(f"git {' '.join(args)} failed: {self._sanitize(detail)}")
        return proc.stdout.strip()

    def _configure_identity(self) -> None:
        self._run_git(["config", "user.name", self.git_name], cwd=self.workdir)
        self._run_git(["config", "user.email", self.git_email], cwd=self.workdir)

    def _ensure_repo_sync(self) -> None:
        """
        Make sure self.workdir is a clone of the backup repo synced to the
        latest remote branch. Handles fresh clone, re-sync, and empty repo.
        确保 workdir 是备份仓库的克隆，并同步到远端最新分支。
        处理首次克隆、重新同步、空仓库三种情况。
        """
        git_dir = os.path.join(self.workdir, ".git")

        if os.path.isdir(git_dir):
            self._configure_identity()
            try:
                self._run_git(["remote", "set-url", "origin", self._authed_url()], cwd=self.workdir)
                self._run_git(["fetch", "origin", self.branch], cwd=self.workdir)
                # Switch to the branch (create a local tracking branch if needed).
                try:
                    self._run_git(["checkout", self.branch], cwd=self.workdir)
                except RuntimeError:
                    self._run_git(["checkout", "-B", self.branch, f"origin/{self.branch}"], cwd=self.workdir)
                self._run_git(["reset", "--hard", f"origin/{self.branch}"], cwd=self.workdir)
            except RuntimeError as e:
                # Remote branch may not exist yet (empty repo) — keep local state.
                logger.warning(f"备份仓库同步失败，沿用本地状态: {self._sanitize(str(e))}")
            return

        # --- Fresh working dir: clone, or init if the remote repo is empty ---
        parent = os.path.dirname(os.path.abspath(self.workdir)) or "."
        os.makedirs(parent, exist_ok=True)
        if os.path.exists(self.workdir):
            shutil.rmtree(self.workdir)

        try:
            self._run_git(["clone", self._authed_url(), self.workdir], cwd=parent)
            self._configure_identity()
            try:
                self._run_git(["checkout", self.branch], cwd=self.workdir)
            except RuntimeError:
                self._run_git(["checkout", "-b", self.branch], cwd=self.workdir)
        except RuntimeError as e:
            # Likely an empty repo (no commits to clone) — initialize locally.
            logger.warning(f"克隆失败（可能是空仓库），改为本地初始化: {self._sanitize(str(e))}")
            os.makedirs(self.workdir, exist_ok=True)
            self._run_git(["init"], cwd=self.workdir)
            self._run_git(["checkout", "-b", self.branch], cwd=self.workdir)
            self._run_git(["remote", "add", "origin", self._authed_url()], cwd=self.workdir)
            self._configure_identity()

    def _push_with_retry(self) -> None:
        """Push current branch, retrying network failures with backoff.
        推送当前分支，网络失败时指数退避重试。"""
        delays = [2, 4, 8, 16]
        last_err: Exception | None = None
        for attempt in range(len(delays) + 1):
            try:
                self._run_git(["push", "-u", "origin", self.branch], cwd=self.workdir, timeout=180)
                return
            except RuntimeError as e:
                last_err = e
                if attempt < len(delays):
                    wait = delays[attempt]
                    logger.warning(
                        f"push 失败，{wait}s 后重试 ({attempt + 1}/{len(delays)}): "
                        f"{self._sanitize(str(e))}"
                    )
                    time.sleep(wait)
        raise RuntimeError(f"push 重试耗尽: {self._sanitize(str(last_err))}")

    def _commit_and_push_sync(self, filename: str, payload: str, date_str: str) -> dict:
        self._ensure_repo_sync()

        backup_dir = os.path.join(self.workdir, self.backup_subdir)
        os.makedirs(backup_dir, exist_ok=True)
        file_path = os.path.join(backup_dir, filename)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(payload)

        # --- Stage ONLY the backup subdir / 只暂存备份子目录 ---
        # Never `git add -A` / `git add .` — that could stage the repo's
        # .github/workflows/* files, which the default GitHub Actions token
        # is not allowed to push.
        # 绝不用 `git add -A` / `git add .`，那会把仓库里的 workflow 文件
        # 一起暂存，而默认 GitHub Actions token 无权推送它们。
        self._run_git(["add", "--", self.backup_subdir], cwd=self.workdir)

        # --- Nothing changed? skip the commit / 无改动则跳过提交 ---
        status = self._run_git(
            ["status", "--porcelain", "--", self.backup_subdir], cwd=self.workdir
        )
        if not status.strip():
            return {
                "pushed": False,
                "reason": "no_changes",
                "file": f"{self.backup_subdir}/{filename}",
            }

        self._run_git(["commit", "-m", f"backup: {date_str}"], cwd=self.workdir)
        self._push_with_retry()
        commit = self._run_git(["rev-parse", "HEAD"], cwd=self.workdir)

        return {
            "pushed": True,
            "file": f"{self.backup_subdir}/{filename}",
            "repo": self.repo,
            "branch": self.branch,
            "commit": commit,
        }

    # ---------------------------------------------------------
    # Background scheduler (daily at HH:MM)
    # 后台调度器（每天 HH:MM）
    # ---------------------------------------------------------
    def _seconds_until_next_run(self) -> float:
        now = datetime.now()
        target = now.replace(
            hour=self.run_hour, minute=self.run_minute, second=0, microsecond=0
        )
        if target <= now:
            target += timedelta(days=1)
        return max(1.0, (target - now).total_seconds())

    async def ensure_started(self) -> None:
        """Lazy-start the scheduler on first call.
        懒加载：首次调用时启动调度器。"""
        if not self._running:
            await self.start()

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        if self.configured:
            logger.info(
                f"Backup scheduler started, daily at "
                f"{self.run_hour:02d}:{self.run_minute:02d} → {self.repo} / "
                f"备份调度已启动，每天 {self.run_hour:02d}:{self.run_minute:02d} 推送到 {self.repo}"
            )
        else:
            logger.warning(
                "Backup scheduler started but OMBRE_BACKUP_TOKEN is not set — "
                "backups will be skipped until configured / "
                "备份调度已启动，但未设置 OMBRE_BACKUP_TOKEN，配置前不会执行备份"
            )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Backup scheduler stopped / 备份调度已停止")

    async def _background_loop(self) -> None:
        while self._running:
            delay = self._seconds_until_next_run()
            logger.info(
                f"Next backup in {delay / 3600:.2f}h / "
                f"下次备份在 {delay / 3600:.2f} 小时后"
            )
            try:
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            if not self.configured:
                logger.warning("到点但未配置 OMBRE_BACKUP_TOKEN，跳过本次备份")
            else:
                try:
                    await self.run_backup()
                except Exception as e:
                    logger.error(f"Scheduled backup failed / 定时备份失败: {e}")
            # Sleep past the trigger minute so we don't double-fire.
            # 睡过触发分钟，避免同一分钟内重复触发。
            try:
                await asyncio.sleep(61)
            except asyncio.CancelledError:
                break
