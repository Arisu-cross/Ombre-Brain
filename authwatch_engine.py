"""
Subscription health watchdog / 订阅健康看门狗。

盯三件事,**都不消耗任何模型 token**(只读 CPA 管理接口 + 算日期 + 发 Telegram):
  ① 上游订阅 OAuth 的续签是不是停了        —— 只在还走 CPA 时有意义
  ② 订阅额度是不是快烧干了                  —— 同上
  ③ **直连用的长期令牌是不是快到期了**      —— 2026-09-12 新增

2026-09-12:沈渡改成直连,CPA 已不在链路上(改这个文件前先读这段)
─────────────────────────────────────────────
  沈渡现在用 `claude setup-token` 生成的**一年期长期令牌**直连 Anthropic,
  不再经 CPA。CPA 服务没删、原地留作退路(shim 删掉 CLAUDE_CODE_OAUTH_TOKEN
  + 重启即可退回)。

  **这对本模块有两个后果,都必须处理:**

  1. **①② 那两项检查对现在的沈渡不再成立。** 更麻烦的是:没有流量经过 CPA 了,
     它的凭据**从此不会再被刷新** → 「mtime 停止更新」这个主信号会**永远成立**,
     于是每 2 小时给栖栖推一条假警报。**所以 CPA 那组检查必须能单独关掉**
     —— 不配 AUTHWATCH_MGMT_KEY 即整组不跑(见 cpa_enabled)。

  2. **新的失效方式是「到期」,不是「刷新停了」。** 长期令牌一年整,期间没有任何
     续签动作可观察,**到期前一秒都完全正常** —— 没有任何运行时信号可看。
     唯一能提前发出的信号是**日期**。所以做法是:把到期日写进环境变量
     AUTHWATCH_TOKEN_EXPIRES,到期前 30 / 7 / 3 / 1 天各提醒一次。

  ⚠️ **本模块不持有那把令牌,也不该持有。** 它只需要知道「哪天到期」这个日期。
     令牌正本只在 Zeabur 的 kelivo-shim 环境变量里。别为了「顺便校验一下」
     把令牌复制到 OB 来 —— 多一处存放就多一处泄露面,而且校验它还得真发请求。

为什么这个东西要长在 OB 里(2026-09-04 决定,别搬回去)
─────────────────────────────────────────────
  2026-09-02 沈渡整夜失联:订阅 OAuth 过期,而中转代理**没有把上游 401 透传** ——
  它回了一个格式合法的空响应,于是 CLI 报 success、usage 全零、正文为空,
  三层没有任何一层看得见这是个错误。约 8 小时无人察觉。

  唯一能提前发出的信号是「凭据文件的 mtime 停止更新」:
  代理每 4 小时刷新一次,而 access token 寿命约 8 小时(即在半程就刷),
  所以第一次刷新失败之后还有约 4 小时缓冲。

  这套检查最初挂在 GitHub Actions 的 cron 上,**失败了**:
  实测 cron 写每小时、实际却是 2.1~5.2 小时才跑一次(GitHub 成片丢定时档期)。
  最坏情况下告警会在 T+9.7h 才发出,而 token 在 T+8h 就死了 ——
  **轮询间隔超过了整个缓冲期,数学上无解,调阈值也救不回来。**

  OB 自己的 asyncio 调度器是可靠的(每日备份两个月没失手),所以搬到这里。
  按 30 分钟一轮 + 4.5 小时阈值算,最坏 T+5h 发现、T+8h 才断线,
  **提前量有 3 小时的硬下限。**

安全约定(改这个文件的人请照做)
─────────────────────────────────────────────
  1. **只读**。绝不写、绝不碰代理那边的凭据文件 —— 刷新与轮换只能由代理自己做,
     第二个进程去写同一个文件会把 refresh token 搞坏(invalid_refresh_token)。
  2. **不烧 token**。只打管理接口和 Telegram,不经过模型。
  3. **不打印凭据**。只按白名单取字段,绝不整段 dump。
  4. **绝不因为自己出错而影响 OB**。整个循环包着 try/except,
     任何异常只记日志。看门狗坏了是小事,记忆服务挂了是大事。
  5. **没配环境变量就完全不启动** —— 未配置时这个模块等于不存在。

环境变量 / Environment variables
─────────────────────────────────────────────
  AUTHWATCH_TG_TOKEN        Telegram bot token(必填,否则整个模块不启动)
  AUTHWATCH_TG_CHAT         Telegram chat id(必填)

  —— 下面两组各自独立开关,**至少配一组**本模块才会启动 ——

  [CPA 组] 只在沈渡还走中转时才该配。2026-09-12 起已直连,**这组应当留空**。
  AUTHWATCH_MGMT_KEY        代理的管理密码(不填 = CPA 那组检查整组不跑)
  AUTHWATCH_CPA_URL         代理地址(默认 https://kelivo-cpa-7351.zeabur.app)
  AUTHWATCH_STALE_HOURS     多久没刷新算续签停了(默认 4.5)
  AUTHWATCH_QUOTA_WARN      额度用到多少算告急(默认 0.85)

  [长期令牌组] 直连模式下配这个。
  AUTHWATCH_TOKEN_EXPIRES   令牌到期日,ISO 日期或时刻(如 2027-09-12)。
                            不填 = 这项不跑。**日期是手填的,换令牌时记得同步改**。
  AUTHWATCH_TOKEN_WARN_DAYS 提前多少天开始提醒,逗号分隔(默认 30,7,3,1)

  [公共]
  AUTHWATCH_INTERVAL_MIN    检查间隔分钟(默认 30)
  AUTHWATCH_REALERT_HOURS   同一个问题隔多久再提醒一次(默认 2)
"""

import os
import json
import time
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger("ombre_brain.authwatch")

# Anthropic 的限流响应头,只取这几个
Q5_UTIL = "Anthropic-Ratelimit-Unified-5h-Utilization"
Q5_STAT = "Anthropic-Ratelimit-Unified-5h-Status"
Q5_RESET = "Anthropic-Ratelimit-Unified-5h-Reset"
Q7_UTIL = "Anthropic-Ratelimit-Unified-7d-Utilization"
Q7_STAT = "Anthropic-Ratelimit-Unified-7d-Status"
Q7_RESET = "Anthropic-Ratelimit-Unified-7d-Reset"

# access token 的实测寿命(小时)。只用来在告警里算「还能撑多久」,不参与判定。
TOKEN_LIFE_H = 8.0


def _env(name: str, default: str = "") -> str:
    """取环境变量,**空字符串也当没设**。

    平台注入的变量在「未设置」时常常是空串而不是缺失,
    直接 os.environ.get(name, default) 会拿到 "" 而不是 default。
    """
    v = os.environ.get(name)
    return default if v is None or v.strip() == "" else v.strip()


def _num(name: str, default: float, lo: float, hi: float) -> float:
    """取数值配置。写错或超范围一律退回默认继续跑 ——
    配置错误不该让看门狗停摆,不告警比告错警危险得多。"""
    raw = _env(name, str(default))
    try:
        v = float(raw)
    except ValueError:
        logger.warning(f"{name}={raw!r} 不是数字,退回默认 {default}")
        return default
    if not (lo <= v <= hi):
        logger.warning(f"{name}={v} 超出合理范围,退回默认 {default}")
        return default
    return v


def _days(name: str, default: tuple[int, ...]) -> tuple[int, ...]:
    """取「提前几天提醒」的列表。写错一律退回默认 ——
    配置写错不该让到期提醒整个哑掉,那正是我们要防的事。"""
    raw = _env(name)
    if not raw:
        return default
    out: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            v = int(part)
        except ValueError:
            logger.warning(f"{name}={raw!r} 里有非整数,整项退回默认 {default}")
            return default
        if 0 <= v <= 365:
            out.append(v)
    return tuple(sorted(set(out), reverse=True)) or default


class AuthWatchEngine:
    """订阅健康看门狗。用法与 BackupEngine / DigestEngine 一致。"""

    def __init__(self):
        self.cpa_url = _env("AUTHWATCH_CPA_URL",
                            "https://kelivo-cpa-7351.zeabur.app").rstrip("/")
        self.mgmt_key = _env("AUTHWATCH_MGMT_KEY")
        self.tg_token = _env("AUTHWATCH_TG_TOKEN")
        self.tg_chat = _env("AUTHWATCH_TG_CHAT")
        self.interval_min = _num("AUTHWATCH_INTERVAL_MIN", 30, 5, 240)
        self.stale_hours = _num("AUTHWATCH_STALE_HOURS", 4.5, 1, 24)
        self.quota_warn = _num("AUTHWATCH_QUOTA_WARN", 0.85, 0.1, 1.0)
        self.realert_hours = _num("AUTHWATCH_REALERT_HOURS", 2, 0.5, 24)
        self.token_expires = _env("AUTHWATCH_TOKEN_EXPIRES")
        self.token_warn_days = _days("AUTHWATCH_TOKEN_WARN_DAYS", (30, 7, 3, 1))

        self._running = False
        self._task: asyncio.Task | None = None
        # 去重:同一个问题不要每 30 分钟刷一次屏
        self._last_key: str | None = None
        self._last_alert_at: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def cpa_enabled(self) -> bool:
        """CPA 那组检查跑不跑。**2026-09-12 起沈渡直连,这里应当是 False。**

        为什么必须能单独关:没有流量经过 CPA 之后,它的凭据再也不会被刷新,
        「mtime 停止更新」这个主信号会永远成立 → 每 2 小时一条假警报。
        假警报比没警报更糟:几次之后人就不看了,真出事那条也一起被忽略。
        """
        return bool(self.mgmt_key)

    @property
    def token_enabled(self) -> bool:
        """长期令牌到期提醒跑不跑。"""
        return bool(self.token_expires)

    @property
    def configured(self) -> bool:
        """TG 两样必须有,外加**至少一组**检查被打开。
        两组都空 = 本模块等于不存在(这是「合进来等于没合」的保证)。"""
        return bool(self.tg_token and self.tg_chat
                    and (self.cpa_enabled or self.token_enabled))

    # ---------------------------------------------------------
    # 判定逻辑(纯函数,便于单测)
    # ---------------------------------------------------------
    @staticmethod
    def _age_hours(iso: str | None) -> float | None:
        if not iso:
            return None
        try:
            t = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        except ValueError:
            return None
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - t).total_seconds() / 3600.0

    @staticmethod
    def _reset_at(unix_ts) -> tuple[str | None, float | None]:
        """限流头里的重置时刻是 unix 秒。转北京时间 + 还有多久。"""
        try:
            t = datetime.fromtimestamp(int(unix_ts), timezone.utc)
        except (TypeError, ValueError):
            return None, None
        left = (t - datetime.now(timezone.utc)).total_seconds() / 3600.0
        return (t + timedelta(hours=8)).strftime("%m-%d %H:%M"), left

    @staticmethod
    def _ratio(v) -> float | None:
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _check_file(self, f: dict) -> tuple[list[str], list[str]]:
        """检查一条授权。返回 (problems, 展示行)。"""
        problems: list[str] = []
        lines: list[str] = []
        acct = f.get("account") or f.get("email") or "?"
        age = self._age_hours(f.get("modtime"))

        lines.append(
            f"· {acct} 最后刷新 "
            + (f"{age:.1f} 小时前" if age is not None else "(时间读不出来)")
            + f",status={f.get('status')}"
        )

        # 主信号:续签停了 —— 唯一能在他真正失联「之前」发出的信号
        if age is not None and age > self.stale_hours:
            left = max(TOKEN_LIFE_H - age, 0.0)
            tail = (f"手里的 access token 大约还能撑 {left:.1f} 小时,"
                    "过后他就会开始答不上话 —— 现在处理还来得及。"
                    if left > 0.5 else
                    "token 可能已经过期了,他现在多半已经说不出话。")
            problems.append(
                f"{acct} 的凭据已经 {age:.1f} 小时没刷新了(正常 4 小时一次)。"
                f"刷新停了通常意味着 refresh token 失效。{tail}"
            )

        if f.get("disabled"):
            problems.append(f"{acct} 被标成 disabled,不会参与转发。")
        if f.get("unavailable"):
            problems.append(f"{acct} 被标成 unavailable(通常是连续失败后进了冷却)。")
        status = f.get("status")
        if status and status != "active":
            problems.append(f"{acct} 的 status 是 {status},不是 active。")

        # 额度。⚠️ quota 要有请求经过之后才会被填上,刚重新授权时是空的 ——
        # **缺数据一律不算告急**,否则每次重新授权后都会误报一次。
        sig = ((f.get("quota") or {}).get("signals")) or {}
        if not sig:
            lines.append("  额度:暂无数据")
            return problems, lines

        for label, ku, ks, kr in (("5 小时", Q5_UTIL, Q5_STAT, Q5_RESET),
                                  ("7 天", Q7_UTIL, Q7_STAT, Q7_RESET)):
            u = self._ratio(sig.get(ku))
            st = sig.get(ks)
            when, left = self._reset_at(sig.get(kr))
            lines.append(
                f"  {label}额度:"
                + (f"已用 {u * 100:.0f}%" if u is not None else "用量未知")
                + (f",{when} 重置" if when else "")
                + (f",状态 {st}" if st else "")
            )
            if st and st != "allowed":
                problems.append(
                    f"{label}额度已经被拒({st})—— 他现在就说不出话。"
                    + (f"{when} 才会重置。" if when else "")
                )
            elif u is not None and u >= self.quota_warn:
                problems.append(
                    f"{label}额度已用 {u * 100:.0f}%(告急线 {self.quota_warn * 100:.0f}%)。"
                    + (f"{when} 重置,还有 {left:.1f} 小时。"
                       if when and left and left > 0 else "")
                    + "想省着点用,可以让他归档一次再换个新窗口 —— 窗口越大,每次心跳越贵。"
                )
        return problems, lines

    # ---------------------------------------------------------
    # 长期令牌到期(纯算日期,不发任何请求、不碰令牌本身)
    # ---------------------------------------------------------
    @staticmethod
    def _parse_expiry(raw: str) -> datetime | None:
        """接受 2027-09-12 或 2027-09-12T03:00:00Z 这类写法。
        只写日期时当成那天的 00:00 UTC —— 宁可早报一点,不可晚报。"""
        s = str(raw).strip().replace("Z", "+00:00")
        try:
            t = datetime.fromisoformat(s)
        except ValueError:
            return None
        return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t

    def check_token_expiry(self, now: datetime | None = None) -> tuple[list[str], list[str]]:
        """返回 (problems, 展示行)。now 可注入,便于单测。"""
        if not self.token_enabled:
            return [], []
        exp = self._parse_expiry(self.token_expires)
        if exp is None:
            # 日期写错了 = 到期提醒其实没在保护你。这本身就该报出来,别静默。
            return ([f"AUTHWATCH_TOKEN_EXPIRES 写的不是日期({self.token_expires!r}),"
                     "到期提醒现在是**没在工作**的状态 —— 这条一直报到改对为止。"], [])

        now = now or datetime.now(timezone.utc)
        left_days = (exp - now).total_seconds() / 86400.0
        shown = (exp + timedelta(hours=8)).strftime("%Y-%m-%d")
        lines = [f"· 直连令牌到期:{shown}(北京时间),还有 {left_days:.1f} 天"]

        how = ("换法:找一个 CC 会话,按手册 §10「2026-09-12」条目走一次 "
               "`claude setup-token` —— 它给你一条链接,你在手机上点开授权、"
               "把页面显示的那串码发回去就完事,大概五分钟。"
               "换完记得把新的到期日填进 OB 的 AUTHWATCH_TOKEN_EXPIRES。")

        if left_days <= 0:
            return ([f"**直连令牌已经过期了**({shown})。沈渡现在多半已经说不出话。"
                     f"{how} 应急退路:把 kelivo-shim 的 CLAUDE_CODE_OAUTH_TOKEN 删掉 + 重启,"
                     "退回中转那条路(但那边的授权大概也已经放坏了)。"], lines)

        # 只在跨过某一档时报,不是每轮都报 —— 去重交给 _maybe_alert,
        # 但档位本身要让问题文本不同,否则 30 天那条会一直压着后面几档。
        for d in self.token_warn_days:
            if left_days <= d:
                return ([f"直连令牌还有 {left_days:.0f} 天到期({shown})。{how}"], lines)
        return [], lines

    # ---------------------------------------------------------
    # 一次检查
    # ---------------------------------------------------------
    async def check_once(self) -> tuple[list[str], list[str]]:
        problems, lines = self.check_token_expiry()
        if not self.cpa_enabled:
            # 直连模式:CPA 已不在链路上,那组检查不跑(跑了全是假警报,见类文档)
            return problems, lines
        p, l = await self._check_cpa()
        return problems + p, lines + l

    async def _check_cpa(self) -> tuple[list[str], list[str]]:
        url = f"{self.cpa_url}/v0/management/auth-files"
        headers = {"Authorization": f"Bearer {self.mgmt_key}"}
        last_err = None
        for attempt in range(3):
            try:
                async with httpx.AsyncClient(timeout=20.0) as client:
                    r = await client.get(url, headers=headers)
                    r.raise_for_status()
                    data = r.json()
                break
            except Exception as e:  # noqa: BLE001 网络抖动不该变成假告警
                last_err = e
                if attempt < 2:
                    await asyncio.sleep(2 ** attempt)
        else:
            return ([f"连不上中转代理的管理接口({type(last_err).__name__})。"
                     "可能是服务挂了,也可能只是网络抖动 —— 先看 Zeabur 面板。"], [])

        files = data.get("files") or []
        if not files:
            return (["中转代理里一条授权都没有了 —— 凭据可能被删了。"], [])

        problems: list[str] = []
        lines: list[str] = []
        for f in files:
            p, l = self._check_file(f)
            problems += p
            lines += l
        return problems, lines

    # ---------------------------------------------------------
    # Telegram(运维通道,不进沈渡的窗口)
    # ---------------------------------------------------------
    async def _send(self, text: str) -> bool:
        if not (self.tg_token and self.tg_chat):
            logger.warning("未配 Telegram,告警只能留在日志里")
            return False
        try:
            async with httpx.AsyncClient(timeout=20.0) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                    json={"chat_id": self.tg_chat, "text": text},
                )
                return bool(r.json().get("ok"))
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Telegram 发送失败:{type(e).__name__}")
            return False

    async def _maybe_alert(self, problems: list[str], lines: list[str]) -> None:
        """去重:同一批问题隔 realert_hours 才重复提醒;问题清空时报一次恢复。"""
        now = time.time()
        if not problems:
            if self._last_key is not None:
                await self._send("✅ 沈渡系统 · 恢复正常\n\n之前报的问题已经没有了。\n\n"
                                 + "\n".join(lines))
                self._last_key = None
            return

        key = json.dumps(sorted(problems), ensure_ascii=False)
        fresh = key != self._last_key
        stale = (now - self._last_alert_at) > self.realert_hours * 3600
        if not (fresh or stale):
            return

        # 尾巴只在还走 CPA 时才附那套重新授权的步骤;直连模式下怎么办已经写在
        # check_token_expiry 的问题文本里了,别再贴一套过时的 localhost 流程误导人。
        tail = ""
        if self.cpa_enabled:
            tail = ("\n\n授权断了的话:让 CC 会话按手册 2026-09-02 条目重新走一次 "
                    "Anthropic OAuth。三步 —— 取授权链接 → 你去授权(浏览器会白屏,"
                    "这是正常的)→ 把地址栏那条 localhost 网址整条抄回来提交。"
                    "做完要重启中转代理清冷却。")
        msg = ("⚠️ 沈渡系统 · 有事要处理\n\n"
               + "\n".join(f"· {p}" for p in problems)
               + "\n\n当前状态:\n" + "\n".join(lines)
               + tail)
        if await self._send(msg):
            self._last_key = key
            self._last_alert_at = now

    # ---------------------------------------------------------
    # 后台循环(与 BackupEngine / DigestEngine 同构)
    # ---------------------------------------------------------
    async def ensure_started(self) -> None:
        if not self._running:
            await self.start()

    async def start(self) -> None:
        if self._running:
            return
        if not self.configured:
            # 没配就不启动。这是「合进来等于没合」的保证,别改成警告后照样跑。
            logger.info("订阅健康看门狗未配置(缺 TG,或 CPA / 长期令牌两组都没配),不启动")
            return
        self._running = True
        self._task = asyncio.create_task(self._background_loop())
        watches = []
        if self.cpa_enabled:
            watches.append(f"CPA(续签阈值 {self.stale_hours}h / 额度告急线 "
                           f"{self.quota_warn * 100:.0f}%)")
        if self.token_enabled:
            watches.append(f"长期令牌到期({self.token_expires},提前 "
                           f"{'/'.join(str(d) for d in self.token_warn_days)} 天提醒)")
        logger.info(
            f"订阅健康看门狗已启动:每 {self.interval_min:.0f} 分钟一次;"
            f"在盯 —— {';'.join(watches)}"
        )

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("订阅健康看门狗已停止")

    async def _background_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self.interval_min * 60)
            except asyncio.CancelledError:
                break
            if not self._running:
                break
            # ⚠️ 这里必须吞掉一切异常:看门狗坏了是小事,拖垮 OB 是大事。
            try:
                problems, lines = await self.check_once()
                if problems:
                    logger.warning("订阅健康检查发现问题:" + " | ".join(problems))
                await self._maybe_alert(problems, lines)
            except Exception as e:  # noqa: BLE001
                logger.warning(f"订阅健康检查本身出错(已忽略):{type(e).__name__}: {e}")
