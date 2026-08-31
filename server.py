# ============================================================
# Module: MCP Server Entry Point (server.py)
# 模块：MCP 服务器主入口
#
# Starts the Ombre Brain MCP service and registers memory
# operation tools for Claude to call.
# 启动 Ombre Brain MCP 服务，注册记忆操作工具供 Claude 调用。
#
# Core responsibilities:
# 核心职责：
#   - Initialize config, bucket manager, dehydrator, decay engine
#     初始化配置、记忆桶管理器、脱水器、衰减引擎
#   - Expose 6 MCP tools:
#     暴露 6 个 MCP 工具：
#       breath — Surface pinned buckets + recent archived session summaries, or search by keyword
#                浮现钉选桶 + 最近归档的会话总结，或按关键词检索
#       hold   — Store a single memory (or write a `feel` reflection)
#                存储单条记忆（或写 feel 反思）
#       grow   — Diary digest, auto-split into multiple buckets
#                日记归档，自动拆分多桶
#       trace  — Modify metadata / resolved / delete
#                修改元数据 / resolved 标记 / 删除
#       pulse  — System status + bucket listing
#                系统状态 + 所有桶列表
#       dream  — Surface recent dynamic buckets for self-digestion
#                返回最近桶 供模型自省/写 feel
#
# Startup:
# 启动方式：
#   Local:  python server.py
#   Remote: OMBRE_TRANSPORT=streamable-http python server.py
#   Docker: docker-compose up
# ============================================================

import os
import sys
import re
import random
import logging
import asyncio
import hashlib
import hmac
import secrets
import time
import json as _json_lib
import httpx


# --- Ensure same-directory modules can be imported ---
# --- 确保同目录下的模块能被正确导入 ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from bucket_manager import BucketManager
from dehydrator import Dehydrator
from decay_engine import DecayEngine
from digest_engine import DigestEngine
from maintenance import backfill_mood, backfill_related
from embedding_engine import EmbeddingEngine
from import_memory import ImportEngine
from backup_engine import BackupEngine
from utils import load_config, setup_logging, strip_wikilinks, count_tokens_approx, now_iso, now_local
from datetime import timedelta, datetime, date

# 矛盾检测的退化通道要拿正文对正文比字面相似度
from rapidfuzz import fuzz

# --- Load config & init logging / 加载配置 & 初始化日志 ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("ombre_brain")

# --- Runtime env vars (port + webhook) / 运行时环境变量 ---
# OMBRE_PORT: HTTP/SSE 监听端口，默认 8000
try:
    OMBRE_PORT = int(os.environ.get("OMBRE_PORT", "8000") or "8000")
except ValueError:
    logger.warning("OMBRE_PORT 不是合法整数，回退到 8000")
    OMBRE_PORT = 8000

# OMBRE_HOOK_URL: 在 breath/dream 被调用后推送事件到该 URL（POST JSON）。
# OMBRE_HOOK_SKIP: 设为 true/1/yes 跳过推送。
# 详见 ENV_VARS.md。
OMBRE_HOOK_URL = os.environ.get("OMBRE_HOOK_URL", "").strip()
OMBRE_HOOK_SKIP = os.environ.get("OMBRE_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
# 无参 breath / 唤醒 / breath-hook 里"最近记下"(hold 写入的动态桶)浮现条数,0 = 关闭
BREATH_RECENT_N = int(os.environ.get("BREATH_RECENT_N", "3") or "3")
# 唤醒时归档桶的呈现方式,默认 raw(原文)。
# 为什么默认给原文而不是脱水:归档桶的内容是 archive_session 写下的「给下一个自己的信」,
# 本身就是刻意精炼过的产物。再走一次 dehydrate(>100token 就送 LLM 压缩)等于二次摘要,
# 会把最该保留的语气与细节磨平——正是"老内容被反复摘要越来越糊"那个老问题。
# 想退回旧行为:设 BREATH_WAKE_ARCHIVE_MODE=summary(单行)或 full(脱水)。
BREATH_WAKE_ARCHIVE_MODE = (os.environ.get("BREATH_WAKE_ARCHIVE_MODE") or "raw").strip().lower()
# 唤醒时浮现几条归档。三条路径(无query breath / wake+startup / breath-hook)统一用它,
# 免得改了一处忘了另两处。改成原文呈现后每条约 350~1900 token,条数直接决定开窗成本:
# 5 条≈6100、3 条≈4600(预算 10000)。想省 token 或觉得摊太开就调小,想要更长的连续性就调大。
BREATH_ARCHIVE_N = int(os.environ.get("BREATH_ARCHIVE_N", "5") or "5")
# 归档按天合并:同一天只有一个档案桶,当天再次归档往里追加一节(而不是每次新建一个)。
# 每次新建会让一天碎成好几份 —— 浮现时既占「最近 N 条」的名额,读起来也不连贯。
# 合并后「最近 N 条归档」≈ 最近 N 天。设 0 退回旧行为(每次一个新桶)。
ARCHIVE_MERGE_BY_DAY = (os.environ.get("ARCHIVE_MERGE_BY_DAY", "1") or "1").strip() not in ("0", "false", "False")
# 单条归档原文的 token 上限,防止某一条异常大的桶吃光预算(超出截断并标注)。
# 3000 是照真实数据定的:实测归档普遍 350~1900 token,五条全展开约 6000,
# 预算 10000 放得下。初版设 1200 太紧,砍掉了最长两条的尾巴——而归档的
# 「## 亮点 / ## 心情」恰好写在末尾,截断等于精准切掉最该看到的情绪总结。
# 这里只作异常防护,正常归档不该触发;真正的总量约束是外层 token_budget。
BREATH_RAW_MAX_TOKENS = int(os.environ.get("BREATH_RAW_MAX_TOKENS", "3000") or "3000")
# 唤醒(无 query 的 breath / wake / startup / breath-hook)一次浮现的 token 总预算。
# 原来写死 10000。按天合并之后一个归档桶 = 一整天,忙的一天就能顶掉大半预算,
# 于是更早的天被整条挤掉。做成可调:想让他看全就调大,想省 token 就调小。
# ⚠️ 这是**每开一个新窗口的一次性成本**(之后每轮按缓存价重读),调大要心里有数。
BREATH_WAKE_BUDGET = int(os.environ.get("BREATH_WAKE_BUDGET", "10000") or "10000")
# feel() 认定「相关」的相似度门槛。定高一点是有意的:feel 是他留下的痕迹,
# 拿低相关的凑数比返回「没有」更糟——他会把不相干的感受当成自己以前的想法。
# 宁可空手而归。命中率太低就调低(0.5~0.6),老是翻出不相干的就调高。
FEEL_SIM_THRESHOLD = float(os.environ.get("FEEL_SIM_THRESHOLD", "0.65") or "0.65")
# feel() 一次返回的 token 上限。feel 逐字返回不摘要,长了会挤占对话窗口。
FEEL_MAX_TOKENS = int(os.environ.get("FEEL_MAX_TOKENS", "4000") or "4000")
# dream() 的「旧事重提」通道:每次做梦额外捞几条**很久没被想起**的旧记忆。
# 为什么需要它:dream 原本只看最近新增的 5 个桶 —— 三个月前的事除非他专门去搜,
# 否则永远浮不上来。而「忘了很久又突然想起」恰恰是记忆最像人的地方。
# 设 0 = 整条通道关闭(连重提标记都不写)。
RESURFACE_N = int(os.environ.get("RESURFACE_N", "2") or "2")
# 多久没被想起才算「旧事」。太短会把上周的事当旧事翻出来,太长则几乎不触发。
RESURFACE_MIN_IDLE_DAYS = int(os.environ.get("RESURFACE_MIN_IDLE_DAYS", "30") or "30")
# 同一件旧事重提后多少天内不再翻出来 —— 防止连着几轮都在念叨同一件事。
RESURFACE_COOLDOWN_DAYS = int(os.environ.get("RESURFACE_COOLDOWN_DAYS", "14") or "14")
# 旧事重提给多少字的正文摘录(0=只给标题行)。给一点是必要的:只有标题行
# 他认不出这是哪件事,等于没重提;全文又太贵,要细节他可以 dream(detail_ids=)。
RESURFACE_EXCERPT_CHARS = int(os.environ.get("RESURFACE_EXCERPT_CHARS", "200") or "200")
# 重提的最低门槛(见 _resurface_candidates 的打分)。没够这个分就一条都不提 ——
# 和 feel 同一个道理:**宁可空手而归,也别为了凑数把「那天买了瓶酱油」翻出来。**
# 分的量级参考:重要度9+情绪强+一年没想起≈17;重要度4+平淡+半年≈2.6。
RESURFACE_MIN_SCORE = float(os.environ.get("RESURFACE_MIN_SCORE", "4.0") or "4.0")
# --- 存入时的矛盾检测 / conflict detection on hold ---
# 每次 hold 新内容时,捞几条语义最近的旧桶比对日期/数字/关键事实。
# 检测到冲突**不改任何东西**,只在返回里附警告,怎么处理交给她和他。
# 设 0 = 整条通道关闭。
CONFLICT_CHECK_N = int(os.environ.get("CONFLICT_CHECK_N", "3") or "3")
# 相似度门槛:只有够像的桶才拿来比。调低会误报泛滥(两件不相干的事各有各的日期,
# 一比一个"矛盾"),这就是为什么这个值定得比检索用的 0.5 高得多。
CONFLICT_MIN_SIM = float(os.environ.get("CONFLICT_MIN_SIM", "0.78") or "0.78")
# 向量通道不可用时的退化门槛:两段正文的**字面相似度**(rapidfuzz,0~1)。
# 不能拿 search() 的百分制分数或 _calc_topic_score 当门槛 —— 那些是「查询词
# 对整个桶(名字/域/标签/正文加权)」的分,「同一句话只差一个日期」也只有 0.35,
# 拿它卡门槛会把真冲突全漏掉。这里要的是正文对正文,所以直接比两段文本。
CONFLICT_KEYWORD_MIN = float(os.environ.get("CONFLICT_KEYWORD_MIN", "0.75") or "0.75")
# 一次 hold 最多做几次 LLM 核对(每次一个 API 调用,控制成本和延迟)。
CONFLICT_MAX_LLM = int(os.environ.get("CONFLICT_MAX_LLM", "2") or "2")
# --- 新桶自动关联 / auto-link on hold ---
# 新桶入库时按语义相似度自动挂上既有桶。封存(归档/休眠/过期便利贴)不参与:
# 关联是给「还活着的记忆」用的路标,指向已经沉下去的东西只会把他带回坟场。
AUTOLINK_ON_HOLD = os.environ.get("AUTOLINK_ON_HOLD", "1") != "0"
AUTOLINK_TOP_K = int(os.environ.get("AUTOLINK_TOP_K", "3") or "3")
AUTOLINK_MIN_SIM = float(os.environ.get("AUTOLINK_MIN_SIM", "0.55") or "0.55")


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to OMBRE_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    if OMBRE_HOOK_SKIP or not OMBRE_HOOK_URL:
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(OMBRE_HOOK_URL, json=body)
    except Exception as e:
        logger.warning(f"Webhook push failed ({event} → {OMBRE_HOOK_URL}): {e}")

# --- Initialize core components / 初始化核心组件 ---
embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager / 记忆桶管理器
dehydrator = Dehydrator(config)                      # Dehydrator / 脱水器
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine / 衰减引擎
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine / 导入引擎
backup_engine = BackupEngine(config, bucket_mgr)     # Daily backup engine / 每日备份引擎
digest_engine = DigestEngine(config, bucket_mgr, embedding_engine, dehydrator)  # Auto-digest / 自动消化引擎

# --- Create MCP server instance / 创建 MCP 服务器实例 ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
# stateless_http + json_response(默认开,OMBRE_STATELESS=0 关闭):
# 每次调用独立请求、无会话可失效——服务器重启后,常驻客户端(claude -p)手里的
# 旧连接不再变成死连接,不用重启对话进程就能继续用记忆工具。
_STATELESS = os.environ.get("OMBRE_STATELESS", "1").strip().lower() not in ("0", "false", "no", "off")
mcp = FastMCP(
    "Ombre Brain",
    host="0.0.0.0",
    port=OMBRE_PORT,
    stateless_http=_STATELESS,
    json_response=_STATELESS,
)


# =============================================================
# Dashboard Auth — simple cookie-based session auth
# Dashboard 认证 —— 基于 Cookie 的会话认证
#
# Env var OMBRE_DASHBOARD_PASSWORD overrides file-stored password.
# First visit with no password set → forced setup wizard.
# Sessions stored in memory (lost on restart, 7-day expiry).
# =============================================================
_sessions: dict[str, float] = {}  # {token: expiry_timestamp}


def _get_auth_file() -> str:
    return os.path.join(config["buckets_dir"], ".dashboard_auth.json")


def _load_password_hash() -> str | None:
    try:
        auth_file = _get_auth_file()
        if os.path.exists(auth_file):
            with open(auth_file, "r", encoding="utf-8") as f:
                return _json_lib.load(f).get("password_hash")
    except Exception:
        pass
    return None


def _save_password_hash(password: str) -> None:
    salt = secrets.token_hex(16)
    h = hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    auth_file = _get_auth_file()
    os.makedirs(os.path.dirname(auth_file), exist_ok=True)
    with open(auth_file, "w", encoding="utf-8") as f:
        _json_lib.dump({"password_hash": f"{salt}:{h}"}, f)


def _verify_password_hash(password: str, stored: str) -> bool:
    if ":" not in stored:
        return False
    salt, h = stored.split(":", 1)
    return hmac.compare_digest(
        h, hashlib.sha256(f"{salt}:{password}".encode()).hexdigest()
    )


def _is_setup_needed() -> bool:
    """True if no password is configured (env var or file)."""
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return False
    return _load_password_hash() is None


def _verify_any_password(password: str) -> bool:
    """Check password against env var (first) or stored hash."""
    env_pwd = os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")
    if env_pwd:
        return hmac.compare_digest(password, env_pwd)
    stored = _load_password_hash()
    if not stored:
        return False
    return _verify_password_hash(password, stored)


def _create_session() -> str:
    token = secrets.token_urlsafe(32)
    _sessions[token] = time.time() + 86400 * 7  # 7-day expiry
    return token


def _is_authenticated(request) -> bool:
    token = request.cookies.get("ombre_session")
    if not token:
        return False
    expiry = _sessions.get(token)
    if expiry is None or time.time() > expiry:
        _sessions.pop(token, None)
        return False
    return True


def _require_auth(request):
    """Return JSONResponse(401) if not authenticated, else None."""
    from starlette.responses import JSONResponse
    if not _is_authenticated(request):
        return JSONResponse(
            {"error": "Unauthorized", "setup_needed": _is_setup_needed()},
            status_code=401,
        )
    return None


# --- Auth endpoints ---
@mcp.custom_route("/auth/status", methods=["GET"])
async def auth_status(request):
    """Return auth state (authenticated, setup_needed)."""
    from starlette.responses import JSONResponse
    return JSONResponse({
        "authenticated": _is_authenticated(request),
        "setup_needed": _is_setup_needed(),
    })


@mcp.custom_route("/auth/setup", methods=["POST"])
async def auth_setup_endpoint(request):
    """Initial password setup (only when no password is configured)."""
    from starlette.responses import JSONResponse
    if not _is_setup_needed():
        return JSONResponse({"error": "Already configured"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "").strip()
    if len(password) < 6:
        return JSONResponse({"error": "密码不能少于6位"}, status_code=400)
    _save_password_hash(password)
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


@mcp.custom_route("/auth/login", methods=["POST"])
async def auth_login(request):
    """Login with password."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    password = body.get("password", "")
    if _verify_any_password(password):
        token = _create_session()
        resp = JSONResponse({"ok": True})
        resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
        return resp
    return JSONResponse({"error": "密码错误"}, status_code=401)


@mcp.custom_route("/auth/logout", methods=["POST"])
async def auth_logout(request):
    """Invalidate session."""
    from starlette.responses import JSONResponse
    token = request.cookies.get("ombre_session")
    if token:
        _sessions.pop(token, None)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie("ombre_session")
    return resp


@mcp.custom_route("/auth/change-password", methods=["POST"])
async def auth_change_password(request):
    """Change dashboard password (requires current password)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    if os.environ.get("OMBRE_DASHBOARD_PASSWORD", ""):
        return JSONResponse({"error": "当前使用环境变量密码，请直接修改 OMBRE_DASHBOARD_PASSWORD"}, status_code=400)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    current = body.get("current", "")
    new_pwd = body.get("new", "").strip()
    if not _verify_any_password(current):
        return JSONResponse({"error": "当前密码错误"}, status_code=401)
    if len(new_pwd) < 6:
        return JSONResponse({"error": "新密码不能少于6位"}, status_code=400)
    _save_password_hash(new_pwd)
    _sessions.clear()
    token = _create_session()
    resp = JSONResponse({"ok": True})
    resp.set_cookie("ombre_session", token, httponly=True, samesite="lax", max_age=86400 * 7)
    return resp


# =============================================================
# /health endpoint: lightweight keepalive
# 轻量保活接口
# For Cloudflare Tunnel or reverse proxy to ping, preventing idle timeout
# 供 Cloudflare Tunnel 或反代定期 ping，防止空闲超时断连
# =============================================================
@mcp.custom_route("/", methods=["GET"])
async def root_redirect(request):
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard")


# =============================================================
# 开机自跑一次的存量维护
#
# 栖栖只有手机、进不了容器 —— 让她「去服务器上跑个脚本」等于这两件事永远不会发生。
# 所以服务端自己做一次:补情绪坐标(心境检索要用)、补关联(孤岛桶连上邻居)。
#
# 三重保险:
#   1. 结果写进 {buckets_dir}/.maintenance.json,**每个任务一辈子只跑一次**
#      （标记文件在持久卷上，重新部署不会重跑）
#   2. 出任何错都只记日志:维护失败绝不能影响服务本身
#   3. OMBRE_STARTUP_MAINTENANCE=0 可整个关掉
#
# 两个任务都是幂等的:补过的跳过、已有关联的不覆盖。
# =============================================================
STARTUP_MAINTENANCE = os.environ.get("OMBRE_STARTUP_MAINTENANCE", "1") != "0"
_MAINTENANCE_TASKS = ("mood", "related")
_maintenance_lock = asyncio.Lock()
_maintenance_done = False


def _maintenance_state_path() -> str:
    return os.path.join(config["buckets_dir"], ".maintenance.json")


def _load_maintenance_state() -> dict:
    try:
        with open(_maintenance_state_path(), encoding="utf-8") as f:
            return _json_lib.load(f)
    except (OSError, ValueError):
        return {}


def _save_maintenance_state(state: dict) -> None:
    try:
        with open(_maintenance_state_path(), "w", encoding="utf-8") as f:
            _json_lib.dump(state, f, ensure_ascii=False, indent=2)
    except OSError as e:
        logger.warning(f"Maintenance state save failed / 维护标记写入失败: {e}")


async def _run_startup_maintenance() -> None:
    """跑一次存量维护(每个任务只跑一次)。任何异常都只记日志。"""
    global _maintenance_done
    if not STARTUP_MAINTENANCE or _maintenance_done:
        return
    async with _maintenance_lock:
        if _maintenance_done:
            return
        state = _load_maintenance_state()
        if all(t in state for t in _MAINTENANCE_TASKS):
            _maintenance_done = True
            return

        for task, runner in (
            ("mood", lambda: backfill_mood(bucket_mgr, dehydrator)),
            ("related", lambda: backfill_related(bucket_mgr, embedding_engine)),
        ):
            if task in state:
                continue
            try:
                stats = await runner()
            except Exception as e:
                logger.error(f"Startup maintenance ({task}) failed / 存量维护失败: {e}")
                continue
            # 引擎当时不可用(缺 key / embedding 没开)不算「做过了」——
            # 落了标记就再也不会重试,那件事等于永远没做。下次启动再来一遍。
            if stats.get("error"):
                logger.warning(f"Startup maintenance ({task}) skipped / 跳过,下次再试: {stats['error']}")
                continue
            stats["at"] = now_iso()
            state[task] = stats
            logger.info(f"Startup maintenance ({task}) / 存量维护完成: {stats}")

        if state:
            _save_maintenance_state(state)
        if all(t in state for t in _MAINTENANCE_TASKS):
            _maintenance_done = True


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request):
    from starlette.responses import JSONResponse
    # Lazy-start the daily backup scheduler. In HTTP mode /health is pinged
    # by the keepalive loop every 60s, so this reliably starts the scheduler
    # shortly after boot.
    # 懒启动每日备份调度器：HTTP 模式下 /health 每 60 秒被保活循环 ping，
    # 因此服务启动后不久即可拉起调度器。
    try:
        await backup_engine.ensure_started()
    except Exception as e:
        logger.warning(f"Backup scheduler start failed / 备份调度启动失败: {e}")
    # 消化引擎的定期扫描同理懒启动。默认只演习+记日志，不动数据
    # （真要放手整理得显式设 DIGEST_AUTO_EXECUTE=1）。
    try:
        await digest_engine.ensure_started()
    except Exception as e:
        logger.warning(f"Digest scanner start failed / 消化扫描启动失败: {e}")
    # 存量维护:补情绪坐标 + 补关联，一辈子只跑一次（见 _run_startup_maintenance）。
    # 放后台跑，不拖慢 /health —— 保活循环每 60 秒 ping 一次，超时会被当成服务挂了。
    if STARTUP_MAINTENANCE and not _maintenance_done:
        asyncio.create_task(_run_startup_maintenance())
    try:
        stats = await bucket_mgr.get_stats()
        body = {
            "status": "ok",
            "buckets": stats["permanent_count"] + stats["dynamic_count"],
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "backup_scheduler": "running" if backup_engine.is_running else "stopped",
        }
        # 只报数字，不报任何记忆内容：这是公开端点
        mstate = _load_maintenance_state()
        if mstate:
            body["maintenance"] = {
                k: {kk: vv for kk, vv in v.items() if kk != "dry_run"}
                for k, v in mstate.items() if isinstance(v, dict)
            }
        return JSONResponse(body)
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


# =============================================================
# /breath-hook endpoint: Dedicated hook for SessionStart
# 会话启动专用挂载点
#
# Same selection as the no-query breath: pinned buckets + recent
# archived session summaries + recent hold-written dynamic buckets.
# 与无 query breath 一致：钉选桶 + 最近归档的会话总结 + 最近记下的动态桶。
# =============================================================
@mcp.custom_route("/breath-hook", methods=["GET"])
async def breath_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        HOOK_ARCHIVE_DEFAULT = BREATH_ARCHIVE_N  # 归档条数上限（env BREATH_ARCHIVE_N）
        HOOK_ARCHIVE_MIN = min(2, BREATH_ARCHIVE_N)  # 下限：保底这么多（不超过上限）
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        # pinned
        pinned = [b for b in all_buckets if b["metadata"].get("pinned") or b["metadata"].get("protected")]
        # recent archived session summaries (written by archive_session), by archive time desc
        archived = [b for b in all_buckets if b["metadata"].get("type") == "archived"]
        archived.sort(key=_archived_sort_key, reverse=True)
        archived = archived[:HOOK_ARCHIVE_DEFAULT]

        parts = await _render_pinned(pinned, "full")
        token_budget = BREATH_WAKE_BUDGET
        for r in parts:
            token_budget -= count_tokens_approx(r)
        # 归档给原文(默认 raw):它已是 archive_session 精炼过的「给下一个自己的信」,
        # 再脱水一次会磨平语气与细节。与无 query breath 的唤醒口径保持一致。
        parts += await _render_archived(
            archived, BREATH_WAKE_ARCHIVE_MODE, token_budget, min_keep=HOOK_ARCHIVE_MIN
        )
        # 最近记下的动态桶:单行摘要即可(线索行,细节让他自己 breath 查),不烧脱水 API
        token_budget = BREATH_WAKE_BUDGET - sum(count_tokens_approx(x) for x in parts)
        parts += await _render_archived(
            _recent_dynamic(all_buckets, BREATH_RECENT_N), "summary", token_budget,
            min_keep=1, prefix="📝 [最近记下] ",
        )

        if not parts:
            await _fire_webhook("breath_hook", {"surfaced": 0})
            return PlainTextResponse("")
        body_text = "[Ombre Brain - 记忆浮现]\n" + "\n---\n".join(parts)
        await _fire_webhook("breath_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Breath hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# /dream-hook endpoint: Dedicated hook for Dreaming
# Dreaming 专用挂载点
# =============================================================
@mcp.custom_route("/dream-hook", methods=["GET"])
async def dream_hook(request):
    from starlette.responses import PlainTextResponse
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        candidates = [
            b for b in all_buckets
            if b["metadata"].get("type") not in ("permanent", "feel")
            and not b["metadata"].get("pinned", False)
            and not b["metadata"].get("protected", False)
        ]
        candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        recent = candidates[:10]

        if not recent:
            return PlainTextResponse("")

        parts = []
        for b in recent:
            meta = b["metadata"]
            resolved_tag = "[已解决]" if meta.get("resolved", False) else "[未解决]"
            parts.append(
                f"{meta.get('name', b['id'])} {resolved_tag} "
                f"V{meta.get('valence', 0.5):.1f}/A{meta.get('arousal', 0.3):.1f}\n"
                f"{strip_wikilinks(b['content'][:200])}"
            )

        body_text = "[Ombre Brain - Dreaming]\n" + "\n---\n".join(parts)
        await _fire_webhook("dream_hook", {"surfaced": len(parts), "chars": len(body_text)})
        return PlainTextResponse(body_text)
    except Exception as e:
        logger.warning(f"Dream hook failed: {e}")
        return PlainTextResponse("")


# =============================================================
# Internal helper: merge-or-create
# 内部辅助：检查是否可合并，可以则合并，否则新建
# Shared by hold and grow to avoid duplicate logic
# hold 和 grow 共用，避免重复逻辑
# =============================================================
# =============================================================
# 封存判定 + 自动关联
#
# 「封存」= 已归档 / 休眠 / 过期便利贴 —— 这些桶不该被新桶挂上关联。
# =============================================================
def _is_sealed(meta: dict) -> bool:
    """这个桶是不是已经封存(归档/休眠/过期便利贴),不参与自动关联。"""
    return bool(
        meta.get("type") == "archived"
        or meta.get("dormant")
        or _is_expired(meta)
    )


async def _link_targets(bucket_id: str, top_k: int = None, min_sim: float = None) -> list[str]:
    """给某个桶挑关联对象:语义最近的几个**未封存**桶的 id。

    多捞一些再过滤——直接 top_k 会出现「前3个全是归档桶」于是一个都不剩。
    embedding 不可用时返回空列表(静默,不影响存入)。
    """
    top_k = AUTOLINK_TOP_K if top_k is None else top_k
    min_sim = AUTOLINK_MIN_SIM if min_sim is None else min_sim
    try:
        similar = await embedding_engine.find_similar_buckets(
            bucket_id, top_k=max(top_k * 4, 8), min_sim=min_sim
        )
    except Exception as e:
        logger.warning(f"Auto-link candidate search failed / 关联候选检索失败 {bucket_id}: {e}")
        return []

    picked = []
    for bid, _sim in similar:
        if len(picked) >= top_k:
            break
        try:
            b = await bucket_mgr.get(bid)
        except Exception:
            continue
        if not b or _is_sealed(b["metadata"]) or b["metadata"].get("type") == "feel":
            continue
        picked.append(bid)
    return picked


async def _autolink_new_bucket(bucket_id: str) -> list[str]:
    """新桶入库后按语义相似度自动建立关联。失败静默——关联建不上不该让存入失败。"""
    if not AUTOLINK_ON_HOLD:
        return []
    try:
        targets = await _link_targets(bucket_id)
        if targets:
            await bucket_mgr.set_related(bucket_id, targets, overwrite=False)
        return targets
    except Exception as e:
        logger.warning(f"Auto-link on hold failed / 入库自动关联失败 {bucket_id}: {e}")
        return []


# =============================================================
# 矛盾检测:新内容和哪条旧记忆对不上
#
# 三道闸,一道比一道贵,就是为了别让误报泛滥、也别让成本失控:
#   1. 只取语义/关键词上**足够像**的几条旧桶(不像的根本不比)
#   2. 本地正则先看有没有「日期/数字」这类可对照的硬信号(没有就不问 LLM)
#   3. 剩下的才交给 LLM 逐条核对(最多 CONFLICT_MAX_LLM 次)
# 检测到冲突**不动任何数据**,只在 hold 的返回里附警告。
# =============================================================
_DATE_PAT = re.compile(
    r"\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?|\d{1,2}[-/月]\d{1,2}日?|"
    r"\d{1,2}:\d{2}|\d{1,2}点(?:\d{1,2}分)?"
)
_NUM_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:块|元|万|千|百|个|人|次|天|周|月|年|岁|斤|公斤|米|公里|小时|分钟|%)")


def _fact_signals(text: str) -> set:
    """抽出文本里可对照的硬信号:日期/时刻 + 带单位的数字。"""
    text = text or ""
    return {m.group(0) for m in _DATE_PAT.finditer(text)} | {
        m.group(0) for m in _NUM_PAT.finditer(text)
    }


def _worth_llm_check(new_content: str, old_content: str) -> bool:
    """本地预筛:两边都有硬信号、且不完全一样,才值得花一次 LLM 去核对。

    完全一样 = 讲的是同一个日期/数字,那是复述不是矛盾;
    一边没有 = 没有可对照的东西,LLM 只会去揣摩语气,那正是误报的来源。
    """
    a, b = _fact_signals(new_content), _fact_signals(old_content)
    return bool(a and b and a != b)


async def _conflict_candidates(content: str, domain: list) -> list[dict]:
    """挑比对对象:优先向量语义检索,向量通道不可用时退化到关键词检索。

    退化路径效果稍弱(关键词像不代表说的是同一件事),门槛因此定得更高。
    """
    picked: list[dict] = []
    seen = set()
    try:
        vector_results = await embedding_engine.search_similar(content, top_k=CONFLICT_CHECK_N * 3)
    except Exception as e:
        logger.warning(f"Conflict vector search failed / 矛盾检测向量检索失败: {e}")
        vector_results = []

    for bid, sim in vector_results or []:
        if len(picked) >= CONFLICT_CHECK_N:
            break
        if sim < CONFLICT_MIN_SIM or bid in seen:
            continue
        try:
            b = await bucket_mgr.get(bid)
        except Exception:
            continue
        if not b or b["metadata"].get("type") == "feel" or _is_expired(b["metadata"]):
            continue
        seen.add(bid)
        picked.append(b)

    if picked:
        return picked

    # --- 退化通道:没有向量就按关键词相近度挑 ---
    try:
        matches = await bucket_mgr.search(content, limit=CONFLICT_CHECK_N * 2, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Conflict keyword search failed / 矛盾检测关键词检索失败: {e}")
        return []
    for b in matches:
        if len(picked) >= CONFLICT_CHECK_N:
            break
        old_body = (b.get("content", "") or "")[:2000]
        if fuzz.ratio(content[:2000], old_body) / 100.0 < CONFLICT_KEYWORD_MIN:
            continue
        if b["metadata"].get("type") == "feel" or _is_expired(b["metadata"]):
            continue
        picked.append(b)
    return picked


async def _detect_conflicts(content: str, domain: list) -> list[dict]:
    """返回 [{"id","name","points":[...]}, ...];没冲突返回 []。

    全程不修改任何桶——这是检测,不是纠正。怎么处理由她定。
    """
    if CONFLICT_CHECK_N <= 0 or not content.strip():
        return []
    try:
        candidates = await _conflict_candidates(content, domain)
    except Exception as e:
        logger.warning(f"Conflict candidate pick failed / 矛盾检测选取候选失败: {e}")
        return []

    out = []
    llm_used = 0
    for b in candidates:
        if llm_used >= CONFLICT_MAX_LLM:
            break
        old_content = b.get("content", "") or ""
        if not _worth_llm_check(content, old_content):
            continue
        llm_used += 1
        try:
            points = await dehydrator.check_conflict(content, old_content)
        except Exception as e:
            # 核对不了就当没冲突:矛盾检测是附加提醒,绝不能让它把存入弄失败
            logger.warning(f"Conflict check call failed / 矛盾核对调用失败: {e}")
            continue
        if points:
            out.append({
                "id": b["id"],
                "name": b["metadata"].get("name", b["id"]),
                "points": points,
            })
    return out


def _format_conflicts(conflicts: list[dict]) -> str:
    """把冲突警告拼成给他看的一段话。没冲突返回空串。"""
    if not conflicts:
        return ""
    lines = ["", "⚠️ 这条和已有记忆可能对不上(**没有自动改任何东西**,要不要处理你定):"]
    for c in conflicts:
        lines.append(f"  · 旧桶「{c['name']}」({c['id']})")
        for pt in c["points"]:
            lines.append(f"      - {pt}")
    lines.append("  处理方式:确认新的对 → trace(旧桶id, content=...) 更正;两边都要留 → 不用管。")
    return "\n".join(lines)


async def _merge_or_create(
    content: str,
    tags: list,
    importance: int,
    domain: list,
    valence: float,
    arousal: float,
    name: str = "",
    trigger_date: str = None,
) -> tuple[str, bool, dict]:
    """
    Check if a similar bucket exists for merging; merge if so, create if not.
    Returns (bucket_id_or_name, is_merged, info).
    检查是否有相似桶可合并，有则合并，无则新建。
    返回 (桶ID或名称, 是否合并, info)；info 带 bucket_id 与冲突警告列表。
    """
    # --- 矛盾检测:先看新内容和哪条旧记忆对不上（只检测，不改数据）---
    conflicts = await _detect_conflicts(content, domain)

    try:
        existing = await bucket_mgr.search(content, limit=1, domain_filter=domain or None)
    except Exception as e:
        logger.warning(f"Search for merge failed, creating new / 合并搜索失败，新建: {e}")
        existing = []

    # 检测到冲突就**不合并**:自动合并会把「旧说3月5日/新说3月8日」揉成一段
    # 自相矛盾的正文,连痕迹都不留 —— 那正是矛盾检测要防的事。
    # 冲突时新内容独立成桶,两个说法都在,由她决定留哪个。
    if conflicts and existing:
        logger.info(
            f"Conflict detected, skipping auto-merge / 检测到矛盾,跳过自动合并: "
            f"{[c['id'] for c in conflicts]}"
        )
        existing = []

    if existing and existing[0].get("score", 0) > config.get("merge_threshold", 75):
        bucket = existing[0]
        # --- Never merge into pinned/protected buckets ---
        # --- 不合并到钉选/保护桶 ---
        if not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
            try:
                merged = await dehydrator.merge(bucket["content"], content)
                old_v = bucket["metadata"].get("valence", 0.5)
                old_a = bucket["metadata"].get("arousal", 0.3)
                merged_valence = round((old_v + valence) / 2, 2)
                merged_arousal = round((old_a + arousal) / 2, 2)
                await bucket_mgr.update(
                    bucket["id"],
                    content=merged,
                    tags=list(set(bucket["metadata"].get("tags", []) + tags)),
                    importance=max(bucket["metadata"].get("importance", 5), importance),
                    domain=list(set(bucket["metadata"].get("domain", []) + domain)),
                    valence=merged_valence,
                    arousal=merged_arousal,
                )
                # --- Update embedding after merge ---
                try:
                    await embedding_engine.generate_and_store(bucket["id"], merged)
                except Exception:
                    pass
                if trigger_date:
                    await bucket_mgr.update(bucket["id"], trigger_date=trigger_date)
                return (
                    bucket["metadata"].get("name", bucket["id"]),
                    True,
                    {"bucket_id": bucket["id"], "conflicts": conflicts, "linked": []},
                )
            except Exception as e:
                logger.warning(f"Merge failed, creating new / 合并失败，新建: {e}")

    bucket_id = await bucket_mgr.create(
        content=content,
        tags=tags,
        importance=importance,
        domain=domain,
        valence=valence,
        arousal=arousal,
        name=name or None,
        trigger_date=trigger_date or None,
    )
    # --- Generate embedding for new bucket ---
    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass
    # --- 新桶自动关联:按语义相似度挂上既有桶(封存桶不参与)---
    linked = await _autolink_new_bucket(bucket_id)
    return bucket_id, False, {
        "bucket_id": bucket_id, "conflicts": conflicts, "linked": linked,
    }


def _summary_line(b: dict, prefix: str = "") -> str:
    """单行摘要：bucket_id + 桶名 + 主题 + 情感坐标 + 重要度 + 更新时间。

    省 Token 模式下用它替代脱水全文，每个桶只占一行。
    """
    meta = b.get("metadata", {})
    name = meta.get("name", b["id"])
    domains = ",".join(meta.get("domain", [])) or "-"
    val = meta.get("valence", 0.5)
    aro = meta.get("arousal", 0.3)
    imp = meta.get("importance", "?")
    updated = meta.get("last_active", meta.get("created", "")) or "-"
    return (
        f"{prefix}[bucket_id:{b['id']}] [{name}] "
        f"主题:{domains} 情感:V{val:.1f}/A{aro:.1f} "
        f"重要:{imp} 更新:{updated}"
    )


def _passes_date_filter(meta: dict, date_from: str, date_to: str) -> bool:
    """按桶更新时间(last_active,回退 created)过滤。date_from/date_to 为 YYYY-MM-DD，闭区间。

    无任何日期参数 → 全部通过；桶无时间戳 → 不排除（保守保留）。
    """
    if not date_from and not date_to:
        return True
    ts = str(meta.get("last_active", meta.get("created", "")))[:10]  # 取 YYYY-MM-DD
    if not ts:
        return True
    if date_from and ts < date_from:
        return False
    if date_to and ts > date_to:
        return False
    return True


def _archived_sort_key(b: dict) -> str:
    """归档桶排序键：按真实归档时刻 archived_at 排序。

    存量老桶没有 archived_at 时回退 created——会话总结桶的 created 就是归档
    时刻，可靠；last_active 会被批量操作刷成同一时刻导致顺序随缘，只作最后兜底。
    """
    meta = b["metadata"]
    # or 链而非 get 默认值:空字符串的 archived_at(存量脏数据)也要落到下一级
    return str(meta.get("archived_at") or meta.get("created") or meta.get("last_active") or "")


async def _render_pinned(pinned_buckets: list, mode: str) -> list:
    """钉选桶 → 核心准则条目（summary 单行 / full 脱水全文）。"""
    results = []
    for b in pinned_buckets:
        try:
            if mode == "summary":
                results.append(_summary_line(b, prefix="📌 [核心准则] "))
                continue
            clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
            results.append(f"📌 [核心准则] [bucket_id:{b['id']}] {summary}")
        except Exception as e:
            logger.warning(f"Failed to dehydrate pinned bucket / 钉选桶脱水失败: {e}")
            continue
    return results


def _truncate_to_tokens(text: str, limit: int) -> str:
    """把文本裁到 token 预算内。

    用二分按 count_tokens_approx 实测切点,不用「token×固定比例」估字符数——
    中文 1 字≈1.5token、英文 1 词≈1.3token,中英混排密度差好几倍,固定比例会切不准
    (曾按 ×2 估算,1200 的预算切出来 3158 token)。
    """
    if limit <= 0 or count_tokens_approx(text) <= limit:
        return text
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if count_tokens_approx(text[:mid]) <= limit:
            lo = mid
        else:
            hi = mid - 1
    # 不写「用 xx 取全文」——OB 没有按 bucket_id 读原文的工具:trace 是改不是读,
    # dream(detail_ids) 只覆盖最近新增的桶,breath(query) 走的是脱水全文。
    # 与其指一条走不通的路让他白跑,不如只如实说明这里被截断了。
    return text[:lo].rstrip() + "\n…(原文过长,此处截断)"


# 一天一个档案(archive_session 按天合并)之后,忙的一天会有好几节:
#   # 会话归档 2026-08-02
#   ## 01:24 …  ## 15:43 …  ## 19:33 …
# 从头往后砍等于**把今天最近发生的事切掉**,他醒来读到的是凌晨那节 ——
# 2026-08-02 就这么撞上了:用户说「breath 的时候说八月二号的归档内容太长被截断了,
# 沈渡那边看不见」。所以改成按节裁:**保最近的几节**,老的整节省略并如实说明。
_SECTION_RE = re.compile(r"^##\s+\d{1,2}:\d{2}\s*$", re.M)


def _split_archive_sections(text: str):
    """把当天档案拆成 (抬头, [节…])。没有 `## HH:MM` 结构就返回 (text, [])。"""
    marks = list(_SECTION_RE.finditer(text))
    if not marks:
        return text, []
    head = text[: marks[0].start()].rstrip()
    sections = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        sections.append(text[m.start(): end].rstrip())
    return head, sections


def _truncate_section_keep_ends(section: str, limit: int) -> str:
    """单节太长时,保住开头**和结尾**。

    归档的写法是正文在前、`**亮点**` / `**心情**` 收在最后 —— 直接截尾正好把
    最该看见的情绪总结切掉(2026-07-25 已经吃过一次这个亏)。所以掐中间。
    """
    if count_tokens_approx(section) <= limit:
        return section
    lines = section.split("\n")
    tail_lines = [ln for ln in lines[-6:] if ln.startswith("**")]
    tail = "\n".join(tail_lines)
    tail_tokens = count_tokens_approx(tail) if tail else 0
    note = "\n…(这一节太长,中间略去)\n"
    head_budget = limit - tail_tokens - count_tokens_approx(note)
    if head_budget <= 0:
        return _truncate_to_tokens(section, limit)
    head = _truncate_to_tokens(section, head_budget)
    head = head.replace("\n…(原文过长,此处截断)", "")
    return head.rstrip() + note + tail if tail else head


def _truncate_archive_raw(text: str, limit: int) -> str:
    """归档原文裁到预算内:**按节保最近的**,再不够才在节内掐中间。"""
    if limit <= 0 or count_tokens_approx(text) <= limit:
        return text
    head, sections = _split_archive_sections(text)
    if not sections:
        return _truncate_to_tokens(text, limit)

    head_tokens = count_tokens_approx(head)
    kept: list = []
    used = head_tokens
    for sec in reversed(sections):                      # 从最近的一节往回收
        sec_tokens = count_tokens_approx(sec)
        note_tokens = 40                                # 给省略说明留点余量
        if kept and used + sec_tokens + note_tokens > limit:
            break
        if not kept and used + sec_tokens > limit:
            # 连最近这一节都放不下:节内掐中间,保住开头与亮点/心情
            kept.append(_truncate_section_keep_ends(sec, max(limit - head_tokens - note_tokens, 200)))
            used = limit
            break
        kept.append(sec)
        used += sec_tokens
    kept.reverse()

    omitted = len(sections) - len(kept)
    parts = [head] if head else []
    if omitted > 0:
        parts.append(f"…(这一天更早的 {omitted} 节已省略,下面是最近的)")
    parts.extend(kept)
    return "\n\n".join(p for p in parts if p).strip()


async def _render_archived(
    archived_buckets: list, mode: str, token_budget: int, min_keep: int = 0,
    prefix: str = "🗄️ [归档] ",
) -> list:
    """归档桶 → 最近归档条目，受 token 预算约束。

    min_keep: 保底条数——token 预算耗尽也至少渲染这么多条（有货的前提下），
    保证唤醒时"最近归档 2-5 条"的下限不被钉选桶挤掉。
    prefix: 条目前缀，"最近记下"段复用本函数时换成 📝。
    """
    results = []
    for b in archived_buckets:
        if token_budget <= 0 and len(results) >= min_keep:
            break
        try:
            if mode == "summary":
                line = _summary_line(b, prefix=prefix)
                line_tokens = count_tokens_approx(line)
                if line_tokens > token_budget and len(results) >= min_keep:
                    break
                results.append(line)
                token_budget -= line_tokens
                continue
            if mode == "raw":
                # 原文直出:归档内容本身已是精炼过的「写给下一个自己的信」,不再脱水,
                # 免得二次摘要把语气和细节磨平。超长的单条截断,不让它吃光预算。
                body = _truncate_archive_raw(
                    strip_wikilinks(b["content"]).strip(), BREATH_RAW_MAX_TOKENS
                )
                name = b.get("metadata", {}).get("name", b["id"])
                entry = f"{prefix}[bucket_id:{b['id']}] {name}\n{body}"
                entry_tokens = count_tokens_approx(entry)
                if entry_tokens > token_budget and len(results) >= min_keep:
                    break
                results.append(entry)
                token_budget -= entry_tokens
                continue
            clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
            summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if summary_tokens > token_budget and len(results) >= min_keep:
                break
            results.append(f"{prefix}[bucket_id:{b['id']}] {summary}")
            token_budget -= summary_tokens
        except Exception as e:
            logger.warning(f"Failed to dehydrate archived bucket / 归档桶脱水失败: {e}")
            continue
    return results


def _is_expired(meta: dict) -> bool:
    """便利贴是否已过期。没有 expires_at 的普通记忆永远返回 False(不受影响)。
    时间戳坏了当没过期处理——宁可多留一张便利贴,也不误删。"""
    exp = meta.get("expires_at")
    if not exp:
        return False
    try:
        return now_local() >= datetime.fromisoformat(str(exp))
    except (ValueError, TypeError):
        return False


# =============================================================
# 触发日期 / trigger_date —— 「到那天再提醒我」
#
# 和便利贴(expires_at)正好相反:那个到点撕掉,这个到点才响。
# 到期(或已过期还没处理)的桶在唤醒时浮进「今日浮现」区,处理完 trace 标
# trigger_done=1 就不再重复浮现。用的是本地日期(now_local,北京时间),
# 不是 UTC —— 「今天」必须和她过的那个今天是同一天。
# =============================================================
_TRIGGER_ALIASES = {
    "today": 0, "今天": 0, "今日": 0,
    "tomorrow": 1, "明天": 1, "明日": 1,
    "后天": 2, "大后天": 3,
}


def _normalize_trigger_date(raw: str) -> str | None:
    """把 trigger_date 入参规整成 YYYY-MM-DD;认不出来返回 None。

    支持:YYYY-MM-DD / YYYY/M/D / MM-DD(补当年) / +N(N天后) / today,明天,后天…
    认不出来**不猜**——宁可回一句「看不懂这个日期」让他重说,
    也不要默默定在一个错的日子上,那种错要到该响的那天才会被发现。
    """
    raw = (raw or "").strip()
    if not raw:
        return None
    today = now_local().date()

    key = raw.lower()
    if key in _TRIGGER_ALIASES:
        return (today + timedelta(days=_TRIGGER_ALIASES[key])).isoformat()

    m = re.fullmatch(r"\+(\d{1,4})\s*(?:天|d|days?)?", key)
    if m:
        return (today + timedelta(days=int(m.group(1)))).isoformat()

    norm = raw.replace("/", "-").replace(".", "-")
    norm = re.sub(r"(\d+)年(\d+)月(\d+)日?", r"\1-\2-\3", norm)
    norm = re.sub(r"(\d+)月(\d+)日?", r"\1-\2", norm)

    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", norm)
    if m:
        y, mo, d = (int(x) for x in m.groups())
    else:
        m = re.fullmatch(r"(\d{1,2})-(\d{1,2})", norm)
        if not m:
            return None
        y = today.year
        mo, d = (int(x) for x in m.groups())
    try:
        return date(y, mo, d).isoformat()
    except ValueError:
        return None


def _due_triggers(all_buckets: list, today: str = None) -> list:
    """到期待处理的桶:trigger_date <= 今天 且 trigger_done 不为真。

    按日期升序 —— 拖得最久的排最前,今天才定的排最后。
    归档/休眠桶照样算:一条提醒不该因为那条记忆自己沉下去就失效,
    「到点响」是它被写下时就答应过的事。
    """
    today = today or now_local().date().isoformat()
    due = []
    for b in all_buckets:
        meta = b.get("metadata", {})
        td = str(meta.get("trigger_date", "") or "").strip()[:10]
        if not td or meta.get("trigger_done"):
            continue
        if td <= today:
            b["_overdue_days"] = _days_between(td, today)
            due.append(b)
    due.sort(key=lambda x: str(x["metadata"].get("trigger_date", "")))
    return due


def _days_between(earlier: str, later: str) -> int:
    """两个 YYYY-MM-DD 相差几天;算不出来当 0(只用于展示,不参与判定)。"""
    try:
        return (date.fromisoformat(later) - date.fromisoformat(earlier)).days
    except (ValueError, TypeError):
        return 0


def _trigger_prefix(b: dict) -> str:
    """今日浮现条目的前缀:今天的标日期,过期的标「已过 N 天」。"""
    overdue = int(b.get("_overdue_days", 0) or 0)
    td = str(b["metadata"].get("trigger_date", ""))[:10]
    if overdue > 0:
        return f"⏰ [今日浮现·已过{overdue}天 {td}] "
    return f"⏰ [今日浮现·{td}] "


async def _render_due_triggers(due_buckets: list, mode: str, token_budget: int,
                               min_keep: int = 3) -> list:
    """今日浮现区渲染:每条自带日期前缀(过期的标已过几天)。"""
    results = []
    for b in due_buckets:
        rendered = await _render_archived(
            [b], mode, token_budget, min_keep=1 if len(results) < min_keep else 0,
            prefix=_trigger_prefix(b),
        )
        if not rendered:
            break
        results.extend(rendered)
        token_budget -= sum(count_tokens_approx(r) for r in rendered)
        if token_budget <= 0 and len(results) >= min_keep:
            break
    return results


def _recent_dynamic(all_buckets: list, limit: int) -> list:
    """hold 写入的普通动态桶，按 last_active（回退 created）降序取最近几条。

    修复「他自己 hold 过的内容 breath 时被忽略」：之前无参 breath/唤醒只回
    钉选+归档，动态桶只能靠语义搜索命中——刚记下的事在新窗口里等于不存在。
    钉选/归档/feel/休眠桶不算，它们各有自己的浮现通道。limit<=0 时关闭。
    """
    if limit <= 0:
        return []
    dyn = [
        b for b in all_buckets
        if b["metadata"].get("type") == "dynamic"
        and not b["metadata"].get("pinned")
        and not b["metadata"].get("protected")
        and not b["metadata"].get("dormant")
        and not _is_expired(b["metadata"])   # 便利贴过期即刻不再浮现(物理删除交给 decay 巡查)
    ]
    dyn.sort(
        key=lambda b: str(b["metadata"].get("last_active", b["metadata"].get("created", ""))),
        reverse=True,
    )
    return dyn[:limit]


def _resurface_candidates(all_buckets: list, exclude_ids: set, limit: int) -> list:
    """「旧事重提」:挑几条很久没被想起、但当初记得很牢的旧记忆。

    **刻意不用衰减分数来排。** 衰减分 = 重要度 × 激活次数 × e^(-λ×闲置天数),
    闲置越久分越低 —— 拿它排序,结果永远是「最近的那几条」,而这条通道要的
    恰恰是相反的东西。所以这里自己算一个分,把「久」从惩罚项变成加分项:

      情绪重(emotion) —— 平淡的流水账不值得重提,当时有情绪的才值得
      闲置久(idle)    —— 越久没想起越优先,一年封顶
      提得少(quiet)   —— 从没被翻出来过的排前面
      当初记得牢      —— importance 作为总体量级

    每项都留了下限(0.5+),避免任何单项为 0 就把一条本该重提的记忆判死。

    **候选池必须包含 archive/**:衰减引擎大约 60~80 天就把普通桶挪进归档
    (score=重要度×激活^0.3×e^(-0.05×天),8 分的桶约 66 天就跌破 0.3 阈值)。
    只在"活着"的桶里找,真正的旧事一条都碰不到 —— 而"已经淡出去了却突然想起"
    正是这条通道的全部意义。OB 的归档本就是淡去不是删除,捞回来看一眼不违背它。

    排除:钉选/永久/feel(各有自己的浮现通道)、**会话归档**(archive_session
    写的「给下一个自己的信」,有自己的浮现通道,tags 带 session / domain 是「归档」)、
    休眠桶(OB 里 dormant = 闲置久且重要度<3,正是不值得重提的那批)、过期便利贴、
    本轮已在「最近」里出现的,以及冷却期内刚重提过的。
    """
    if limit <= 0:
        return []

    now = now_local()
    out = []
    for b in all_buckets:
        meta = b["metadata"]
        if meta.get("type") in ("permanent", "feel"):
            continue
        if meta.get("pinned") or meta.get("protected") or meta.get("dormant"):
            continue
        # 会话归档不进这条通道:它有自己的浮现口(唤醒时的「最近归档」)。
        # ⚠️ 不能只看 type=="archived" —— 衰减淡出的普通桶也是这个 type,
        # 而它们恰恰是这条通道要找的。靠 archive_session 写死的标记来分辨。
        tags = meta.get("tags") or []
        domains = meta.get("domain") or []
        if "session" in tags or "归档" in domains:
            continue
        if b["id"] in exclude_ids or _is_expired(meta):
            continue

        last_active_str = str(meta.get("last_active", meta.get("created", "")))
        try:
            idle_days = (now - datetime.fromisoformat(last_active_str)).total_seconds() / 86400
        except (ValueError, TypeError):
            continue
        if idle_days < RESURFACE_MIN_IDLE_DAYS:
            continue

        # 冷却:最近重提过的先放一放,别连着几轮念叨同一件事
        resurfaced_str = str(meta.get("last_resurfaced", "") or "")
        if resurfaced_str:
            try:
                since = (now - datetime.fromisoformat(resurfaced_str)).total_seconds() / 86400
                if since < RESURFACE_COOLDOWN_DAYS:
                    continue
            except (ValueError, TypeError):
                pass    # 时间戳坏了就当没重提过,宁可多翻一次也不永久埋掉

        valence = float(meta.get("valence", 0.5) or 0.5)
        arousal = float(meta.get("arousal", 0.3) or 0.3)
        emotion = max(arousal, abs(valence - 0.5) * 2)          # 0~1,离"平静中性"多远
        idle_w = min(idle_days / 365.0, 1.0)                     # 越久越优先,一年封顶
        quiet_w = 1.0 / (1.0 + float(meta.get("activation_count", 1) or 1))
        importance = float(meta.get("importance", 5) or 5)

        score = importance * (0.5 + emotion) * (0.5 + idle_w) * (0.5 + quiet_w)
        if score < RESURFACE_MIN_SCORE:
            continue    # 不够分量的旧事就让它继续沉着,别为了填满名额硬凑
        b["resurface_score"] = score
        b["idle_days"] = idle_days
        b["faded"] = meta.get("type") == "archived"   # 已被衰减引擎淡出
        out.append(b)

    out.sort(key=lambda x: x["resurface_score"], reverse=True)
    return out[:limit]


async def _related_note(bucket: dict) -> str:
    """D3：若桶存在 related 关联，返回一行关联桶 id+名称的注脚（不展开全文）。

    related 元字段兼容多种格式：逗号分隔字符串、id 列表、或 {id,name} 字典列表。
    无 related 时返回空串。
    """
    meta = bucket.get("metadata", {})
    related = meta.get("related") or []
    if isinstance(related, str):
        related = [r.strip() for r in related.split(",") if r.strip()]
    if not isinstance(related, (list, tuple)) or not related:
        return ""

    parts = []
    for r in related:
        if isinstance(r, dict):
            rid = str(r.get("id", "")).strip()
            rname = str(r.get("name", "")).strip()
        else:
            rid = str(r).strip()
            rname = ""
        if not rid:
            continue
        if not rname:
            try:
                rb = await bucket_mgr.get(rid)
                if rb:
                    rname = rb["metadata"].get("name", rid)
            except Exception:
                rname = ""
        parts.append(f"{rname}({rid})" if rname else rid)

    return "  ↳ 关联桶: " + ", ".join(parts) if parts else ""


async def _ensure_related(bucket: dict) -> None:
    """命中桶若无 related，用 embedding 相似度自动补全前3个最相似桶并写入(不覆盖已有)。

    就地更新 bucket["metadata"]["related"]，使同一次 breath 调用即可展示。
    embedding 不可用 / 无相似桶时静默跳过，不影响检索返回。
    """
    meta = bucket.get("metadata", {})
    if meta.get("related"):
        return
    try:
        similar = await embedding_engine.find_similar_buckets(bucket["id"], top_k=3, min_sim=0.5)
        if not similar:
            return
        related_ids = [bid for bid, _ in similar]
        ok = await bucket_mgr.set_related(bucket["id"], related_ids, overwrite=False)
        if ok:
            meta["related"] = related_ids  # 就地反映，供本次 _related_note 使用
    except Exception as e:
        logger.warning(f"Auto-link related failed / 自动关联失败 {bucket.get('id', '?')}: {e}")


def _extract_todos(bucket: dict) -> list[str]:
    """提取一个桶的待办：优先读 todos 元字段，再扫描正文中的未勾选 markdown 复选框。

    支持的格式：
      - todos 元字段（list 或逗号分隔字符串）
      - 正文行 `- [ ] xxx` / `* [ ] xxx`（仅未勾选；已勾选 [x] 忽略）
    去重保序。
    """
    meta = bucket.get("metadata", {})
    todos: list[str] = []

    raw = meta.get("todos")
    if isinstance(raw, str):
        todos += [t.strip() for t in raw.split(",") if t.strip()]
    elif isinstance(raw, (list, tuple)):
        todos += [str(t).strip() for t in raw if str(t).strip()]

    content = bucket.get("content", "") or ""
    for line in content.splitlines():
        m = re.match(r"\s*[-*]\s*\[\s\]\s*(.+)", line)
        if m:
            todos.append(strip_wikilinks(m.group(1).strip()))

    return list(dict.fromkeys(todos))


# =============================================================
# Tool 1: breath — Breathe
# 工具 1：breath — 呼吸
#
# No args: pinned buckets + recent archived session summaries (2-5, no semantic surfacing)
# 无参数：钉选桶 + 最近归档的会话总结（按归档时间降序取 2-5 条，不做语义浮现）
# Plus the most recent hold-written dynamic buckets (BREATH_RECENT_N, default 3):
# without them, freshly held memories were invisible in a new window unless a
# semantic query happened to hit them.
# 另附最近 hold 写入的动态桶（BREATH_RECENT_N 条，默认 3，设 0 关闭）：
# 不带的话，刚记下的事在新窗口里除非语义搜索碰巧命中，否则等于不存在。
# Wake-up (no query / wake / startup) never triggers Dreaming and never
# includes feel buckets — dream() and breath(domain="feel") are explicit-only.
# 唤醒（无 query / wake / startup）不触发 Dreaming、不带 feel——两者只在显式调用时出现。
# With args: semantic surfacing — search by keyword + emotion coordinates
# 有参数：语义浮现——按关键词+情感坐标检索记忆
# =============================================================
@mcp.tool()
async def breath(
    query: str = "",
    max_tokens: int = -1,
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    max_results: int = -1,
    importance_min: int = -1,
    mode: str = "",
    date_from: str = "",
    date_to: str = "",
    include_dormant: bool = False,
    wake: bool = False,
    startup: bool = False,
) -> str:
    """检索/浮现记忆。不传query或传空=返回钉选桶+最近归档的会话总结(archive_session写入,按归档时间降序,默认2-5条)+最近记下的动态桶(hold写入,按活跃时间降序,默认3条,env BREATH_RECENT_N可调/设0关闭);不触发Dreaming,不带feel;有query=语义浮现(关键词+向量检索,返回匹配结果)。max_tokens控制返回总token上限:默认-1=按模式自动(自适应检索5000省钱,无query/其它按 env BREATH_WAKE_BUDGET,默认10000);显式传则按值(上限20000)。domain逗号分隔,valence/arousal 0~1(-1忽略):有query时它们是四维评分里的情感共鸣维;**不带query只给坐标=心境共鸣模式**,不看文本、只按各桶情绪坐标与传入坐标的距离排序返回(可与domain/date_from/date_to/importance_min/max_results组合;钉选桶和feel不进这条通道)。max_results控制返回数量:默认-1=自适应(不卡固定条数,搜索时按"与最高分的相对差距"圈定相关集,无query/wake/startup时最近归档取2-5条,真正上限交给max_tokens);显式传>=1则按该值硬截断(最大50)。钉选桶不计入名额,超出部分末尾附注。importance_min>=1时按重要度批量拉取(不走语义搜索,按importance降序返回最多20条)。mode=summary每桶只返回单行摘要省token,mode=full返回脱水全文,mode=raw返回原文不做二次压缩;不传时:唤醒/无query浮现的归档与最近记下默认raw(原文——归档本就是写给下一个自己的信,再脱水会磨平语气细节),其余默认summary;query非空时忽略mode始终返回full。单条原文超限会按「## HH:MM」分节裁、保最近几节并标注(env BREATH_RAW_MAX_TOKENS,设0=不限)。date_from/date_to(YYYY-MM-DD,可选)按桶更新时间闭区间过滤,可与其他参数组合。include_dormant=True时包含休眠桶(默认隐藏)。wake=True或startup=True时触发"唤醒模式":忽略query/domain等检索参数,返回钉选桶+最近归档桶(按归档时间降序,默认2-5条,可用max_results显式调整条数)+最近记下的动态桶;唤醒不触发Dreaming、不带feel——dream()和breath(domain=\"feel\")需要时单独调用。"""
    await decay_engine.ensure_started()
    # max_results=-1(默认)→ 自适应:相关度决定条数,token预算兜底
    # 显式传 >=1 → 按该值硬截断(向后兼容手动指定)
    auto_results = max_results is None or max_results < 1
    if auto_results:
        REL_WINDOW = 0.6        # 搜索:保留分数 >= 最高分*0.6 的相关桶
        AUTO_HARD_CAP = 50      # 安全上限,正常情况下 token 预算先触顶
    else:
        max_results = min(max_results, 50)
    # 记住调用方有没有显式指定 mode:没指定时唤醒模式要给归档用原文(见下),
    # 显式指定则一律尊重调用方。
    mode_explicit = bool((mode or "").strip())
    mode = (mode or "summary").strip().lower()
    if mode not in ("summary", "full", "raw"):
        mode = "summary"
    # token 预算:max_tokens=-1(默认)→ 按模式给默认值;显式传则按值(上限 20000)
    # 自适应检索默认压到 5000(省钱),浮现/其它模式仍 10000;summary 浮现本就便宜
    auto_tokens = max_tokens is None or max_tokens < 1
    is_search = bool((query or "").strip()) and domain.strip().lower() != "feel"
    if auto_tokens:
        max_tokens = 5000 if (is_search and auto_results) else BREATH_WAKE_BUDGET
    else:
        max_tokens = min(max_tokens, max(20000, BREATH_WAKE_BUDGET))
    date_from = (date_from or "").strip()
    date_to = (date_to or "").strip()

    # --- startup / wake mode: triggered surface — only pinned + recent archived ---
    # --- startup / wake 唤醒模式：触发式浮现——只返回钉选桶 + 最近归档桶（2-5条）---
    # 唤醒时不触发 Dreaming、不带 feel：dream/feel 只在被显式调用时才出现。
    # （startup 旧行为曾打包 Dreaming + 最近 feel，现与 wake 统一为纯唤醒浮现。）
    if startup or wake:
        WAKE_ARCHIVE_DEFAULT = BREATH_ARCHIVE_N  # 归档条数上限（env BREATH_ARCHIVE_N）
        WAKE_ARCHIVE_MIN = min(2, BREATH_ARCHIVE_N)  # 下限：预算再紧也保底这么多（不超过上限）
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            logger.error(f"Failed to list buckets for wake / wake列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        archived_buckets = [
            b for b in all_buckets if b["metadata"].get("type") == "archived"
        ]
        archived_buckets.sort(key=_archived_sort_key, reverse=True)
        archive_limit = max_results if not auto_results else WAKE_ARCHIVE_DEFAULT
        archived_buckets = archived_buckets[:archive_limit]

        pinned_results = await _render_pinned(pinned_buckets, mode)

        token_budget = max_tokens
        for r in pinned_results:
            token_budget -= count_tokens_approx(r)
        # 唤醒的重点是「读到上一段发生了什么」,只给标题行等于没醒。
        # 调用方没指定 mode 时,归档与最近记下都用原文(默认 raw,可用环境变量改)。
        wake_mode = mode if mode_explicit else BREATH_WAKE_ARCHIVE_MODE

        # 今日浮现:到期(或过期未处理)的触发日期桶,排在归档之前——
        # 它是「今天要做的事」,压在一堆昨天的总结下面等于没提醒。
        due_buckets = _due_triggers(all_buckets)
        due_results = await _render_due_triggers(due_buckets, wake_mode, token_budget)
        for r in due_results:
            token_budget -= count_tokens_approx(r)
        # 已经在「今日浮现」露过面的,不再在归档/最近记下里重复一遍
        due_ids = {b["id"] for b in due_buckets}
        archived_buckets = [b for b in archived_buckets if b["id"] not in due_ids]

        min_keep = WAKE_ARCHIVE_MIN if auto_results else 0
        archive_results = await _render_archived(
            archived_buckets, wake_mode, token_budget, min_keep=min_keep
        )

        # 最近记下:他自己 hold 的动态桶也要浮上来(保底 1 条,预算兜底)
        for r in archive_results:
            token_budget -= count_tokens_approx(r)
        # 「最近记下」保持单行:它是线索行,细节让他自己 breath/trace 去查(与 /breath-hook 同口径)
        recent_results = await _render_archived(
            [b for b in _recent_dynamic(all_buckets, BREATH_RECENT_N)
             if b["id"] not in due_ids],
            mode, token_budget,
            min_keep=1 if auto_results else 0, prefix="📝 [最近记下] ",
        )

        if not pinned_results and not archive_results and not recent_results and not due_results:
            await _fire_webhook("breath", {"mode": "wake_empty", "matches": 0})
            return "唤醒模式：没有钉选记忆，也没有最近归档的记忆。"

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if due_results:
            parts.append(
                "=== 今日浮现 ===\n" + "\n---\n".join(due_results)
                + "\n(处理完用 trace(bucket_id, trigger_done=1) 标一下,就不再重复浮现)"
            )
        if archive_results:
            parts.append("=== 最近归档 ===\n" + "\n---\n".join(archive_results))
        if recent_results:
            parts.append("=== 最近记下 ===\n" + "\n---\n".join(recent_results))
        await _fire_webhook(
            "breath",
            {
                "mode": "startup" if startup else "wake",
                "pinned": len(pinned_results),
                "due": len(due_results),
                "archived": len(archive_results),
                "recent": len(recent_results),
            },
        )
        return "\n\n".join(parts)

    # --- Mood-only mode: emotion coordinates without a query ---
    # --- 心境共鸣模式：只给情绪坐标、不给关键词，按坐标距离排序 ---
    # 「现在这个心情，让我想起过什么」——这个问法本来就没有关键词。
    # 有 query 时不走这里：那时情绪坐标仍是四维评分里的一维（老行为不变）。
    q_valence_only = valence if 0 <= valence <= 1 else None
    q_arousal_only = arousal if 0 <= arousal <= 1 else None
    if (q_valence_only is not None or q_arousal_only is not None) and not (query or "").strip():
        try:
            mood_matches = await bucket_mgr.search_by_mood(
                query_valence=q_valence_only,
                query_arousal=q_arousal_only,
                limit=max_results if not auto_results else 50,
                domain_filter=[d.strip() for d in domain.split(",") if d.strip()] or None,
            )
        except Exception as e:
            logger.error(f"Mood search failed / 心境检索失败: {e}")
            return "按心境检索时出错，请稍后重试。"

        # 可与现有检索参数组合：日期区间 / 重要度下限 / 休眠桶
        mood_matches = [
            b for b in mood_matches
            if _passes_date_filter(b["metadata"], date_from, date_to)
            and (include_dormant or not b["metadata"].get("dormant"))
            and not _is_expired(b["metadata"])
            and (importance_min < 1
                 or int(b["metadata"].get("importance", 0)) >= importance_min)
        ]
        if not mood_matches:
            return "没有情绪坐标接近的记忆。"

        coord = []
        if q_valence_only is not None:
            coord.append(f"V{q_valence_only:.2f}")
        if q_arousal_only is not None:
            coord.append(f"A{q_arousal_only:.2f}")
        results = []
        token_used = 0
        for b in mood_matches:
            if token_used >= max_tokens:
                break
            try:
                meta = b["metadata"]
                if mode == "summary":
                    entry = _summary_line(b, prefix="🎭 [心境共鸣] ")
                else:
                    clean_meta = {k: v for k, v in meta.items() if k != "tags"}
                    body = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    entry = f"🎭 [心境共鸣] [bucket_id:{b['id']}] {body}"
                entry += (f"\n  ↳ 情感坐标 V{float(meta.get('valence', 0.5)):.1f}"
                          f"/A{float(meta.get('arousal', 0.3)):.1f}"
                          f" 距离 {b['mood_distance']:.2f}")
                entry_tokens = count_tokens_approx(entry)
                if token_used + entry_tokens > max_tokens and results:
                    break
                results.append(entry)
                token_used += entry_tokens
            except Exception as e:
                logger.warning(f"Mood entry render failed / 心境条目渲染失败: {e}")
                continue

        if not results:
            return "没有情绪坐标接近的记忆。"
        await _fire_webhook("breath", {"mode": "mood", "matches": len(results)})
        header = f"=== 心境共鸣({'/'.join(coord)},按情绪坐标距离排序)==="
        return header + "\n" + "\n---\n".join(results)

    # --- importance_min mode: bulk fetch by importance threshold ---
    # --- 重要度批量拉取模式：跳过语义搜索，按 importance 降序返回 ---
    if importance_min >= 1:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
        except Exception as e:
            return f"记忆系统暂时无法访问: {e}"
        filtered = [
            b for b in all_buckets
            if int(b["metadata"].get("importance", 0)) >= importance_min
            and b["metadata"].get("type") not in ("feel",)
            and _passes_date_filter(b["metadata"], date_from, date_to)
            and (include_dormant or not b["metadata"].get("dormant"))
        ]
        filtered.sort(key=lambda b: int(b["metadata"].get("importance", 0)), reverse=True)
        filtered = filtered[:20]
        if not filtered:
            return f"没有重要度 >= {importance_min} 的记忆。"
        results = []
        token_used = 0
        for b in filtered:
            if token_used >= max_tokens:
                break
            try:
                clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                t = count_tokens_approx(summary)
                if token_used + t > max_tokens:
                    break
                imp = b["metadata"].get("importance", 0)
                results.append(f"[importance:{imp}] [bucket_id:{b['id']}] {summary}")
                token_used += t
            except Exception as e:
                logger.warning(f"importance_min dehydrate failed: {e}")
        return "\n---\n".join(results) if results else "没有可以展示的记忆。"

    # --- No args or empty query: pinned + recent archived session summaries ---
    # --- 无参数或空query：钉选桶 + 最近归档的会话总结，不做语义浮现 ---
    # 每次新窗口唤醒，读到的应该是"上一个/前几个窗口的会话总结"（archive_session 写入）。
    # 普通动态桶（hold 写入的）不出现在这里，只作为语义搜索（query 非空）的检索库；
    # 语义浮现（权重排序/冷启动/多样性采样）见下方 "With args: search mode" 分支。
    if not query or not query.strip():
        BREATH_ARCHIVE_DEFAULT = BREATH_ARCHIVE_N  # 归档条数上限（env BREATH_ARCHIVE_N）
        BREATH_ARCHIVE_MIN = min(2, BREATH_ARCHIVE_N)  # 下限：保底这么多（不超过上限）
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            logger.error(f"Failed to list buckets for surfacing / 浮现列桶失败: {e}")
            return "记忆系统暂时无法访问。"

        # --- Pinned/protected buckets: always surface as core principles ---
        # --- 钉选桶：作为核心准则，始终浮现 ---
        pinned_buckets = [
            b for b in all_buckets
            if b["metadata"].get("pinned") or b["metadata"].get("protected")
        ]
        pinned_results = await _render_pinned(pinned_buckets, mode)

        # --- Recent archived session summaries, by archive time desc ---
        # --- 最近归档的会话总结：按归档时间降序取最近几条 ---
        archived_buckets = [
            b for b in all_buckets
            if b["metadata"].get("type") == "archived"
            and _passes_date_filter(b["metadata"], date_from, date_to)
            and (include_dormant or not b["metadata"].get("dormant"))
        ]
        archived_buckets.sort(key=_archived_sort_key, reverse=True)
        archive_limit = max_results if not auto_results else BREATH_ARCHIVE_DEFAULT
        archived_buckets = archived_buckets[:archive_limit]

        logger.info(
            f"Breath (no query): {len(all_buckets)} total, "
            f"{len(pinned_buckets)} pinned, {len(archived_buckets)} archived"
        )

        token_budget = max_tokens
        for r in pinned_results:
            token_budget -= count_tokens_approx(r)

        # 今日浮现:无参 breath() 也是唤醒口径,同样先给今天该响的那几条
        due_buckets = _due_triggers(all_buckets)
        due_results = await _render_due_triggers(
            due_buckets, mode if mode_explicit else BREATH_WAKE_ARCHIVE_MODE, token_budget,
        )
        for r in due_results:
            token_budget -= count_tokens_approx(r)
        # 已经在「今日浮现」露过面的,不再在归档/最近记下里重复一遍
        due_ids = {b["id"] for b in due_buckets}
        archived_buckets = [b for b in archived_buckets if b["id"] not in due_ids]

        # 与唤醒同口径:没显式指定 mode 时归档给原文——无参 breath() 正是他
        # 唤醒协议里用的那一个,只给标题行等于没醒。
        archive_results = await _render_archived(
            archived_buckets, mode if mode_explicit else BREATH_WAKE_ARCHIVE_MODE,
            token_budget, min_keep=BREATH_ARCHIVE_MIN if auto_results else 0,
        )

        # 最近记下:他自己 hold 的动态桶也要浮上来(保底 1 条,预算兜底)
        for r in archive_results:
            token_budget -= count_tokens_approx(r)
        recent_buckets = [
            b for b in _recent_dynamic(all_buckets, BREATH_RECENT_N)
            if _passes_date_filter(b["metadata"], date_from, date_to)
            and b["id"] not in due_ids
        ]
        recent_results = await _render_archived(
            recent_buckets, mode, token_budget,
            min_keep=1 if auto_results else 0, prefix="📝 [最近记下] ",
        )

        if not pinned_results and not archive_results and not recent_results and not due_results:
            return "没有钉选记忆，也没有归档的会话总结。"

        parts = []
        if pinned_results:
            parts.append("=== 核心准则 ===\n" + "\n---\n".join(pinned_results))
        if due_results:
            parts.append(
                "=== 今日浮现 ===\n" + "\n---\n".join(due_results)
                + "\n(处理完用 trace(bucket_id, trigger_done=1) 标一下,就不再重复浮现)"
            )
        if archive_results:
            parts.append("=== 最近归档 ===\n" + "\n---\n".join(archive_results))
        if recent_results:
            parts.append("=== 最近记下 ===\n" + "\n---\n".join(recent_results))
        return "\n\n".join(parts)

    # --- Feel retrieval: domain="feel" is a special channel ---
    # --- Feel 检索：domain="feel" 是独立入口 ---
    if domain.strip().lower() == "feel":
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            feels.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
            if not feels:
                return "没有留下过 feel。"
            results = []
            for f in feels:
                created = f["metadata"].get("created", "")
                entry = f"[{created}] [bucket_id:{f['id']}]\n{strip_wikilinks(f['content'])}"
                results.append(entry)
                if count_tokens_approx("\n---\n".join(results)) > max_tokens:
                    break
            return "=== 你留下的 feel ===\n" + "\n---\n".join(results)
        except Exception as e:
            logger.error(f"Feel retrieval failed: {e}")
            return "读取 feel 失败。"

    # --- With args: search mode (keyword + vector dual channel) ---
    # --- 有参数：检索模式（关键词 + 向量双通道）---
    domain_filter = [d.strip() for d in domain.split(",") if d.strip()] or None
    q_valence = valence if 0 <= valence <= 1 else None
    q_arousal = arousal if 0 <= arousal <= 1 else None

    # Auto 模式拉宽候选池(50),避免"所有相关记忆"在排序前就被截断;
    # 显式模式沿用旧行为(至少 20,保证相关度附注准确)
    fetch_n = AUTO_HARD_CAP if auto_results else max(max_results, 20)
    try:
        matches = await bucket_mgr.search(
            query,
            limit=fetch_n,
            domain_filter=domain_filter,
            query_valence=q_valence,
            query_arousal=q_arousal,
        )
    except Exception as e:
        logger.error(f"Search failed / 检索失败: {e}")
        return "检索过程出错，请稍后重试。"

    # --- Exclude pinned/protected from search results (they surface in surfacing mode) ---
    # --- 搜索模式排除钉选桶（它们在浮现模式中始终可见）---
    matches = [b for b in matches if not (b["metadata"].get("pinned") or b["metadata"].get("protected"))]

    # --- Vector similarity channel: find semantically related buckets ---
    # --- 向量相似度通道：找到语义相关的桶 ---
    matched_ids = {b["id"] for b in matches}
    try:
        vector_results = await embedding_engine.search_similar(query, top_k=fetch_n)
        for bucket_id, sim_score in vector_results:
            if bucket_id not in matched_ids and sim_score > 0.5:
                bucket = await bucket_mgr.get(bucket_id)
                if bucket and not (bucket["metadata"].get("pinned") or bucket["metadata"].get("protected")):
                    bucket["score"] = round(sim_score * 100, 2)
                    bucket["vector_match"] = True
                    matches.append(bucket)
                    matched_ids.add(bucket_id)
    except Exception as e:
        logger.warning(f"Vector search failed, using keyword only / 向量搜索失败: {e}")

    # --- D2: filter by bucket update time (combines with all other params) ---
    # --- 按桶更新时间过滤（可与其他参数组合）---
    if date_from or date_to:
        matches = [b for b in matches if _passes_date_filter(b["metadata"], date_from, date_to)]

    # --- E5: hide dormant buckets unless explicitly included ---
    # --- 默认隐藏休眠桶，除非 include_dormant=True ---
    if not include_dormant:
        matches = [b for b in matches if not b["metadata"].get("dormant")]

    # --- B4: sort by relevance, then select ---
    # --- 按相关性排序后选取 ---
    matches.sort(key=lambda b: b.get("score", 0), reverse=True)
    if auto_results:
        # 自适应:保留分数落在最高分相对窗口内的"相关集",弱相关长尾自动丢弃。
        # 真正的天花板是 token 预算(下方循环),AUTO_HARD_CAP 仅作安全上限。
        if matches:
            top_score = matches[0].get("score", 0)
            floor = max(bucket_mgr.fuzzy_threshold, top_score * REL_WINDOW)
            matches = [b for b in matches if b.get("score", 0) >= floor]
        matches = matches[:AUTO_HARD_CAP]
    else:
        # 显式 max_results:按该值硬截断(向后兼容)
        matches = matches[:max_results]
    # total_relevant = 相关集大小;循环中若因 token 预算未能全展示,末尾附注
    total_relevant = len(matches)

    results = []
    token_used = 0
    displayed = 0
    for bucket in matches:
        if token_used >= max_tokens:
            break
        try:
            clean_meta = {k: v for k, v in bucket["metadata"].items() if k != "tags"}
            # --- Memory reconstruction: shift displayed valence by current mood ---
            # --- 记忆重构：根据当前情绪微调展示层 valence（±0.1）---
            if q_valence is not None and "valence" in clean_meta:
                original_v = float(clean_meta.get("valence", 0.5))
                shift = (q_valence - 0.5) * 0.2  # ±0.1 max shift
                clean_meta["valence"] = max(0.0, min(1.0, original_v + shift))
            summary = await dehydrator.dehydrate(strip_wikilinks(bucket["content"]), clean_meta)
            summary_tokens = count_tokens_approx(summary)
            if token_used + summary_tokens > max_tokens:
                break
            await bucket_mgr.touch(bucket["id"])
            if bucket.get("vector_match"):
                summary = f"[语义关联] [bucket_id:{bucket['id']}] {summary}"
            else:
                summary = f"[bucket_id:{bucket['id']}] {summary}"
            # --- D3: append related-bucket note (id + name, no full content) ---
            # --- 命中桶若有 related 关联，附一行关联桶 id+名称，不展开全文 ---
            # 无 related 时用 embedding 相似度自动补全前3个（已有不覆盖）
            await _ensure_related(bucket)
            rel_note = await _related_note(bucket)
            if rel_note:
                summary = summary + "\n" + rel_note
            results.append(summary)
            token_used += summary_tokens
            displayed += 1
        except Exception as e:
            logger.warning(f"Failed to dehydrate search result / 检索结果脱水失败: {e}")
            continue

    # --- Random surfacing: when search returns < 3, 40% chance to float old memories ---
    # --- 随机浮现：检索结果不足 3 条时，40% 概率从低权重旧桶里漂上来 ---
    if len(matches) < 3 and random.random() < 0.4:
        try:
            all_buckets = await bucket_mgr.list_all(include_archive=False)
            matched_ids = {b["id"] for b in matches}
            low_weight = [
                b for b in all_buckets
                if b["id"] not in matched_ids
                and decay_engine.calculate_score(b["metadata"]) < 2.0
            ]
            if low_weight:
                drifted = random.sample(low_weight, min(random.randint(1, 3), len(low_weight)))
                drift_results = []
                for b in drifted:
                    clean_meta = {k: v for k, v in b["metadata"].items() if k != "tags"}
                    summary = await dehydrator.dehydrate(strip_wikilinks(b["content"]), clean_meta)
                    drift_results.append(f"[surface_type: random]\n{summary}")
                results.append("--- 忽然想起来 ---\n" + "\n---\n".join(drift_results))
        except Exception as e:
            logger.warning(f"Random surfacing failed / 随机浮现失败: {e}")

    if not results:
        await _fire_webhook("breath", {"mode": "empty", "matches": 0})
        return "未找到相关记忆。"

    # --- B4: overflow note at the very end / 末尾附注超出名额的相关桶 ---
    not_shown = total_relevant - displayed
    if not_shown > 0:
        hint = "可增大 max_tokens 查看" if auto_results else "可增大 max_results 查看"
        results.append(f"…还有 {not_shown} 个相关桶未显示（{hint}）")

    final_text = "\n---\n".join(results)
    await _fire_webhook("breath", {"mode": "ok", "matches": len(matches), "chars": len(final_text)})
    return final_text


# =============================================================
# Tool 2: hold — Hold on to this
# 工具 2：hold — 握住，留下来
# =============================================================
@mcp.tool()
async def hold(
    content: str,
    tags: str = "",
    importance: int = 5,
    pinned: bool = False,
    feel: bool = False,
    source_bucket: str = "",    valence: float = -1,
    arousal: float = -1,
    remember_days: int = 0,
    trigger_date: str = "",
) -> str:
    """存储单条记忆,自动打标+合并。tags逗号分隔,importance 1-10。pinned=True创建永久钉选桶。feel=True存储你的第一人称感受(不参与普通浮现)。source_bucket=被消化的记忆桶ID(feel模式下,标记源记忆为已消化)。remember_days=只想记几天的临时便利贴(如她说"明天要去医院"这类):到点自动撕掉、不进长期记忆、这几天里正常浮现在「最近记下」;0=普通记忆(默认);与pinned/feel互斥。trigger_date=到那天再提醒我(YYYY-MM-DD,也认"明天"/"3月5日"/"+7"):到期或已过期未处理的桶会在唤醒(breath 无参/wake/startup)时出现在「今日浮现」区,处理完用 trace(id, trigger_done=1) 标掉。存入时会自动比对语义最近的旧记忆,发现日期/数字/事实对不上会在返回里附冲突警告——**不自动改任何东西**,怎么处理你定。"""
    await decay_engine.ensure_started()

    # --- Input validation / 输入校验 ---
    if not content or not content.strip():
        return "内容为空，无法存储。"

    importance = max(1, min(10, importance))
    extra_tags = [t.strip() for t in tags.split(",") if t.strip()]

    # --- 触发日期:认不出来的写法直接回绝,不猜(猜错要到该响那天才发现)---
    trigger_at = None
    if trigger_date and trigger_date.strip():
        trigger_at = _normalize_trigger_date(trigger_date)
        if not trigger_at:
            return f"看不懂这个触发日期:{trigger_date}。用 YYYY-MM-DD,或「明天」「3月5日」「+7」这类写法。"

    # --- Feel mode: store as feel type, minimal metadata ---
    # --- Feel 模式：存为 feel 类型，最少元数据 ---
    if feel:
        # Feel valence/arousal = model's own perspective
        feel_valence = valence if 0 <= valence <= 1 else 0.5
        feel_arousal = arousal if 0 <= arousal <= 1 else 0.3
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=[],
            importance=5,
            domain=[],
            valence=feel_valence,
            arousal=feel_arousal,
            name=None,
            bucket_type="feel",
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        # --- Mark source memory as digested + store model's valence perspective ---
        # --- 标记源记忆为已消化 + 存储模型视角的 valence ---
        if source_bucket and source_bucket.strip():
            try:
                update_kwargs = {"digested": True}
                if 0 <= valence <= 1:
                    update_kwargs["model_valence"] = feel_valence
                await bucket_mgr.update(source_bucket.strip(), **update_kwargs)
            except Exception as e:
                logger.warning(f"Failed to mark source as digested / 标记已消化失败: {e}")
        return f"🫧feel→{bucket_id}"

    # --- Step 1: auto-tagging / 自动打标 ---
    try:
        analysis = await dehydrator.analyze(content)
    except Exception as e:
        logger.warning(f"Auto-tagging failed, using defaults / 自动打标失败: {e}")
        analysis = {
            "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
            "tags": [], "suggested_name": "",
        }

    domain = analysis["domain"]
    auto_valence = analysis["valence"]
    auto_arousal = analysis["arousal"]
    auto_tags = analysis["tags"]
    suggested_name = analysis.get("suggested_name", "")

    # --- User-supplied valence/arousal takes priority over analyze() result ---
    # --- 用户显式传入的 valence/arousal 优先，analyze() 结果作为 fallback ---
    final_valence = valence if 0 <= valence <= 1 else auto_valence
    final_arousal = arousal if 0 <= arousal <= 1 else auto_arousal

    all_tags = list(dict.fromkeys(auto_tags + extra_tags))

    # --- Pinned buckets bypass merge and are created directly in permanent dir ---
    # --- 钉选桶跳过合并，直接新建到 permanent 目录 ---
    if pinned:
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=10,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="permanent",
            pinned=True,
            trigger_date=trigger_at,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        return f"📌钉选→{bucket_id} {','.join(domain)}" + (f" ⏰{trigger_at}" if trigger_at else "")

    # --- 便利贴:只记几天、到点自动撕掉的临时记忆 ---
    # 像钉选一样**跳过合并**——便利贴是独立的一条,不该被并进长期桶
    # (并进去过期时间会丢,还会连累那个长期桶跟着被撕)。
    # 到期物理删除交给 decay_engine 每日巡查;浮现层(_recent_dynamic)另做即时过滤,
    # 所以就算 decay 还没跑到,过期的便利贴也不会再冒到他眼前。
    if remember_days and remember_days > 0:
        days = max(1, min(90, int(remember_days)))   # 夹在 1~90 天,防呆
        expires_at = (now_local() + timedelta(days=days)).isoformat()
        # 便利贴 + 触发日期:别让它在该响之前就被撕掉。
        # 触发日比过期日晚,就把过期日顺延到触发日之后一天——
        # 「明天要去医院」这种正是两个都该有的场景,不该逼她二选一。
        if trigger_at:
            try:
                trig_dt = datetime.fromisoformat(trigger_at) + timedelta(days=1)
                if trig_dt > datetime.fromisoformat(expires_at):
                    expires_at = trig_dt.isoformat()
                    days = max(days, (trig_dt - now_local()).days)
            except (ValueError, TypeError):
                pass
        bucket_id = await bucket_mgr.create(
            content=content,
            tags=all_tags,
            importance=importance,
            domain=domain,
            valence=final_valence,
            arousal=final_arousal,
            name=suggested_name or None,
            bucket_type="dynamic",
            expires_at=expires_at,
            trigger_date=trigger_at,
        )
        try:
            await embedding_engine.generate_and_store(bucket_id, content)
        except Exception:
            pass
        await _autolink_new_bucket(bucket_id)
        return (f"🗒️便利贴→{bucket_id} 记{days}天 {','.join(domain)}"
                + (f" ⏰{trigger_at}" if trigger_at else ""))

    # --- Step 2: merge or create / 合并或新建 ---
    result_name, is_merged, info = await _merge_or_create(
        content=content,
        tags=all_tags,
        importance=importance,
        domain=domain,
        valence=final_valence,
        arousal=final_arousal,
        name=suggested_name,
        trigger_date=trigger_at,
    )

    action = "合并→" if is_merged else "新建→"
    line = f"{action}{result_name} {','.join(domain)}"
    if trigger_at:
        line += f" ⏰{trigger_at}"
    return line + _format_conflicts(info.get("conflicts"))


# =============================================================
# Tool 3: grow — Grow, fragments become memories
# 工具 3：grow — 生长，一天的碎片长成记忆
# =============================================================
@mcp.tool()
async def grow(content: str) -> str:
    """日记归档,自动拆分为多桶。短内容(<30字)走快速路径。"""
    await decay_engine.ensure_started()

    if not content or not content.strip():
        return "内容为空，无法整理。"

    # --- Short content fast path: skip digest, use hold logic directly ---
    # --- 短内容快速路径：跳过 digest 拆分，直接走 hold 逻辑省一次 API ---
    # For very short inputs (like "1"), calling digest is wasteful:
    # it sends the full DIGEST_PROMPT (~800 tokens) to DeepSeek for nothing.
    # Instead, run analyze + create directly.
    if len(content.strip()) < 30:
        logger.info(f"grow short-content fast path: {len(content.strip())} chars")
        try:
            analysis = await dehydrator.analyze(content)
        except Exception as e:
            logger.warning(f"Fast-path analyze failed / 快速路径打标失败: {e}")
            analysis = {
                "domain": ["未分类"], "valence": 0.5, "arousal": 0.3,
                "tags": [], "suggested_name": "",
            }
        result_name, is_merged, _info = await _merge_or_create(
            content=content.strip(),
            tags=analysis.get("tags", []),
            importance=analysis.get("importance", 5) if isinstance(analysis.get("importance"), int) else 5,
            domain=analysis.get("domain", ["未分类"]),
            valence=analysis.get("valence", 0.5),
            arousal=analysis.get("arousal", 0.3),
            name=analysis.get("suggested_name", ""),
        )
        action = "合并" if is_merged else "新建"
        return f"{action} → {result_name} | {','.join(analysis.get('domain', []))} V{analysis.get('valence', 0.5):.1f}/A{analysis.get('arousal', 0.3):.1f}"

    # --- Step 1: let API split and organize / 让 API 拆分整理 ---
    try:
        items = await dehydrator.digest(content)
    except Exception as e:
        logger.error(f"Diary digest failed / 日记整理失败: {e}")
        return f"日记整理失败: {e}"

    if not items:
        return "内容为空或整理失败。"

    results = []
    created = 0
    merged = 0

    # --- Step 2: merge or create each item (with per-item error handling) ---
    # --- 逐条合并或新建（单条失败不影响其他）---
    for item in items:
        try:
            result_name, is_merged, _info = await _merge_or_create(
                content=item["content"],
                tags=item.get("tags", []),
                importance=item.get("importance", 5),
                domain=item.get("domain", ["未分类"]),
                valence=item.get("valence", 0.5),
                arousal=item.get("arousal", 0.3),
                name=item.get("name", ""),
            )

            if is_merged:
                results.append(f"📎{result_name}")
                merged += 1
            else:
                results.append(f"📝{item.get('name', result_name)}")
                created += 1
        except Exception as e:
            logger.warning(
                f"Failed to process diary item / 日记条目处理失败: "
                f"{item.get('name', '?')}: {e}"
            )
            results.append(f"⚠️{item.get('name', '?')}")

    return f"{len(items)}条|新{created}合{merged}\n" + "\n".join(results)


async def _trace_merge(target_id: str, source_id: str) -> str:
    """E4：把 source 桶合并进 target 桶。

    内容追加、标签去重、importance 取大、情感取平均，最后删除源桶。
    钉选/保护桶不能参与 merge。
    """
    if target_id == source_id:
        return "源桶与目标桶相同，无法合并。"
    target = await bucket_mgr.get(target_id)
    if not target:
        return f"未找到目标桶: {target_id}"
    source = await bucket_mgr.get(source_id)
    if not source:
        return f"未找到源桶: {source_id}"

    tm, sm = target["metadata"], source["metadata"]
    if tm.get("pinned") or tm.get("protected") or sm.get("pinned") or sm.get("protected"):
        return "merge 不能对钉选/保护桶使用。"

    merged_content = (
        (target.get("content", "") or "").rstrip()
        + f"\n\n--- 合并自 {sm.get('name', source_id)}({source_id}) ---\n"
        + (source.get("content", "") or "").lstrip()
    )
    merged_tags = list(dict.fromkeys(list(tm.get("tags", [])) + list(sm.get("tags", []))))
    merged_imp = max(int(tm.get("importance", 5)), int(sm.get("importance", 5)))
    merged_val = (float(tm.get("valence", 0.5)) + float(sm.get("valence", 0.5))) / 2
    merged_aro = (float(tm.get("arousal", 0.3)) + float(sm.get("arousal", 0.3))) / 2

    ok = await bucket_mgr.update(
        target_id,
        content=merged_content,
        tags=merged_tags,
        importance=merged_imp,
        valence=merged_val,
        arousal=merged_aro,
    )
    if not ok:
        return f"合并失败：无法更新目标桶 {target_id}"

    try:
        await embedding_engine.generate_and_store(target_id, merged_content)
    except Exception:
        pass

    del_ok = await bucket_mgr.delete(source_id)
    if del_ok:
        embedding_engine.delete_embedding(source_id)

    return (
        f"已合并 {source_id} → {target_id}"
        f"（标签{len(merged_tags)}个, 重要度{merged_imp}, V{merged_val:.2f}/A{merged_aro:.2f}）"
        f"{'，源桶已删除' if del_ok else '，但源桶删除失败'}"
    )


# =============================================================
# Tool 4: trace — Trace, redraw the outline of a memory
# 工具 4：trace — 描摹，重新勾勒记忆的轮廓
# Also handles deletion (delete=True), batch ops, and merge.
# 同时承接删除、批量操作、合并功能
# =============================================================
@mcp.tool()
async def trace(
    bucket_id: str,
    name: str = "",
    domain: str = "",
    valence: float = -1,
    arousal: float = -1,
    importance: int = -1,
    tags: str = "",
    resolved: int = -1,
    pinned: int = -1,
    digested: int = -1,
    content: str = "",
    delete: bool = False,
    merge: str = "",
    trigger_date: str = "",
    trigger_done: int = -1,
) -> str:
    """修改记忆元数据或内容。resolved=1沉底/0激活,pinned=1钉选/0取消,digested=1隐藏/0取消,content=替换正文,delete=True删除。只传需改的,-1或空=不改。bucket_id支持逗号分隔多个ID批量操作(批量模式下content和name忽略)。merge=另一个bucket_id时,把该源桶合并进bucket_id(内容追加/标签去重/重要度取大/情感取平均/删源桶,钉选桶不可merge)。dormant休眠桶始终可被trace访问修改。trigger_date=设/改触发日期(YYYY-MM-DD,也认「明天」「3月5日」「+7」;传none/clear/-撤销这条提醒),trigger_done=1标记「这条今日浮现已处理」不再重复浮现/0重新激活。"""

    ids = [x.strip() for x in (bucket_id or "").split(",") if x.strip()]
    if not ids:
        return "请提供有效的 bucket_id。"

    # --- E4: merge mode / 合并模式 ---
    if merge and merge.strip():
        return await _trace_merge(ids[0], merge.strip())

    is_batch = len(ids) > 1

    # --- Delete mode (batch-aware) / 删除模式（支持批量）---
    if delete:
        results = []
        for bid in ids:
            ok = await bucket_mgr.delete(bid)
            if ok:
                embedding_engine.delete_embedding(bid)
            results.append(f"{'已遗忘' if ok else '未找到'}:{bid}")
        return " | ".join(results) if is_batch else (
            f"已遗忘记忆桶: {ids[0]}" if "已遗忘" in results[0] else f"未找到记忆桶: {ids[0]}"
        )

    # --- Collect fields shared by single & batch (excludes name/content) ---
    # --- 收集单条/批量通用字段（不含 name/content）---
    common = {}
    if domain:
        common["domain"] = [d.strip() for d in domain.split(",") if d.strip()]
    if 0 <= valence <= 1:
        common["valence"] = valence
    if 0 <= arousal <= 1:
        common["arousal"] = arousal
    if 1 <= importance <= 10:
        common["importance"] = importance
    if tags:
        common["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if resolved in (0, 1):
        common["resolved"] = bool(resolved)
    if pinned in (0, 1):
        common["pinned"] = bool(pinned)
        if pinned == 1:
            common["importance"] = 10  # pinned → lock importance
    if digested in (0, 1):
        common["digested"] = bool(digested)
    # --- 触发日期:设/改/撤销 ---
    if trigger_date and trigger_date.strip():
        raw = trigger_date.strip()
        if raw.lower() in ("none", "clear", "-", "无", "取消"):
            common["trigger_date"] = ""      # 空串 = 撤销(连 trigger_done 一起清)
        else:
            normalized = _normalize_trigger_date(raw)
            if not normalized:
                return f"看不懂这个触发日期:{raw}。用 YYYY-MM-DD,或「明天」「3月5日」「+7」这类写法。"
            common["trigger_date"] = normalized
    if trigger_done in (0, 1):
        common["trigger_done"] = bool(trigger_done)

    # --- feel 不该被「解决」---
    # feel 是他写下的痕迹,不是待办。标成 resolved 会让它沉底、按已处理淡化 ——
    # 而一段感受本来就该留着它原来的形状,没有"处理完"这回事。
    # 整条拒绝而不是悄悄跳过:悄悄跳过他会以为改成功了。resolved=0(重新激活)不拦。
    if common.get("resolved") is True:
        feel_ids = []
        for bid in ids:
            try:
                b = await bucket_mgr.get(bid)
            except Exception:
                continue
            if b and b["metadata"].get("type") == "feel":
                feel_ids.append(bid)
        if feel_ids:
            return (
                f"没有改:{', '.join(feel_ids)} 是 feel。\n"
                "feel 是你留下的痕迹,不是待办 —— 它没有「解决」这回事,就该留着本来的形状。\n"
                "（真要让它别再出现,用 trace(digested=1) 隐藏;要改字就传 content。）"
            )

    # --- E3: batch mode (name & content ignored) / 批量模式（忽略 name/content）---
    if is_batch:
        if not common:
            return "批量模式下没有可修改的字段（name 和 content 在批量模式下被忽略）。"
        results = []
        ok_count = 0
        for bid in ids:
            b = await bucket_mgr.get(bid)
            if not b:
                results.append(f"未找到:{bid}")
                continue
            if await bucket_mgr.update(bid, **common):
                ok_count += 1
                results.append(f"已修改:{bid}")
            else:
                results.append(f"失败:{bid}")
        changed = ", ".join(f"{k}={v}" for k, v in common.items())
        return f"批量修改 {ok_count}/{len(ids)} 桶 [{changed}]:\n" + " | ".join(results)

    # --- Single mode / 单桶模式 ---
    bucket_id = ids[0]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return f"未找到记忆桶: {bucket_id}"

    updates = dict(common)
    if name:
        updates["name"] = name
    if content:
        updates["content"] = content

    if not updates:
        return "没有任何字段需要修改。"

    success = await bucket_mgr.update(bucket_id, **updates)
    if not success:
        return f"修改失败: {bucket_id}"

    # Re-generate embedding if content changed
    if "content" in updates:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception:
            pass

    changed = ", ".join(f"{k}={v}" for k, v in updates.items() if k != "content")
    if "content" in updates:
        changed += (", content=已替换" if changed else "content=已替换")
    # Explicit hint about resolved state change semantics
    # 特别提示 resolved 状态变化的语义
    if "resolved" in updates:
        if updates["resolved"]:
            changed += " → 已沉底，只在关键词触发时重新浮现"
        else:
            changed += " → 已重新激活，将参与浮现排序"
    if "digested" in updates:
        if updates["digested"]:
            changed += " → 已隐藏，保留但不再浮现"
        else:
            changed += " → 已取消隐藏，重新参与浮现"
    if "trigger_date" in updates:
        changed += (" → 到那天(或过期未处理)会出现在「今日浮现」"
                    if updates["trigger_date"] else " → 已撤销这条提醒")
    if "trigger_done" in updates:
        changed += (" → 已标记处理完，不再重复浮现"
                    if updates["trigger_done"] else " → 重新等待浮现")
    return f"已修改记忆桶 {bucket_id}: {changed}"


# =============================================================
# Tool 5: pulse — Heartbeat, system status + memory listing
# 工具 5：pulse — 脉搏，系统状态 + 记忆列表
# =============================================================
@mcp.tool()
async def digest(execute: bool = False, max_groups: int = 0) -> str:
    """把长期没被想起的低重要度碎片按语义分组,提炼成沉淀摘要桶。**默认只演习**。

    不传参 = 演习:只输出整理计划(哪几组、每组哪些桶、闲置多久),不动任何数据。
    execute=True 才真的整理:每组提炼成一条沉淀摘要桶,原桶**归档不删**(随时能捞回来),
    并互相写上关联。max_groups 限制这一轮最多整理几组(0=用默认上限)。
    钉选/保护/永久/feel/便利贴/带未处理触发日期的桶一律不参与。
    """
    await decay_engine.ensure_started()

    if not execute:
        plan = await digest_engine.plan()
        if not plan.get("groups"):
            return (f"演习:够条件的候选桶 {plan.get('candidates', 0)} 个,"
                    f"没有能成组的。{plan.get('note', '')}")
        lines = [
            f"=== 消化演习(没有改动任何记忆)===",
            f"候选桶 {plan['candidates']} 个,分组方式:{plan['method']},"
            f"本轮可整理 {len(plan['groups'])} 组",
        ]
        for i, g in enumerate(plan["groups"], 1):
            lines.append(f"\n第 {i} 组({g['size']} 条 → 1 条沉淀摘要)"
                         f" 主题:{','.join(g['domains']) or '-'}")
            if g["tags"]:
                lines.append(f"  共同标签:{','.join(g['tags'])}")
            for b in g["buckets"]:
                lines.append(f"  · [{b['id']}] {b['name']}"
                             f"(重要度{b['importance']},闲置{b['idle_days']}天)")
        lines.append("\n确认无误后用 digest(execute=True) 实际整理;原桶只归档不删除。")
        return "\n".join(lines)

    result = await digest_engine.execute(max_groups=max_groups or None)
    if result.get("error"):
        return f"没有执行:{result['error']}"
    if not result.get("groups"):
        return f"候选桶 {result.get('candidates', 0)} 个,没有能成组的,什么都没做。"

    lines = [f"=== 消化完成:{result['digested']}/{len(result['groups'])} 组 ==="]
    for g in result["groups"]:
        if g.get("ok"):
            lines.append(f"✅ 沉淀→[{g['sediment_id']}] {g.get('name', '')} "
                         f"(合了 {len(g['sources'])} 条,原桶已归档:{len(g.get('archived', []))})")
        else:
            lines.append(f"⏭️ 跳过一组({len(g.get('sources', []))} 条):{g.get('reason', '')}")
    lines.append("原桶都在 archive/ 里,要捞回来用 trace 或面板恢复。")
    return "\n".join(lines)


@mcp.tool()
async def pulse(include_archive: bool = False, show_all: bool = False) -> str:
    """系统状态+记忆桶列表。include_archive=True含归档。默认只显示全部钉选桶+非钉选桶随机抽样5个(每次调用结果不同),末尾附统计;show_all=True显示全部桶。"""
    try:
        stats = await bucket_mgr.get_stats()
    except Exception as e:
        return f"获取系统状态失败: {e}"

    status = (
        f"=== Ombre Brain 记忆系统 ===\n"
        f"固化记忆桶: {stats['permanent_count']} 个\n"
        f"动态记忆桶: {stats['dynamic_count']} 个\n"
        f"归档记忆桶: {stats['archive_count']} 个\n"
        f"总存储大小: {stats['total_size_kb']:.1f} KB\n"
        f"衰减引擎: {'运行中' if decay_engine.is_running else '已停止'}\n"
    )

    # --- List all bucket summaries / 列出所有桶摘要 ---
    try:
        buckets = await bucket_mgr.list_all(include_archive=include_archive)
    except Exception as e:
        return status + f"\n列出记忆桶失败: {e}"

    if not buckets:
        return status + "\n记忆库为空。"

    # --- E5: hide dormant buckets from default pulse (show_all reveals them) ---
    # --- 默认隐藏休眠桶；show_all=True 时一并显示 ---
    if not show_all:
        buckets = [b for b in buckets if not b.get("metadata", {}).get("dormant")]
    if not buckets:
        return status + "\n记忆库为空（仅有休眠桶，show_all=True 查看）。"

    # --- B2: default view = all pinned + 5 random non-pinned (no weight ranking) ---
    # --- 默认视图：全部钉选桶 + 非钉选桶随机抽样 5 个（不按权重排序，每次调用结果不同）---
    PULSE_RANDOM_SAMPLE = 5

    def _is_pinned(b):
        m = b.get("metadata", {})
        return bool(m.get("pinned") or m.get("protected"))

    total_count = len(buckets)
    if show_all:
        display_buckets = buckets
    else:
        pinned = [b for b in buckets if _is_pinned(b)]
        non_pinned = [b for b in buckets if not _is_pinned(b)]
        sampled = random.sample(non_pinned, min(PULSE_RANDOM_SAMPLE, len(non_pinned)))
        display_buckets = pinned + sampled

    lines = []
    for b in display_buckets:
        meta = b.get("metadata", {})
        if meta.get("pinned") or meta.get("protected"):
            icon = "📌"
        elif meta.get("type") == "permanent":
            icon = "📦"
        elif meta.get("type") == "feel":
            icon = "🫧"
        elif meta.get("type") == "archived":
            icon = "🗄️"
        elif meta.get("resolved", False):
            icon = "✅"
        else:
            icon = "💭"
        try:
            score = decay_engine.calculate_score(meta)
        except Exception:
            score = 0.0
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        resolved_tag = " [已解决]" if meta.get("resolved", False) else ""
        if meta.get("dormant"):
            resolved_tag += " [休眠]"
        lines.append(
            f"{icon} [{meta.get('name', b['id'])}]{resolved_tag} "
            f"bucket_id:{b['id']} "
            f"主题:{domains} "
            f"情感:V{val:.1f}/A{aro:.1f} "
            f"重要:{meta.get('importance', '?')} "
            f"权重:{score:.2f} "
            f"标签:{','.join(meta.get('tags', []))}"
        )

    footer = f"\n=== 统计 ===\n显示 {len(display_buckets)}/{total_count} 个记忆桶"
    if not show_all and len(display_buckets) < total_count:
        footer += f"（隐藏 {total_count - len(display_buckets)} 个,show_all=True 查看全部）"

    return status + "\n=== 记忆列表 ===\n" + "\n".join(lines) + footer


# =============================================================
# Tool 6: dream — Dreaming, digest recent memories
# 工具 6：dream — 做梦，消化最近的记忆
#
# Reads recent surface-level buckets (≤10), returns them for
# Claude to introspect under prompt guidance.
# 读取最近新增的表层桶（≤10个），返回给 Claude 在提示词引导下自主思考。
# Claude then decides: resolve some, write feels, or do nothing.
# =============================================================
@mcp.tool()
async def dream(detail_ids: str = "") -> str:
    """做梦——读取最近新增的记忆桶,供你自省。默认返回最近5个桶的摘要省token;detail_ids(逗号分隔的bucket_id)指定的桶返回全文,其余仅摘要。另附「旧事重提」:几条很久没被想起、但当初记得很牢的旧记忆(带一小段正文,想看全文把它的ID传进detail_ids;env RESURFACE_N可调,设0关闭)。读完后可以trace(resolved=1)放下,或hold(feel=True)写感受。"""
    await decay_engine.ensure_started()
    detail_set = {d.strip() for d in detail_ids.split(",") if d.strip()}

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Dream failed to list buckets: {e}")
        return "记忆系统暂时无法访问。"

    # --- Filter: recent surface-level dynamic buckets (not permanent/pinned/feel) ---
    candidates = [
        b for b in all_buckets
        if b["metadata"].get("type") not in ("permanent", "feel")
        and not b["metadata"].get("pinned", False)
        and not b["metadata"].get("protected", False)
    ]

    # --- Sort by creation time desc, take top 5 ---
    # --- B3: 默认返回最近 5 个桶的摘要，detail_ids 指定的桶返回全文 ---
    candidates.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
    recent = candidates[:5]

    # --- 旧事重提:很久没被想起、但当初记得很牢的旧记忆 ---
    # 放在这里(而不是并进 recent)是因为两者的挑选逻辑正好相反:
    # recent 看「新」,这条看「久」。合成一个列表就必然被新的挤掉。
    resurfaced = []
    if RESURFACE_N > 0:
        try:
            # 单独取一次带 archive/ 的全量:旧事多半已经淡出去了(见下)
            with_archive = await bucket_mgr.list_all(include_archive=True)
        except Exception as e:
            logger.warning(f"Resurface list failed / 旧事重提列桶失败: {e}")
            with_archive = all_buckets
        resurfaced = _resurface_candidates(
            with_archive, exclude_ids={b["id"] for b in recent}, limit=RESURFACE_N
        )

    # recent 空 ⟹ candidates 空 ⟹ 旧事重提也必然为空(它的候选是 candidates 的子集)
    if not recent:
        return "没有需要消化的新记忆。"

    parts = []
    for b in recent:
        meta = b["metadata"]
        resolved_tag = " [已解决]" if meta.get("resolved", False) else " [未解决]"
        domains = ",".join(meta.get("domain", []))
        val = meta.get("valence", 0.5)
        aro = meta.get("arousal", 0.3)
        created = meta.get("created", "")
        head = (
            f"[{meta.get('name', b['id'])}]{resolved_tag} "
            f"主题:{domains} V{val:.1f}/A{aro:.1f} "
            f"创建:{created}\n"
            f"ID: {b['id']}"
        )
        if b["id"] in detail_set:
            # 指定桶：返回全文
            parts.append(head + "\n" + strip_wikilinks(b["content"]))
        else:
            # 其余桶：仅摘要（不返回 content 全文）
            parts.append(head)

    header = (
        "=== Dreaming ===\n"
        "以下是你最近的记忆。用第一人称想：\n"
        "- 这些东西里有什么在你这里留下了重量？\n"
        "- 有什么还没想清楚？\n"
        "- 有什么可以放下了？\n"
        "想完之后：值得放下的用 trace(bucket_id, resolved=1)；\n"
        "有沉淀的用 hold(content=\"...\", feel=True, source_bucket=\"bucket_id\", valence=你的感受) 写下来。\n"
        "valence 是你对这段记忆的感受，不是事件本身的情绪。\n"
        "没有沉淀就不写，不强迫产出。\n"
    )

    # --- Connection hint: find most similar pair via embeddings ---
    connection_hint = ""
    if embedding_engine and embedding_engine.enabled and len(recent) >= 2:
        try:
            best_pair = None
            best_sim = 0.0
            ids = [b["id"] for b in recent]
            names = {b["id"]: b["metadata"].get("name", b["id"]) for b in recent}
            embeddings = {}
            for bid in ids:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb
            for i, id_a in enumerate(ids):
                for id_b in ids[i+1:]:
                    if id_a in embeddings and id_b in embeddings:
                        sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                        if sim > best_sim:
                            best_sim = sim
                            best_pair = (id_a, id_b)
            if best_pair and best_sim > 0.5:
                connection_hint = (
                    f"\n💭 [{names[best_pair[0]]}] 和 [{names[best_pair[1]]}] "
                    f"似乎有关联 (相似度:{best_sim:.2f})——不替你下结论，你自己想。\n"
                )
        except Exception as e:
            logger.warning(f"Dream connection hint failed: {e}")

    # --- Feel crystallization hint: detect repeated feel themes ---
    crystal_hint = ""
    if embedding_engine and embedding_engine.enabled:
        try:
            feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
            if len(feels) >= 3:
                feel_embeddings = {}
                for f in feels:
                    emb = await embedding_engine.get_embedding(f["id"])
                    if emb is not None:
                        feel_embeddings[f["id"]] = emb
                # Find clusters: feels with similarity > 0.7 to at least 2 others
                for fid, femb in feel_embeddings.items():
                    similar_feels = []
                    for oid, oemb in feel_embeddings.items():
                        if oid != fid:
                            sim = embedding_engine._cosine_similarity(femb, oemb)
                            if sim > 0.7:
                                similar_feels.append(oid)
                    if len(similar_feels) >= 2:
                        feel_bucket = next((f for f in feels if f["id"] == fid), None)
                        if feel_bucket and not feel_bucket["metadata"].get("pinned"):
                            content_preview = strip_wikilinks(feel_bucket["content"][:80])
                            crystal_hint = (
                                f"\n🔮 你已经写过 {len(similar_feels)+1} 条相似的 feel "
                                f"（围绕「{content_preview}…」）。"
                                f"如果这已经是确信而不只是感受了，"
                                f"你可以用 hold(content=\"...\", pinned=True) 升级它。"
                                f"不急，你自己决定。\n"
                            )
                            break
        except Exception as e:
            logger.warning(f"Dream crystallization hint failed: {e}")

    # --- 旧事重提段:给一小段正文,不然只有标题行他认不出是哪件事 ---
    resurface_block = ""
    if resurfaced:
        r_parts = []
        for b in resurfaced:
            meta = b["metadata"]
            body = strip_wikilinks(b["content"])
            if b["id"] in detail_set:
                excerpt = body
            elif RESURFACE_EXCERPT_CHARS > 0:
                excerpt = body[:RESURFACE_EXCERPT_CHARS]
                if len(body) > RESURFACE_EXCERPT_CHARS:
                    excerpt += "…"
            else:
                excerpt = ""
            head = (
                f"[{meta.get('name', b['id'])}]"
                f"{' [已淡出]' if b.get('faded') else ''} "
                f"约 {int(b['idle_days'])} 天没想起 "
                f"V{float(meta.get('valence', 0.5)):.1f}/A{float(meta.get('arousal', 0.3)):.1f}\n"
                f"ID: {b['id']}"
            )
            r_parts.append(head + ("\n" + excerpt if excerpt else ""))
        resurface_block = (
            "\n\n=== 旧事重提 ===\n"
            "这些是很久没被想起、但当初对你有分量的事。不是待办,也不要求你做什么——\n"
            "只是你可能会想起来。想看全文:dream(detail_ids=\"上面的ID\")。\n"
            "标着[已淡出]的是随时间沉下去的记忆,还在,只是平时不会自己浮上来。\n\n"
            + "\n---\n".join(r_parts)
        )
        # 记下「刚重提过」,冷却期内不再翻出来。这个标记不碰 last_active、
        # 不参与算分 —— 重提一件旧事不该反过来让它显得"刚被想起"。
        for b in resurfaced:
            try:
                await bucket_mgr.mark_resurfaced(b["id"])
            except Exception as e:
                logger.warning(f"Mark resurfaced failed / 标记重提失败: {b['id']}: {e}")

    final_text = header + "\n---\n".join(parts) + connection_hint + crystal_hint + resurface_block
    await _fire_webhook(
        "dream",
        {"recent": len(recent), "resurfaced": len(resurfaced), "chars": len(final_text)},
    )
    return final_text


# =============================================================
# Tool 9: feel — Recall feels you left before, by what you're thinking now
# 工具 9：feel — 按当下在想的事,找回以前留下的感受
#
# 为什么不是「列出全部 feel」:breath(domain="feel") 已经能全捞,但那是个
# 清单——他得自己从一堆无关的感受里翻。feel 不是列表,是「我此刻在想的
# 这件事,我以前怎么感受的」。所以 query 必填,而且宁可返回「没有」也不
# 用低相关的凑数:拿别的感受充数,等于让他把不属于这件事的想法认成自己的。
# 命中后逐字返回、不脱水——feel 本来就是一两句话,再摘要就什么都不剩了。
# （编号接在最后:插在 dream 后面是因为两者是一对,不动既有工具的编号。）
# =============================================================
@mcp.tool()
async def feel(query: str, max_results: int = 5) -> str:
    """按关键词找回你以前留下的感受。query必填——feel不是列表,是「我此刻在想的这件事,我以前怎么感受的」。走向量检索(候选只在feel桶内,相似度≥env FEEL_SIM_THRESHOLD,默认0.65才算命中),换个说法也能找回;向量不可用时退回字面匹配并明说降级。命中后逐字返回不摘要;没命中就说没有,不用低相关的凑数。max_results默认5(上限20),总量另受 env FEEL_MAX_TOKENS 约束。写感受仍走 hold(feel=True, source_bucket=...);想按时间通读全部用 breath(domain="feel")。"""
    await decay_engine.ensure_started()

    query = (query or "").strip()
    if not query:
        return (
            "feel 要一个关键词:你此刻在想的是什么?\n"
            "（想按时间通读全部感受,用 breath(domain=\"feel\")。）"
        )

    max_results = max(1, min(max_results, 20))

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        logger.error(f"Feel failed to list buckets / feel 列桶失败: {e}")
        return "记忆系统暂时无法访问。"

    feels = [b for b in all_buckets if b["metadata"].get("type") == "feel"]
    if not feels:
        return "还没有留下过 feel。"

    by_id = {b["id"]: b for b in feels}
    degraded = ""

    # --- 向量通道:候选只在 feel 桶内(全库 top_k 会把 feel 整个挤掉)---
    scored = await embedding_engine.search_within(
        query, list(by_id.keys()), min_sim=FEEL_SIM_THRESHOLD
    )

    # --- 降级:向量用不了才走字面匹配,并且明说降级了 ---
    # 不明说的话,他会以为"没搜到=真的没有过这种感受",而实际上只是换了个说法。
    if scored is None:
        degraded = "（向量检索不可用,这次是字面匹配——换个说法就可能找不到。）\n"
        from rapidfuzz import fuzz
        scored = []
        for b in feels:
            text = strip_wikilinks(b["content"])
            ratio = max(
                fuzz.partial_ratio(query, text),
                fuzz.token_set_ratio(query, text),
            ) / 100.0
            if ratio >= bucket_mgr.fuzzy_threshold / 100.0:
                scored.append((b["id"], ratio))
        scored.sort(key=lambda x: x[1], reverse=True)

    if not scored:
        return (
            f"{degraded}没有找到和「{query}」相关的 feel。\n"
            f"（一共留下过 {len(feels)} 条,但没有一条够相关——不拿别的凑数。）"
        )

    # --- 命中:逐字返回,不脱水 ---
    parts = []
    token_used = 0
    for bucket_id, sim in scored[:max_results]:
        b = by_id.get(bucket_id)
        if not b:
            continue
        created = b["metadata"].get("created", "")
        # 降级时这个分是字面吻合度,不是余弦相似度 —— 标签跟着换,别让他把两种分当成一回事
        score_label = "字面吻合" if degraded else "相似度"
        entry = (
            f"[{created}] [{score_label} {sim:.2f}] [bucket_id:{bucket_id}]\n"
            f"{strip_wikilinks(b['content'])}"
        )
        entry_tokens = count_tokens_approx(entry)
        if parts and token_used + entry_tokens > FEEL_MAX_TOKENS:
            break
        parts.append(entry)
        token_used += entry_tokens

    omitted = len(scored) - len(parts)
    tail = f"\n\n（另有 {omitted} 条也相关,没全展开。）" if omitted > 0 else ""
    await _fire_webhook("feel", {"query_chars": len(query), "hits": len(parts)})
    return (
        f"{degraded}=== 你以前对这件事的感受 ===\n"
        + "\n---\n".join(parts)
        + tail
    )


# =============================================================
# Tool 7: todos — Pending action items across unresolved buckets
# 工具 7：todos — 汇总未解决桶里的待办事项
# =============================================================
@mcp.tool()
async def todos() -> str:
    """汇总所有未 resolved 桶的待办事项,按桶分组返回(附桶名+重要度)。待办来源:桶的 todos 元字段 + 正文中未勾选的 markdown 复选框 `- [ ]`。"""
    await decay_engine.ensure_started()
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
    except Exception as e:
        return f"记忆系统暂时无法访问: {e}"

    groups = []
    for b in all_buckets:
        meta = b["metadata"]
        if meta.get("resolved", False):
            continue
        items = _extract_todos(b)
        if items:
            groups.append((b, items))

    if not groups:
        return "没有未完成的待办事项。"

    # 按重要度降序分组
    groups.sort(key=lambda g: int(g[0]["metadata"].get("importance", 5)), reverse=True)

    parts = []
    total = 0
    for b, items in groups:
        meta = b["metadata"]
        total += len(items)
        head = (
            f"[{meta.get('name', b['id'])}] "
            f"重要:{meta.get('importance', '?')} "
            f"bucket_id:{b['id']}"
        )
        body = "\n".join(f"  ☐ {it}" for it in items)
        parts.append(head + "\n" + body)

    return f"=== 待办清单（{len(groups)}桶 / {total}项）===\n" + "\n".join(parts)


# =============================================================
# Tool 8: archive_session — Persist a conversation summary into archive
# 工具 8：archive_session — 将对话摘要存入归档
# =============================================================
@mcp.tool()
async def archive_session(
    summary: str,
    highlights: str = "",
    mood: str = "",
    valence: float = -1,
    arousal: float = -1,
) -> str:
    """将【自上次归档以来】的新对话摘要存入归档区。

    ⚠️ 增量归档:summary 只写「上次归档之后」发生的新内容,不要把已经归档过的
    对话(比如今天早些时候归过的那段)再抄一遍——否则同一段会被记两遍。
    不确定上次归到哪,就以返回里的「上次归档」时刻为界,只总结那之后的。

    同一天多次归档会合并进「会话归档 YYYY-MM-DD」这一个档案里(按时刻分节追加),
    不会每次新建。所以放心随时归,不会把一天弄碎。

    summary必需;highlights(亮点)/mood(心情)可选;valence/arousal 0~1可选(-1=用默认)。"""
    if not summary or not summary.strip():
        return "summary 不能为空。"
    await decay_engine.ensure_started()

    # 查上次会话归档的时刻，作为增量边界提示回给沈渡（他据此判断"这次只记哪段"）
    last_archive_label = ""
    try:
        _all = await bucket_mgr.list_all(include_archive=True)
        _prev = [
            b for b in _all
            if b["metadata"].get("type") == "archived"
            and "session" in (b["metadata"].get("tags") or [])
        ]
        if _prev:
            _prev.sort(key=_archived_sort_key, reverse=True)
            _pm = _prev[0]["metadata"]
            last_archive_label = str(
                _pm.get("archived_at") or _pm.get("created") or ""
            )[:16].replace("T", " ")
    except Exception as e:
        logger.warning(f"archive_session last-archive lookup failed: {e}")

    v = valence if 0 <= valence <= 1 else 0.5
    a = arousal if 0 <= arousal <= 1 else 0.3
    _now = now_iso()
    today = _now[:10]            # YYYY-MM-DD
    hhmm = _now[11:16]           # HH:MM

    # 这一次归档的正文段落(合并模式下作为当天档案里的一节)
    seg = [f"## {hhmm}\n{summary.strip()}"]
    if highlights and highlights.strip():
        seg.append(f"\n**亮点**:{highlights.strip()}")
    if mood and mood.strip():
        seg.append(f"\n**心情**:{mood.strip()}")
    segment = "\n".join(seg)

    # ── 按天合并:同一天只有一个档案,当天再次归档就往里追加一节 ──────────────
    # 为什么:每次归档都新建一个桶 → 一天下来碎成好几份,浮现时既占名额又读不出连贯。
    # 合并后「最近 N 条归档」= 最近 N 天,连续性明显更好。
    # 跨天(或 ARCHIVE_MERGE_BY_DAY=0)才开新档。
    day_name = f"会话归档 {today}"
    target = None
    if ARCHIVE_MERGE_BY_DAY:
        try:
            for b in await bucket_mgr.list_all(include_archive=True):
                m = b["metadata"]
                if m.get("type") == "archived" and m.get("name") == day_name:
                    target = b
                    break
        except Exception as e:
            logger.warning(f"archive_session day-bucket lookup failed: {e}")

    if target:
        # 追加:正文接一节;archived_at 刷到本次,否则排序与「上次归档」边界仍停在当天首次
        bucket_id = target["id"]
        content = target["content"].rstrip() + "\n\n" + segment
        try:
            if not await bucket_mgr.update(bucket_id, content=content, valence=v, arousal=a):
                raise RuntimeError("update 返回 False(没找到该桶)")
            await bucket_mgr.touch_archived_at(bucket_id, _now)
        except Exception as e:
            logger.error(f"archive_session append failed: {e}")
            return f"归档失败(追加): {e}"
        name, merged = day_name, True
    else:
        if ARCHIVE_MERGE_BY_DAY:
            content, name = f"# {day_name}\n\n{segment}", day_name
        else:
            legacy = [f"# 会话摘要\n{summary.strip()}"]
            if highlights and highlights.strip():
                legacy.append(f"\n## 亮点\n{highlights.strip()}")
            if mood and mood.strip():
                legacy.append(f"\n## 心情\n{mood.strip()}")
            content = "\n".join(legacy)
            name = f"会话归档 {_now[:16].replace('T', ' ')}"
        merged = False
        try:
            bucket_id = await bucket_mgr.create(
                content=content,
                tags=["会话", "归档", "session"],
                importance=4,
                domain=["归档"],
                valence=v,
                arousal=a,
                name=name,
                bucket_type="dynamic",
            )
        except Exception as e:
            logger.error(f"archive_session create failed: {e}")
            return f"归档失败: {e}"

    try:
        await embedding_engine.generate_and_store(bucket_id, content)
    except Exception:
        pass

    # 追加进已归档的当天档案时不要再 archive 一次:它已经在归档区,
    # 再走一遍只会重写 archived_at(把刚 touch 的值又盖掉)并做无谓的文件移动。
    if merged:
        status = "已追加进今天的档案"
    else:
        archived = False
        try:
            archived = await bucket_mgr.archive(bucket_id)
        except Exception as e:
            logger.warning(f"archive_session archive move failed: {e}")
        status = "已存入归档" if archived else "已创建(归档移动失败,暂留动态区)"

    prev_hint = f"｜上次归档: {last_archive_label}" if last_archive_label else "｜上次归档: 无(首次)"
    return f"🗄️{status} → {bucket_id}｜{name}｜V{v:.1f}/A{a:.1f}{prev_hint}"


# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# 仪表板 API（轻量 Web UI 用）
# =============================================================
@mcp.custom_route("/api/buckets", methods=["GET"])
async def api_buckets(request):
    """List all buckets with metadata (no content for efficiency)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=True)
        result = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "model_valence": meta.get("model_valence"),
                "importance": meta.get("importance", 5),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "activation_count": meta.get("activation_count", 1),
                "score": decay_engine.calculate_score(meta),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        result.sort(key=lambda x: x["score"], reverse=True)
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["GET"])
async def api_bucket_detail(request):
    """Get full bucket content by ID."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    bucket = await bucket_mgr.get(bucket_id)
    if not bucket:
        return JSONResponse({"error": "not found"}, status_code=404)
    meta = bucket.get("metadata", {})
    # raw=1 给编辑器用:保留 [[双链]] 原样,免得一存就把链接洗没了
    raw = request.query_params.get("raw") in ("1", "true", "yes")
    content = bucket.get("content", "")
    return JSONResponse({
        "id": bucket["id"],
        "metadata": meta,
        "content": content if raw else strip_wikilinks(content),
        "score": decay_engine.calculate_score(meta),
    })


# =============================================================
# 面板上直接删改记忆(2026-08-06)
#
# 在此之前面板只读:错记的、重复的、不想留的,只能等衰减或者让沈渡自己改。
# 现在栖栖可以直接改内容/元数据,也可以删——删是软删除,先进回收站(base_dir/trash),
# 回收站不在检索目录里,所以沈渡当场就想不起来了,但栖栖能后悔。
# =============================================================

# 允许面板改的字段 → 归一化函数。没列进来的(id/created/type/activation_count…)一律不给改。
def _norm_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [s.strip() for s in str(v).replace("，", ",").split(",") if s.strip()]


_EDITABLE_FIELDS = {
    "name": lambda v: str(v).strip(),
    "content": lambda v: str(v),
    "tags": _norm_list,
    "domain": _norm_list,
    "importance": lambda v: max(1, min(10, int(v))),
    "valence": lambda v: max(0.0, min(1.0, float(v))),
    "arousal": lambda v: max(0.0, min(1.0, float(v))),
    "model_valence": lambda v: max(0.0, min(1.0, float(v))),
    "resolved": lambda v: bool(v),
    "pinned": lambda v: bool(v),
    "digested": lambda v: bool(v),
}


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["POST"])
async def api_bucket_edit(request):
    """Edit a bucket's content / metadata from the dashboard."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    updates = {}
    for key, normalize in _EDITABLE_FIELDS.items():
        if key not in body or body[key] is None:
            continue
        try:
            updates[key] = normalize(body[key])
        except (TypeError, ValueError):
            return JSONResponse({"error": f"字段 {key} 的值不合法"}, status_code=400)

    if not updates:
        return JSONResponse({"error": "没有可改的字段"}, status_code=400)
    if "name" in updates and not updates["name"]:
        return JSONResponse({"error": "名字不能为空"}, status_code=400)
    if "content" in updates and not updates["content"].strip():
        return JSONResponse({"error": "内容不能为空,想清空就删掉整个桶"}, status_code=400)

    existing = await bucket_mgr.get(bucket_id)
    if not existing:
        return JSONResponse({"error": "not found"}, status_code=404)

    ok = await bucket_mgr.update(bucket_id, **updates)
    if not ok:
        return JSONResponse({"error": "更新失败"}, status_code=500)

    # 内容变了就重算 embedding,否则语义检索还按旧内容找它
    if "content" in updates and embedding_engine and embedding_engine.enabled:
        try:
            await embedding_engine.generate_and_store(bucket_id, updates["content"])
        except Exception as e:
            logger.warning(f"Embedding refresh failed after edit / 改完重算向量失败: {bucket_id}: {e}")

    bucket = await bucket_mgr.get(bucket_id)
    meta = bucket.get("metadata", {}) if bucket else {}
    # pinned 桶的 importance 被 bucket_manager 锁死在 10,这里把真实结果回给前端
    return JSONResponse({
        "ok": True,
        "id": bucket_id,
        "updated": sorted(updates.keys()),
        "metadata": meta,
        "content": bucket.get("content", "") if bucket else "",  # 原样(含双链),给编辑器回显
        "score": decay_engine.calculate_score(meta),
    })


@mcp.custom_route("/api/bucket/{bucket_id}", methods=["DELETE"])
async def api_bucket_delete(request):
    """Move a bucket to the trash (recoverable). ?hard=1 erases it for good."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    hard = request.query_params.get("hard") in ("1", "true", "yes")

    if hard:
        ok = await bucket_mgr.delete(bucket_id)
        if not ok:
            return JSONResponse({"error": "not found"}, status_code=404)
        if embedding_engine and embedding_engine.enabled:
            try:
                embedding_engine.delete_embedding(bucket_id)
            except Exception as e:
                logger.warning(f"Embedding delete failed / 删向量失败: {bucket_id}: {e}")
        return JSONResponse({"ok": True, "id": bucket_id, "hard": True})

    info = await bucket_mgr.soft_delete(bucket_id)
    if not info:
        return JSONResponse({"error": "not found"}, status_code=404)
    # 向量一起撤掉,否则语义检索会捞到一个已经不存在的 id
    if embedding_engine and embedding_engine.enabled:
        try:
            embedding_engine.delete_embedding(bucket_id)
        except Exception as e:
            logger.warning(f"Embedding delete failed / 删向量失败: {bucket_id}: {e}")
    return JSONResponse({"ok": True, "hard": False, **info})


@mcp.custom_route("/api/trash", methods=["GET"])
async def api_trash_list(request):
    """List trashed buckets."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        return JSONResponse(await bucket_mgr.list_trash())
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/trash", methods=["DELETE"])
async def api_trash_empty(request):
    """Empty the trash — irreversible."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    purged = await bucket_mgr.purge_all_trash()
    if embedding_engine and embedding_engine.enabled:
        for bid in purged:
            try:
                embedding_engine.delete_embedding(bid)
            except Exception:
                pass
    return JSONResponse({"ok": True, "purged": len(purged)})


@mcp.custom_route("/api/trash/{bucket_id}/restore", methods=["POST"])
async def api_trash_restore(request):
    """Restore a trashed bucket back to where it came from."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    ok = await bucket_mgr.restore_from_trash(bucket_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    # 补回向量:删的时候撤掉了,不补回来它就只能靠关键词被想起
    bucket = await bucket_mgr.get(bucket_id)
    if bucket and embedding_engine and embedding_engine.enabled:
        try:
            await embedding_engine.generate_and_store(bucket_id, bucket.get("content", ""))
        except Exception as e:
            logger.warning(f"Embedding restore failed / 恢复向量失败: {bucket_id}: {e}")
    return JSONResponse({"ok": True, "id": bucket_id})


@mcp.custom_route("/api/trash/{bucket_id}", methods=["DELETE"])
async def api_trash_purge(request):
    """Erase one trashed bucket for good."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    bucket_id = request.path_params["bucket_id"]
    ok = await bucket_mgr.purge_trash(bucket_id)
    if not ok:
        return JSONResponse({"error": "not found"}, status_code=404)
    if embedding_engine and embedding_engine.enabled:
        try:
            embedding_engine.delete_embedding(bucket_id)
        except Exception:
            pass
    return JSONResponse({"ok": True, "id": bucket_id})


@mcp.custom_route("/api/search", methods=["GET"])
async def api_search(request):
    """Search buckets by query."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    if not query:
        return JSONResponse({"error": "missing q parameter"}, status_code=400)
    try:
        matches = await bucket_mgr.search(query, limit=30)
        result = []
        for b in matches:
            meta = b.get("metadata", {})
            # 面板的搜索结果和记忆列表用同一套卡片来画,所以这里要给齐同样的字段。
            # 少给的后果是可见的:2026-08-31 前这里没有 last_active/type/importance,
            # 搜索结果的时间就成了一个「—」,月相和「心情」标记也全退化。
            result.append({
                "id": b["id"],
                "name": meta.get("name", b["id"]),
                "score": b.get("score", 0),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "tags": meta.get("tags", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "importance": meta.get("importance", 5),
                "pinned": meta.get("pinned", False),
                "resolved": meta.get("resolved", False),
                "digested": meta.get("digested", False),
                "created": meta.get("created", ""),
                "last_active": meta.get("last_active", ""),
                "content_preview": strip_wikilinks(b.get("content", ""))[:200],
            })
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/network", methods=["GET"])
async def api_network(request):
    """Get embedding similarity network for visualization."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        nodes = []
        edges = []
        embeddings = {}

        for b in all_buckets:
            meta = b.get("metadata", {})
            bid = b["id"]
            nodes.append({
                "id": bid,
                "name": meta.get("name", bid),
                "type": meta.get("type", "dynamic"),
                "domain": meta.get("domain", []),
                "valence": meta.get("valence", 0.5),
                "arousal": meta.get("arousal", 0.3),
                "score": decay_engine.calculate_score(meta),
                "resolved": meta.get("resolved", False),
                "pinned": meta.get("pinned", False),
                "digested": meta.get("digested", False),
            })
            if embedding_engine and embedding_engine.enabled:
                emb = await embedding_engine.get_embedding(bid)
                if emb is not None:
                    embeddings[bid] = emb

        # Build edges from embeddings (similarity > 0.5)
        ids = list(embeddings.keys())
        for i, id_a in enumerate(ids):
            for id_b in ids[i+1:]:
                sim = embedding_engine._cosine_similarity(embeddings[id_a], embeddings[id_b])
                if sim > 0.5:
                    edges.append({"source": id_a, "target": id_b, "similarity": round(sim, 3)})

        return JSONResponse({"nodes": nodes, "edges": edges})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/breath-debug", methods=["GET"])
async def api_breath_debug(request):
    """Debug endpoint: simulate breath scoring and return per-bucket breakdown."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    query = request.query_params.get("q", "")
    q_valence = request.query_params.get("valence")
    q_arousal = request.query_params.get("arousal")
    q_valence = float(q_valence) if q_valence else None
    q_arousal = float(q_arousal) if q_arousal else None

    try:
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        results = []
        w = {
            "topic": bucket_mgr.w_topic,
            "emotion": bucket_mgr.w_emotion,
            "time": bucket_mgr.w_time,
            "importance": bucket_mgr.w_importance,
        }
        w_sum = sum(w.values())

        for bucket in all_buckets:
            meta = bucket.get("metadata", {})
            bid = bucket["id"]
            try:
                topic = bucket_mgr._calc_topic_score(query, bucket) if query else 0.0
                emotion = bucket_mgr._calc_emotion_score(q_valence, q_arousal, meta)
                time_s = bucket_mgr._calc_time_score(meta)
                imp = max(1, min(10, int(meta.get("importance", 5)))) / 10.0

                raw_total = (
                    topic * w["topic"]
                    + emotion * w["emotion"]
                    + time_s * w["time"]
                    + imp * w["importance"]
                )
                normalized = (raw_total / w_sum) * 100 if w_sum > 0 else 0
                resolved = meta.get("resolved", False)
                if resolved:
                    normalized *= 0.3

                results.append({
                    "id": bid,
                    "name": meta.get("name", bid),
                    "domain": meta.get("domain", []),
                    "type": meta.get("type", "dynamic"),
                    "resolved": resolved,
                    "pinned": meta.get("pinned", False),
                    "scores": {
                        "topic": round(topic, 4),
                        "emotion": round(emotion, 4),
                        "time": round(time_s, 4),
                        "importance": round(imp, 4),
                    },
                    "weights": w,
                    "raw_total": round(raw_total, 4),
                    "normalized": round(normalized, 2),
                    "passed_threshold": normalized >= bucket_mgr.fuzzy_threshold,
                })
            except Exception:
                continue

        results.sort(key=lambda x: x["normalized"], reverse=True)
        passed = [r for r in results if r["passed_threshold"]]
        return JSONResponse({
            "query": query,
            "valence": q_valence,
            "arousal": q_arousal,
            "weights": w,
            "threshold": bucket_mgr.fuzzy_threshold,
            "total_candidates": len(results),
            "passed_count": len(passed),
            "results": results[:50],  # top 50 for debug
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# 桌面图标 / 添加到主屏幕(2026-08-31)
#
# 栖栖只有手机:她把 /dashboard 用 Safari「添加到主屏幕」之后,iOS 会来抓
# apple-touch-icon 指的那张 PNG 当图标。图和 manifest 都在 app_icon.py 里
# (base64,不是几个 .png 文件 —— 理由见那个文件顶部:Zeabur 构建计划缓存)。
#
# 这几个口子**不鉴权**:图标和 manifest 里没有任何记忆内容,而 iOS 抓图标时
# 不一定带 cookie,要鉴权就会变成默认的灰色截图图标。
# =============================================================

def _icon_response(size: int):
    from starlette.responses import Response
    from app_icon import ICONS
    return Response(
        ICONS[size],
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=604800"},
    )


@mcp.custom_route("/icon-180.png", methods=["GET"])
async def icon_180(request):
    """iPhone 桌面图标(iOS 真正会用的那张)。"""
    return _icon_response(180)


@mcp.custom_route("/icon-192.png", methods=["GET"])
async def icon_192(request):
    """PWA manifest 用。"""
    return _icon_response(192)


@mcp.custom_route("/icon-512.png", methods=["GET"])
async def icon_512(request):
    """PWA manifest 用(大图 / 启动画面)。"""
    return _icon_response(512)


@mcp.custom_route("/icon-32.png", methods=["GET"])
async def icon_32(request):
    """浏览器标签页小图标。"""
    return _icon_response(32)


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request):
    """老浏览器会直接要 /favicon.ico —— 给它同一张 32px 的 PNG。"""
    return _icon_response(32)


@mcp.custom_route("/manifest.webmanifest", methods=["GET"])
async def web_manifest(request):
    """PWA manifest:决定加到主屏后的名字、底色、是否全屏。"""
    from starlette.responses import JSONResponse
    from app_icon import WEB_MANIFEST
    return JSONResponse(
        WEB_MANIFEST,
        media_type="application/manifest+json",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@mcp.custom_route("/dashboard", methods=["GET"])
async def dashboard(request):
    """Serve the dashboard HTML page."""
    from starlette.responses import HTMLResponse
    import os
    dashboard_path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    try:
        with open(dashboard_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("<h1>dashboard.html not found</h1>", status_code=404)


# =============================================================
# 记忆银河 / Memory Galaxy(2026-08-29)
#
# 把记忆库渲染成一片可穿梭的 3D 星系:一条记忆一颗星,created 决定它离银河
# 中心多远(最早的那条就是核心),importance/pinned 决定大小亮度,domain 决定颜色。
# 页面是 galaxy.html,数据走下面的 /api/galaxy——所以不用导出脚本、不用 cron,
# 新存的记忆下次打开就是新的一颗星。
#
# 鉴权:数据接口和面板共用一把锁(_require_auth);页面本身不鉴权,拿不到数据时
# 前端自己跳去 /dashboard 登录。真实记忆不落进仓库,只在运行时从卷里读。
# =============================================================

@mcp.custom_route("/galaxy", methods=["GET"])
async def galaxy_page(request):
    """
    Serve the memory galaxy page. / 记忆银河页面。

    页面存在 galaxy_page.py 里而不是一个 .html 文件 —— Zeabur 的自动部署会沿用
    缓存的旧构建计划(旧 Dockerfile 逐个 COPY xxx.html),新增的 .html 进不了镜像;
    而 `COPY *.py .` 新旧计划都有。原因写在 galaxy_page.py 顶部。
    """
    from starlette.responses import HTMLResponse
    from galaxy_page import GALAXY_HTML
    return HTMLResponse(GALAXY_HTML)


@mcp.custom_route("/api/galaxy", methods=["GET"])
async def api_galaxy(request):
    """
    Memory data for the galaxy page: one star per bucket.
    银河页面的数据源:一条记忆一颗星。

    Query params:
      archive=0   不要会话归档(默认要:归档是 importance 4 的小星,构成日常的底色)
      min=N       只要 importance >= N 的记忆
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        include_archive = request.query_params.get("archive") not in ("0", "false", "no")
        try:
            min_imp = int(request.query_params.get("min", "0"))
        except ValueError:
            min_imp = 0

        all_buckets = await bucket_mgr.list_all(include_archive=include_archive)
        stars = []
        for b in all_buckets:
            meta = b.get("metadata", {})
            importance = meta.get("importance", 5)
            if importance < min_imp:
                continue
            content = strip_wikilinks(b.get("content", "")).strip()

            # domain 是列表,取第一个当颜色;feel 桶没有 domain,归到「情绪」
            domains = meta.get("domain") or []
            domain = domains[0] if domains else ("情绪" if meta.get("type") == "feel" else "未分类")

            # feel 桶的 name 是一串 id,没有可读标题——留空让前端拿正文开头当标题
            name = (meta.get("name") or "").strip()
            if name == b["id"]:
                name = ""

            stars.append({
                "id": b["id"],
                "name": name,
                "domain": domain,
                "importance": importance,
                "pinned": bool(meta.get("pinned") or meta.get("protected")),
                "created": meta.get("created", ""),
                "content": content,
            })

        stars.sort(key=lambda x: x["created"])
        return JSONResponse({
            "config": {
                "title": os.getenv("GALAXY_TITLE", ""),
                "subtitle": os.getenv("GALAXY_SUBTITLE", ""),
                "motto": os.getenv("GALAXY_MOTTO", ""),
                "coreEn": os.getenv("GALAXY_CORE_EN", ""),
            },
            "count": len(stars),
            "stars": stars,
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/config", methods=["GET"])
async def api_config_get(request):
    """Get current runtime config (safe fields only, API key masked)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    dehy = config.get("dehydration", {})
    emb = config.get("embedding", {})
    api_key = dehy.get("api_key", "")
    masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
    return JSONResponse({
        "dehydration": {
            "model": dehy.get("model", ""),
            "base_url": dehy.get("base_url", ""),
            "api_key_masked": masked_key,
            "max_tokens": dehy.get("max_tokens", 1024),
            "temperature": dehy.get("temperature", 0.1),
        },
        "embedding": {
            "enabled": emb.get("enabled", False),
            "model": emb.get("model", ""),
        },
        "merge_threshold": config.get("merge_threshold", 75),
        "transport": config.get("transport", "stdio"),
        "buckets_dir": config.get("buckets_dir", ""),
    })


@mcp.custom_route("/api/config", methods=["POST"])
async def api_config_update(request):
    """Hot-update runtime config. Optionally persist to config.yaml."""
    from starlette.responses import JSONResponse
    import yaml
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    updated = []

    # --- Dehydration config ---
    if "dehydration" in body:
        d = body["dehydration"]
        dehy = config.setdefault("dehydration", {})
        for key in ("model", "base_url", "max_tokens", "temperature"):
            if key in d:
                dehy[key] = d[key]
                updated.append(f"dehydration.{key}")
        if "api_key" in d and d["api_key"]:
            dehy["api_key"] = d["api_key"]
            updated.append("dehydration.api_key")
        # Hot-reload dehydrator
        dehydrator.model = dehy.get("model", "deepseek-chat")
        dehydrator.base_url = dehy.get("base_url", "")
        dehydrator.api_key = dehy.get("api_key", "")
        if hasattr(dehydrator, "client") and dehydrator.api_key:
            from openai import AsyncOpenAI
            dehydrator.client = AsyncOpenAI(
                api_key=dehydrator.api_key,
                base_url=dehydrator.base_url,
            )

    # --- Embedding config ---
    if "embedding" in body:
        e = body["embedding"]
        emb = config.setdefault("embedding", {})
        if "enabled" in e:
            emb["enabled"] = bool(e["enabled"])
            embedding_engine.enabled = emb["enabled"]
            updated.append("embedding.enabled")
        if "model" in e:
            emb["model"] = e["model"]
            embedding_engine.model = emb["model"]
            updated.append("embedding.model")

    # --- Merge threshold ---
    if "merge_threshold" in body:
        config["merge_threshold"] = int(body["merge_threshold"])
        updated.append("merge_threshold")

    # --- Persist to config.yaml if requested ---
    if body.get("persist", False):
        config_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")
        try:
            save_config = {}
            if os.path.exists(config_path):
                with open(config_path, "r", encoding="utf-8") as f:
                    save_config = yaml.safe_load(f) or {}

            if "dehydration" in body:
                sc_dehy = save_config.setdefault("dehydration", {})
                for key in ("model", "base_url", "max_tokens", "temperature"):
                    if key in body["dehydration"]:
                        sc_dehy[key] = body["dehydration"][key]
                # Never persist api_key to yaml (use env var)

            if "embedding" in body:
                sc_emb = save_config.setdefault("embedding", {})
                for key in ("enabled", "model"):
                    if key in body["embedding"]:
                        sc_emb[key] = body["embedding"][key]

            if "merge_threshold" in body:
                save_config["merge_threshold"] = int(body["merge_threshold"])

            with open(config_path, "w", encoding="utf-8") as f:
                yaml.dump(save_config, f, default_flow_style=False, allow_unicode=True)
            updated.append("persisted_to_yaml")
        except Exception as e:
            return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

    return JSONResponse({"updated": updated, "ok": True})


# =============================================================
# /api/host-vault — read/write the host-side OMBRE_HOST_VAULT_DIR
# 用于在 Dashboard 设置 docker-compose 挂载的宿主机记忆桶目录。
# 写入项目根目录的 .env 文件，需 docker compose down/up 才能生效。
# =============================================================

def _project_env_path() -> str:
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def _read_env_var(name: str) -> str:
    """Return current value of `name` from process env first, then .env file (best-effort)."""
    val = os.environ.get(name, "").strip()
    if val:
        return val
    env_path = _project_env_path()
    if not os.path.exists(env_path):
        return ""
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                if k.strip() == name:
                    return v.strip().strip('"').strip("'")
    except Exception:
        pass
    return ""


def _write_env_var(name: str, value: str) -> None:
    """
    Idempotent upsert of `NAME=value` in project .env. Creates the file if missing.
    Preserves other entries verbatim. Quotes values containing spaces.
    """
    env_path = _project_env_path()
    quoted = f'"{value}"' if value and (" " in value or "#" in value) else value
    new_line = f"{name}={quoted}\n"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

    replaced = False
    for i, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        k, _, _v = stripped.partition("=")
        if k.strip() == name:
            lines[i] = new_line
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(new_line)

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)


@mcp.custom_route("/api/host-vault", methods=["GET"])
async def api_host_vault_get(request):
    """Read the current OMBRE_HOST_VAULT_DIR (process env > project .env)."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    value = _read_env_var("OMBRE_HOST_VAULT_DIR")
    return JSONResponse({
        "value": value,
        "source": "env" if os.environ.get("OMBRE_HOST_VAULT_DIR", "").strip() else ("file" if value else ""),
        "env_file": _project_env_path(),
    })


@mcp.custom_route("/api/host-vault", methods=["POST"])
async def api_host_vault_set(request):
    """
    Persist OMBRE_HOST_VAULT_DIR to the project .env file.
    Body: {"value": "/path/to/vault"}  (empty string clears the entry)
    Note: container restart is required for docker-compose to pick up the new mount.
    """
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "invalid JSON"}, status_code=400)

    raw = body.get("value", "")
    if not isinstance(raw, str):
        return JSONResponse({"error": "value must be a string"}, status_code=400)
    value = raw.strip()

    # Reject characters that would break .env / shell parsing
    if "\n" in value or "\r" in value or '"' in value or "'" in value:
        return JSONResponse({"error": "value must not contain quotes or newlines"}, status_code=400)

    try:
        _write_env_var("OMBRE_HOST_VAULT_DIR", value)
    except Exception as e:
        return JSONResponse({"error": f"failed to write .env: {e}"}, status_code=500)

    return JSONResponse({
        "ok": True,
        "value": value,
        "env_file": _project_env_path(),
        "note": "已写入 .env；需在宿主机执行 `docker compose down && docker compose up -d` 让新挂载生效。",
    })


# =============================================================
# Import API — conversation history import
# 导入 API — 对话历史导入
# =============================================================

@mcp.custom_route("/api/import/upload", methods=["POST"])
async def api_import_upload(request):
    """Upload a conversation file and start import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err

    if import_engine.is_running:
        return JSONResponse({"error": "Import already running"}, status_code=409)

    content_type = request.headers.get("content-type", "")
    filename = ""

    try:
        if "multipart/form-data" in content_type:
            form = await request.form()
            file_field = form.get("file")
            if not file_field:
                return JSONResponse({"error": "No file field"}, status_code=400)
            raw_bytes = await file_field.read()
            filename = getattr(file_field, "filename", "upload")
            raw_content = raw_bytes.decode("utf-8", errors="replace")
        else:
            body = await request.body()
            raw_content = body.decode("utf-8", errors="replace")
            # Try to get filename from query params
            filename = request.query_params.get("filename", "upload")

        if not raw_content.strip():
            return JSONResponse({"error": "Empty file"}, status_code=400)

        preserve_raw = request.query_params.get("preserve_raw", "").lower() in ("1", "true")
        resume = request.query_params.get("resume", "").lower() in ("1", "true")

    except Exception as e:
        return JSONResponse({"error": f"Failed to read upload: {e}"}, status_code=400)

    # Start import in background
    async def _run_import():
        try:
            await import_engine.start(raw_content, filename, preserve_raw, resume)
        except Exception as e:
            logger.error(f"Import failed: {e}")

    asyncio.create_task(_run_import())

    return JSONResponse({
        "status": "started",
        "filename": filename,
        "size_bytes": len(raw_content.encode()),
    })


@mcp.custom_route("/api/import/status", methods=["GET"])
async def api_import_status(request):
    """Get current import progress."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    return JSONResponse(import_engine.get_status())


@mcp.custom_route("/api/import/pause", methods=["POST"])
async def api_import_pause(request):
    """Pause the running import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    if not import_engine.is_running:
        return JSONResponse({"error": "No import running"}, status_code=400)
    import_engine.pause()
    return JSONResponse({"status": "pause_requested"})


@mcp.custom_route("/api/import/patterns", methods=["GET"])
async def api_import_patterns(request):
    """Detect high-frequency patterns after import."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        patterns = await import_engine.detect_patterns()
        return JSONResponse({"patterns": patterns})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/results", methods=["GET"])
async def api_import_results(request):
    """List recently imported/created buckets for review."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        limit = int(request.query_params.get("limit", "50"))
        all_buckets = await bucket_mgr.list_all(include_archive=False)
        # Sort by created time, newest first
        all_buckets.sort(key=lambda b: b["metadata"].get("created", ""), reverse=True)
        results = []
        for b in all_buckets[:limit]:
            results.append({
                "id": b["id"],
                "name": b["metadata"].get("name", ""),
                "content": b["content"][:300],
                "type": b["metadata"].get("type", ""),
                "domain": b["metadata"].get("domain", []),
                "tags": b["metadata"].get("tags", []),
                "importance": b["metadata"].get("importance", 5),
                "created": b["metadata"].get("created", ""),
            })
        return JSONResponse({"buckets": results, "total": len(all_buckets)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@mcp.custom_route("/api/import/review", methods=["POST"])
async def api_import_review(request):
    """Apply review decisions: mark buckets as important/noise/pinned."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    decisions = body.get("decisions", [])
    if not decisions:
        return JSONResponse({"error": "No decisions provided"}, status_code=400)

    applied = 0
    errors = 0
    for d in decisions:
        bid = d.get("bucket_id", "")
        action = d.get("action", "")
        if not bid or not action:
            continue
        try:
            if action == "important":
                await bucket_mgr.update(bid, importance=9)
            elif action == "pin":
                await bucket_mgr.update(bid, pinned=True)
            elif action == "noise":
                await bucket_mgr.update(bid, resolved=True, importance=1)
            elif action == "delete":
                file_path = bucket_mgr._find_bucket_file(bid)
                if file_path:
                    os.remove(file_path)
            applied += 1
        except Exception as e:
            logger.warning(f"Review action failed for {bid}: {e}")
            errors += 1

    return JSONResponse({"applied": applied, "errors": errors})


# =============================================================
# /api/status — system status for Dashboard settings tab
# /api/status — Dashboard 设置页用系统状态
# =============================================================
@mcp.custom_route("/api/status", methods=["GET"])
async def api_system_status(request):
    """Return detailed system status for the settings panel."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err: return err
    try:
        stats = await bucket_mgr.get_stats()
        return JSONResponse({
            "decay_engine": "running" if decay_engine.is_running else "stopped",
            "embedding_enabled": embedding_engine.enabled,
            "buckets": {
                "permanent": stats.get("permanent_count", 0),
                "dynamic": stats.get("dynamic_count", 0),
                "archive": stats.get("archive_count", 0),
                "total": stats.get("permanent_count", 0) + stats.get("dynamic_count", 0),
            },
            "using_env_password": bool(os.environ.get("OMBRE_DASHBOARD_PASSWORD", "")),
            "version": "1.3.0",
        })
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# =============================================================
# /api/backup — daily full-store backup
# /api/backup — 每日全库备份
#
# POST /api/backup/run    Manually trigger an export → commit → push now.
# GET  /api/backup/status Scheduler state + last run result.
# =============================================================
@mcp.custom_route("/api/backup/run", methods=["POST"])
async def api_backup_run(request):
    """Manually trigger a full-store export and push to the backup repo."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    # Make sure the scheduler is alive too, so the next daily run is armed.
    try:
        await backup_engine.ensure_started()
    except Exception:
        pass
    try:
        result = await backup_engine.run_backup()
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        logger.error(f"Manual backup failed / 手动备份失败: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@mcp.custom_route("/api/backup/status", methods=["GET"])
async def api_backup_status(request):
    """Return backup scheduler state and the last run result."""
    from starlette.responses import JSONResponse
    err = _require_auth(request)
    if err:
        return err
    return JSONResponse({
        "scheduler": "running" if backup_engine.is_running else "stopped",
        "configured": backup_engine.configured,
        "repo": backup_engine.repo,
        "branch": backup_engine.branch,
        "subdir": backup_engine.backup_subdir,
        "daily_at": f"{backup_engine.run_hour:02d}:{backup_engine.run_minute:02d}",
        "last_result": backup_engine.last_result,
    })


# --- Entry point / 启动入口 ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Ombre Brain starting | transport: {transport}")

    if transport in ("sse", "streamable-http"):
        import threading
        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # --- Application-level keepalive: ping /health every 60s ---
        # --- 应用层保活：每 60 秒 ping 一次 /health，防止 Cloudflare Tunnel 空闲断连 ---
        async def _keepalive_loop():
            await asyncio.sleep(10)  # Wait for server to fully start
            async with httpx.AsyncClient() as client:
                while True:
                    try:
                        await client.get(f"http://localhost:{OMBRE_PORT}/health", timeout=5)
                        logger.debug("Keepalive ping OK / 保活 ping 成功")
                    except Exception as e:
                        logger.warning(f"Keepalive ping failed / 保活 ping 失败: {e}")
                    await asyncio.sleep(60)

        def _start_keepalive():
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_keepalive_loop())

        t = threading.Thread(target=_start_keepalive, daemon=True)
        t.start()

        # --- Add CORS middleware so remote clients (Cloudflare Tunnel / ngrok) can connect ---
        # --- 添加 CORS 中间件，让远程客户端（Cloudflare Tunnel / ngrok）能正常连接 ---
        if transport == "streamable-http":
            _app = mcp.streamable_http_app()
        else:
            _app = mcp.sse_app()
        _app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        uvicorn.run(_app, host="0.0.0.0", port=OMBRE_PORT)
    else:
        mcp.run(transport=transport)
