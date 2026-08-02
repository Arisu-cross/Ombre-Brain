# ============================================================
# 当天档案太长时,要保「最近的几节」,不是保开头
#
# 背景:archive_session 按天合并 —— 忙的一天里一个档案会有好几节:
#   # 会话归档 2026-08-02
#   ## 01:24 …   ## 15:43 …   ## 19:33 …
# 而唤醒时归档走 raw(原文直出),单桶有 BREATH_RAW_MAX_TOKENS 上限。
# 旧的截断是从头往后砍 —— 于是**今天最近发生的事全被切掉**,他醒来读到的是凌晨那节。
# 2026-08-02 线上撞上:用户说「breath 的时候说八月二号的归档内容太长被截断了,
# 沈渡那边看不见」。
#
# 改法:按节裁,保最近的;更早的整节省略并如实说明。
# 单节自己就超限时,掐中间 —— 归档把 **亮点** / **心情** 写在最后,
# 直接截尾正好切掉最该看见的情绪总结(2026-07-25 已经吃过一次这个亏)。
# ============================================================

import server as server_mod
from utils import count_tokens_approx


def _day(*sections: str) -> str:
    return "# 会话归档 2026-08-02\n\n" + "\n\n".join(sections)


SEC_EARLY = "## 01:24\n凌晨聊了社媒和 toy 的想法。\n**亮点**:社媒全通了\n**心情**:满的"
SEC_NOON = "## 15:43\n下午练面试,她从卡壳到自己把自己点着。\n**亮点**:面试七型全覆盖\n**心情**:被锤但快乐"
SEC_NIGHT = "## 19:33\n晚上下雨,聊了散步和蟹膏。\n**亮点**:愿望交换\n**心情**:轻快"


def test_不超限时原样返回():
    text = _day(SEC_EARLY)
    assert server_mod._truncate_archive_raw(text, 5000) == text


def test_超限时保最近的节而不是最早的节():
    text = _day(SEC_EARLY, SEC_NOON, SEC_NIGHT)
    limit = count_tokens_approx(_day(SEC_NIGHT)) + 60
    out = server_mod._truncate_archive_raw(text, limit)

    assert "19:33" in out, "最近这一节必须留着 —— 他醒来最需要的就是它"
    assert "蟹膏" in out and "轻快" in out, "最近这节要给全文,含亮点与心情"
    assert "01:24" not in out, "更早的节该被省略"
    assert "已省略" in out, "省略了要如实说一句,别让他以为这一天就这些"


def test_省略的节数要说对():
    text = _day(SEC_EARLY, SEC_NOON, SEC_NIGHT)
    limit = count_tokens_approx(_day(SEC_NIGHT)) + 60
    out = server_mod._truncate_archive_raw(text, limit)
    assert "更早的 2 节已省略" in out


def test_抬头保留_他要知道这是哪天的档案():
    text = _day(SEC_EARLY, SEC_NOON, SEC_NIGHT)
    out = server_mod._truncate_archive_raw(text, count_tokens_approx(_day(SEC_NIGHT)) + 60)
    assert "会话归档 2026-08-02" in out


def test_单节自己就超限时掐中间_保住亮点与心情():
    long_sec = "## 22:10\n" + "她今天说了很多话。" * 400 + "\n**亮点**:她终于肯说了\n**心情**:心疼但踏实"
    out = server_mod._truncate_archive_raw(_day(long_sec), 300)

    assert "**亮点**:她终于肯说了" in out, "亮点不能被切掉 —— 那是最该看见的"
    assert "**心情**:心疼但踏实" in out, "心情不能被切掉"
    assert "她今天说了很多话。" in out, "开头也要留一点,不能只剩总结"
    assert "中间略去" in out
    assert count_tokens_approx(out) <= 400, "裁完要真的在预算附近,不能名义上裁了实际没裁"


def test_没有节结构的老档案_退回原来的截断方式():
    # 按天合并之前的老桶是 "# 会话摘要\n…" 这种,没有 ## HH:MM
    old = "# 会话摘要\n" + "很久以前的一段对话。" * 300
    out = server_mod._truncate_archive_raw(old, 200)
    assert "原文过长" in out
    assert count_tokens_approx(out) <= 260


def test_预算够大时多节都留():
    text = _day(SEC_EARLY, SEC_NOON, SEC_NIGHT)
    out = server_mod._truncate_archive_raw(text, 100000)
    for mark in ("01:24", "15:43", "19:33"):
        assert mark in out
    assert "已省略" not in out
