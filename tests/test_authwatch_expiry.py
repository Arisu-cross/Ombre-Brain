# ============================================================
# 长期令牌到期提醒(2026-09-12 新增)
#
# 背景:沈渡改成直连之后,失效方式变了 ——
#   以前是「续签停了」,有 mtime 这个运行时信号可看;
#   现在是「一年到期」,**到期前一秒都完全正常**,没有任何运行时信号。
#   唯一能提前发出的信号是日期,所以这套逻辑必须靠谱。
#
# 同时钉死另一件事:直连之后 CPA 那组检查**必须能单独关掉**。
# 不关的话,没有流量经过 CPA → 它的凭据永远不再刷新 →
# 「mtime 停止更新」永远成立 → 每 2 小时一条假警报。
# 假警报比没警报更糟:几次之后人就不看了,真出事那条一起被忽略。
# ============================================================

import importlib
import os
from datetime import datetime, timedelta, timezone

import authwatch_engine as aw


def _engine(**env):
    """按给定环境变量造一个引擎。没给的一律清掉,避免被外部环境污染。"""
    keys = ["AUTHWATCH_MGMT_KEY", "AUTHWATCH_TG_TOKEN", "AUTHWATCH_TG_CHAT",
            "AUTHWATCH_TOKEN_EXPIRES", "AUTHWATCH_TOKEN_WARN_DAYS"]
    old = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        for k, v in env.items():
            os.environ[k] = v
        importlib.reload(aw)
        return aw.AuthWatchEngine()
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        importlib.reload(aw)


def _in_days(n: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=n)).date().isoformat()


# ---------- 开关 ----------

def test_两组都没配就不启动():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c")
    assert e.configured is False


def test_只配令牌到期也能启动_不需要CPA():
    # 这条是直连之后的常态:CPA 那组留空
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_TOKEN_EXPIRES="2027-09-12")
    assert e.configured is True
    assert e.token_enabled is True
    assert e.cpa_enabled is False, "没配 MGMT_KEY 时 CPA 那组必须整组不跑"


def test_没有TG就不启动():
    e = _engine(AUTHWATCH_TOKEN_EXPIRES="2027-09-12")
    assert e.configured is False


# ---------- 到期判定 ----------

def test_还早的时候一声不吭():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_TOKEN_EXPIRES=_in_days(200))
    problems, lines = e.check_token_expiry()
    assert problems == []
    assert lines and "还有" in lines[0]


def test_跨进30天就开始提醒():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_TOKEN_EXPIRES=_in_days(29))
    problems, _ = e.check_token_expiry()
    assert len(problems) == 1
    assert "setup-token" in problems[0], "提醒里必须写清楚怎么换,不然等于没提醒"


def test_越近越急_档位会往下走():
    near = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                   AUTHWATCH_TOKEN_EXPIRES=_in_days(2))
    far = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                  AUTHWATCH_TOKEN_EXPIRES=_in_days(29))
    # 文本必须不同 —— 否则去重逻辑会把后面几档全压掉,只报一次 30 天那条
    assert near.check_token_expiry()[0] != far.check_token_expiry()[0]


def test_已经过期_要说他现在多半说不出话_并给退路():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_TOKEN_EXPIRES=_in_days(-1))
    problems, _ = e.check_token_expiry()
    assert "已经过期" in problems[0]
    assert "CLAUDE_CODE_OAUTH_TOKEN" in problems[0], "过期时必须给出应急退路"


def test_日期写错不许静默_那等于保护没在工作():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_TOKEN_EXPIRES="明年九月")
    problems, _ = e.check_token_expiry()
    assert problems and "没在工作" in problems[0]


def test_带时刻的写法也认():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_TOKEN_EXPIRES="2027-09-12T03:00:00Z")
    assert e._parse_expiry(e.token_expires) is not None


def test_提前天数可调_写错退回默认():
    ok = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                 AUTHWATCH_TOKEN_EXPIRES="2027-09-12",
                 AUTHWATCH_TOKEN_WARN_DAYS="60,14")
    assert ok.token_warn_days == (60, 14)
    bad = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                  AUTHWATCH_TOKEN_EXPIRES="2027-09-12",
                  AUTHWATCH_TOKEN_WARN_DAYS="三十天")
    assert bad.token_warn_days == (30, 7, 3, 1), "写错要退回默认,不能让提醒整个哑掉"


# ---------- 没开这项时彻底不干活 ----------

def test_没配到期日时这项完全不参与():
    e = _engine(AUTHWATCH_TG_TOKEN="t", AUTHWATCH_TG_CHAT="c",
                AUTHWATCH_MGMT_KEY="k")
    assert e.token_enabled is False
    assert e.check_token_expiry() == ([], [])
