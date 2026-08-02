# ============================================================
# 唤醒浮现的 token 总预算可调(BREATH_WAKE_BUDGET)
#
# 背景:归档按天合并之后,一个归档桶 = 一整天。忙的一天顶掉大半预算,
# 更早的天就被整条挤掉;而单桶还另有 BREATH_RAW_MAX_TOKENS 上限会把当天裁短。
# 用户要求「上限调大,展示所有归档内容」,所以两个口子都要能开:
#   BREATH_RAW_MAX_TOKENS=0  → 单桶不裁
#   BREATH_WAKE_BUDGET=<大值> → 总量放开
#
# ⚠️ 这是每开一个新窗口的一次性成本,调大要心里有数 —— 所以做成 env 而不是写死。
# ============================================================

import importlib
import os

import server as server_mod
from utils import count_tokens_approx


def _reload(**env):
    old = {k: os.environ.get(k) for k in env}
    os.environ.update({k: str(v) for k, v in env.items()})
    try:
        return importlib.reload(server_mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_默认预算仍是一万_不改变现状():
    mod = _reload()
    assert mod.BREATH_WAKE_BUDGET == 10000
    importlib.reload(server_mod)


def test_预算可由环境变量调大():
    mod = _reload(BREATH_WAKE_BUDGET=30000)
    assert mod.BREATH_WAKE_BUDGET == 30000
    importlib.reload(server_mod)


def test_单桶上限设0等于不裁_全给他():
    mod = _reload(BREATH_RAW_MAX_TOKENS=0)
    assert mod.BREATH_RAW_MAX_TOKENS == 0
    long_day = "# 会话归档 2026-08-02\n\n" + "\n\n".join(
        f"## {h:02d}:00\n" + "今天发生了很多事。" * 200 for h in (1, 15, 19, 23)
    )
    out = mod._truncate_archive_raw(long_day, mod.BREATH_RAW_MAX_TOKENS)
    assert out == long_day, "设 0 就该原样全给,一个字都不裁"
    assert "已省略" not in out and "截断" not in out
    for h in ("01:00", "15:00", "19:00", "23:00"):
        assert h in out, "一天里每一节都要在"
    importlib.reload(server_mod)


def test_显式传的max_tokens上限会跟着预算放开():
    # 否则调大了 BREATH_WAKE_BUDGET,走「显式传 max_tokens」那条路仍被 20000 顶回去
    mod = _reload(BREATH_WAKE_BUDGET=30000)
    assert max(20000, mod.BREATH_WAKE_BUDGET) == 30000
    importlib.reload(server_mod)
