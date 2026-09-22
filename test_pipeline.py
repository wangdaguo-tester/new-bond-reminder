"""新债提醒的回归测试。

不依赖网络：涉及请求的地方都用假响应替换。
可以直接 `python test_pipeline.py` 跑，也兼容 pytest。
"""

import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import date, datetime

import analysis
import trigger_status
import watchdog
import main as m
from analysis import (
    _build_bond_line,
    _extract_json,
    _market_snapshot,
    _normalize_analysis,
    _parse_number,
    align_analyses,
    get_market_data,
)


# --------------------------------------------------------------------------
# 工具
# --------------------------------------------------------------------------

def _bond(bond_nm, stock_nm="测试股份", **cell_overrides):
    """构造一条集思录格式的新债行。"""
    cell = {"bond_nm": bond_nm, "stock_nm": stock_nm, "apply_date": "2026-09-10"}
    cell.update(cell_overrides)
    return {"id": cell.get("stock_id"), "cell": cell}


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def _patch_get(payload):
    """把 main.requests.get 换成返回固定 payload 的假实现，返回还原函数。"""
    original = m.requests.get
    m.requests.get = lambda *a, **k: _FakeResp(payload)
    return lambda: setattr(m.requests, "get", original)


# --------------------------------------------------------------------------
# _parse_number：接口用字符串返回数值，占位符不能参与算术
# --------------------------------------------------------------------------

def test_parse_number_rejects_placeholders():
    # 东方财富对停牌/亏损股返回这些占位符，原来会抛 TypeError
    assert _parse_number("-") is None
    assert _parse_number("--") is None
    assert _parse_number("") is None
    assert _parse_number(None) is None
    assert _parse_number(True) is None
    assert _parse_number({}) is None


def test_parse_number_coerces_and_scales():
    # 集思录的数值字段是字符串
    assert _parse_number("31.10") == 31.10
    assert _parse_number("3.700") == 3.7
    assert _parse_number(185000, scale=100) == 1850.0
    # 涨跌幅可以为负，不能被 positive_only 误伤
    assert _parse_number("-4.46") == -4.46


def test_parse_number_positive_only():
    assert _parse_number(0, positive_only=True) is None
    assert _parse_number("-1", positive_only=True) is None
    assert _parse_number("0.0", positive_only=True) is None


# --------------------------------------------------------------------------
# get_market_data：非法代码必须在发请求前就被挡掉
# --------------------------------------------------------------------------

def test_get_market_data_rejects_bad_codes():
    assert get_market_data(None) is None
    assert get_market_data("") is None
    assert get_market_data("60051") is None      # 5 位
    assert get_market_data("60051a") is None     # 非数字
    assert get_market_data(300727) is None       # 非字符串


def _patch_analysis_get(payload):
    original = analysis.requests.get
    analysis.requests.get = lambda *a, **k: _FakeResp(payload)
    return lambda: setattr(analysis.requests, "get", original)


def test_get_market_data_maps_verified_field_ids():
    """真实接口核对：f164=市盈率(TTM)、f167=市净率、f46=今开价（不是 PB）。

    润禾材料 300727 的 f167 缩放后为 4.02，与集思录给出的 pb=4.020 一致。
    """
    payload = {"data": {"f43": 3019, "f170": -446, "f164": 5301, "f167": 402, "f46": 2944}}
    restore = _patch_analysis_get(payload)
    try:
        quote = get_market_data("300727")
    finally:
        restore()

    assert quote["price"] == 30.19
    assert quote["change_pct"] == -4.46
    assert quote["pe"] == 53.01      # f164
    assert quote["pb"] == 4.02       # f167，不能取成 f46 的今开价 29.44


def test_get_market_data_tolerates_placeholder_fields():
    """停牌/亏损股返回 "-" 时不能抛 TypeError（原实现会崩掉整条流水线）。"""
    payload = {"data": {"f43": "-", "f170": "-", "f164": "-", "f167": "-", "f46": "-"}}
    restore = _patch_analysis_get(payload)
    try:
        quote = get_market_data("300727")
    finally:
        restore()

    assert quote is not None
    assert all(quote[f] is None for f in ("price", "change_pct", "pe", "pb"))


def test_get_market_data_handles_empty_data():
    restore = _patch_analysis_get({"data": None})
    try:
        assert get_market_data("300727") is None
    finally:
        restore()


# --------------------------------------------------------------------------
# _build_bond_line：曾经因为字符串字段导致溢价率恒为 0%
# --------------------------------------------------------------------------

def test_bond_line_computes_premium_from_string_fields():
    """真实集思录数据：润禾转02，price=30.19 convert_price=31.10 pma_rt=97.07。"""
    cell = {
        "bond_nm": "润禾转02",
        "stock_nm": "润禾材料",
        "convert_price": "31.10",
        "amount": "3.700",
        "rating_cd": "A+",
        "pma_rt": "97.07",
    }
    snapshot = {"price": 30.19, "change_pct": -4.46, "pb": 4.02, "pe": None}
    line = _build_bond_line(0, cell, snapshot)

    assert "溢价率: 3.0%" in line, line          # (100-97.07)/97.07*100
    assert "转股价值: 97.07元" in line, line
    assert "发行规模: 3.7亿" in line, line
    assert "评级: A+" in line, line
    assert "PE" not in line, line                # pe 为 None 时不该出现
    assert "未公布" not in line, line


def test_bond_line_falls_back_to_computing_conversion_value():
    """没给 pma_rt 时用现价/转股价自己算。"""
    cell = {"bond_nm": "X转债", "stock_nm": "X", "convert_price": "31.10"}
    line = _build_bond_line(0, cell, {"price": 30.19})
    assert "转股价值: 97.07元" in line, line
    assert "溢价率: 3.0%" in line, line


def test_bond_line_missing_quote_does_not_fake_zero_premium():
    """行情缺失时必须显示「暂无」，不能渲染成 0% 溢价。"""
    cell = {"bond_nm": "X转债", "stock_nm": "X", "convert_price": "31.10"}
    line = _build_bond_line(0, cell, {"price": None})
    assert "正股价: 暂无" in line, line
    assert "溢价率: 暂无" in line, line
    assert "溢价率: 0%" not in line, line


def test_bond_line_handles_unpublished_convert_price():
    cell = {"bond_nm": "X转债", "stock_nm": "X", "convert_price": "未公布"}
    line = _build_bond_line(0, cell, {"price": 12.05})
    assert "转股价: 未公布" in line, line
    assert "未公布元" not in line, line
    assert "溢价率: 暂无" in line, line


def test_bond_line_tolerates_empty_cell():
    line = _build_bond_line(3, {}, {})
    assert "[3] 未知转债" in line, line


# --------------------------------------------------------------------------
# _market_snapshot：集思录打底，东财补充，两条都没有时为 None
# --------------------------------------------------------------------------

def test_market_snapshot_uses_jisilu_when_no_stock_id():
    """没有 stock_id 时不该发请求，直接用集思录字段。"""
    cell = {"price": "30.19", "increase_rt": "-4.46", "pb": "4.020"}
    snapshot = _market_snapshot(cell)
    assert snapshot["price"] == 30.19
    assert snapshot["change_pct"] == -4.46
    assert snapshot["pb"] == 4.02
    assert snapshot["source"] == "jisilu"


def test_market_snapshot_all_missing():
    snapshot = _market_snapshot({})
    assert snapshot["price"] is None
    assert snapshot["pe"] is None


# --------------------------------------------------------------------------
# align_analyses：模型输出的对齐与防伪造
# --------------------------------------------------------------------------

def test_align_by_index():
    bonds = [_bond("A转债"), _bond("B转债")]
    items = [
        {"index": 1, "score": 5, "suggestion": "谨慎申购", "reason": "r"},
        {"index": 0, "score": 8, "suggestion": "强力申购", "reason": "r"},
    ]
    out = align_analyses(items, bonds)
    assert len(out) == 2
    assert out[0]["score"] == 8
    assert out[1]["score"] == 5
    # 名字一律以集思录真实数据为准，不采信模型
    assert out[0]["bond_name"] == "A转债"


def test_align_marks_uncovered_as_none():
    bonds = [_bond("A转债"), _bond("B转债")]
    out = align_analyses([{"index": 0, "score": 7, "suggestion": "强力申购", "reason": ""}], bonds)
    assert out[0] is not None
    assert out[1] is None


def test_align_drops_hallucinated_bond():
    """模型编造出来的新债必须被丢掉，不能推送给用户。"""
    bonds = [_bond("A转债")]
    items = [
        {"index": 0, "score": 7, "suggestion": "强力申购", "reason": ""},
        {"index": 9, "bond_name": "幽灵转债", "score": 10, "suggestion": "强力申购", "reason": ""},
    ]
    out = align_analyses(items, bonds)
    assert len(out) == 1
    assert out[0]["bond_name"] == "A转债"


def test_align_falls_back_to_name_match():
    bonds = [_bond("A转债"), _bond("B转债")]
    items = [{"bond_name": "B转债", "score": 6, "suggestion": "谨慎申购", "reason": ""}]
    out = align_analyses(items, bonds)
    assert out[0] is None
    assert out[1]["score"] == 6


def test_align_tolerates_whitespace_in_name():
    bonds = [_bond("B转债")]
    items = [{"bond_name": " B 转债 ", "score": 6, "suggestion": "谨慎申购", "reason": ""}]
    assert align_analyses(items, bonds)[0]["score"] == 6


def test_align_ignores_bool_index():
    """bool 是 int 的子类，True 不能被当成下标 1。"""
    bonds = [_bond("A转债"), _bond("B转债")]
    items = [{"index": True, "bond_name": "B转债", "score": 6, "suggestion": "谨慎申购", "reason": ""}]
    out = align_analyses(items, bonds)
    assert out[0] is None
    assert out[1]["score"] == 6


def test_align_skips_garbage_items():
    bonds = [_bond("A转债")]
    items = ["不是字典", None, {"index": 0, "score": 7, "suggestion": "强力申购", "reason": ""}]
    assert align_analyses(items, bonds)[0]["score"] == 7


# --------------------------------------------------------------------------
# _normalize_analysis：字段校验
# --------------------------------------------------------------------------

def test_normalize_clamps_score():
    assert _normalize_analysis({"score": 99, "suggestion": "强力申购", "reason": ""}, "A")["score"] == 10
    assert _normalize_analysis({"score": 0, "suggestion": "强力申购", "reason": ""}, "A")["score"] == 1
    assert _normalize_analysis({"score": "abc", "suggestion": "强力申购", "reason": ""}, "A")["score"] is None
    assert _normalize_analysis({"suggestion": "强力申购"}, "A")["score"] is None


def test_normalize_does_not_invent_suggestion():
    """模型给出枚举外的建议时原样保留，不替它编一个。"""
    out = _normalize_analysis({"score": 5, "suggestion": "随便买", "reason": "x"}, "A")
    assert out["suggestion"] == "随便买"
    out = _normalize_analysis({"score": 5}, "A")
    assert out["suggestion"] == "未知建议"


# --------------------------------------------------------------------------
# _extract_json：模型输出的容错
# --------------------------------------------------------------------------

def test_extract_json_variants():
    assert json.loads(_extract_json('```json\n{"a": 1}\n```')) == {"a": 1}
    assert json.loads(_extract_json('```json {"a": 1}```')) == {"a": 1}   # 单行围栏
    assert json.loads(_extract_json('```\n{"a": 1}\n```')) == {"a": 1}    # 无语言标记
    assert json.loads(_extract_json('{"a": 1}')) == {"a": 1}
    assert json.loads(_extract_json('好的：\n{"a": 1}\n以上。')) == {"a": 1}


# --------------------------------------------------------------------------
# fetch_new_bonds：区分「接口异常」与「今天真没有新债」
# --------------------------------------------------------------------------

def test_fetch_rejects_missing_rows_key():
    restore = _patch_get({"page": 1})
    try:
        bonds, err = m.fetch_new_bonds()
        assert bonds == []
        assert err is not None and "rows" in err
    finally:
        restore()


def test_fetch_rejects_non_list_rows():
    restore = _patch_get({"rows": None})
    try:
        bonds, err = m.fetch_new_bonds()
        assert err is not None
    finally:
        restore()


def test_fetch_rejects_non_dict_payload():
    restore = _patch_get(["not", "a", "dict"])
    try:
        bonds, err = m.fetch_new_bonds()
        assert err is not None
    finally:
        restore()


def test_fetch_empty_rows_is_error_to_avoid_silent_missed_alerts():
    """预告列表为空时无法排除反爬，必须失败以避免静默漏报。"""
    restore = _patch_get({"rows": []})
    try:
        bonds, err = m.fetch_new_bonds()
        assert bonds == []
        assert err is not None
    finally:
        restore()


def test_fetch_filters_to_today():
    today = date.today().strftime("%Y-%m-%d")
    restore = _patch_get({"rows": [
        {"cell": {"bond_nm": "今日债", "apply_date": today}},
        {"cell": {"bond_nm": "未来债", "apply_date": "2099-01-01"}},
        {"cell": {"bond_nm": "日期缺失"}},
    ]})
    try:
        bonds, err = m.fetch_new_bonds()
        assert err is None
        assert [b["cell"]["bond_nm"] for b in bonds] == ["今日债"]
    finally:
        restore()


# --------------------------------------------------------------------------
# load_config：类型写错要回退，而不是崩溃
# --------------------------------------------------------------------------

def test_load_config_falls_back_on_bad_analysis_type():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("analysis: 42\n")   # 类型写错，原来会让 ** 展开抛 TypeError
        cfg = m.load_config(path)
    assert cfg["analysis"] == {"enabled": False, "model": "deepseek-chat"}


def test_load_config_ignores_removed_sections():
    """portfolio / risk 已废弃，留着旧配置也不该影响加载。"""
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("portfolio:\n  - name: 旧配置\nrisk:\n  stop_loss: -0.05\n"
                    "analysis:\n  enabled: true\n")
        cfg = m.load_config(path)
    assert cfg["analysis"]["enabled"] is True
    assert cfg["analysis"]["model"] == "deepseek-chat"


def test_load_config_missing_file():
    path = os.path.join(tempfile.gettempdir(), "definitely-not-here-9f2a.yaml")
    cfg = m.load_config(path)
    assert cfg["analysis"]["enabled"] is False


def test_load_config_merges_user_overrides():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("analysis:\n  model: deepseek-reasoner\n")
        cfg = m.load_config(path)
    # 用户覆盖生效，未覆盖的键保留默认值
    assert cfg["analysis"]["model"] == "deepseek-reasoner"
    assert cfg["analysis"]["enabled"] is False


def test_load_config_notifications():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('analysis:\n  enabled: true\nnotifications:\n  sendkeys:\n    - "K1"\n')
        cfg = m.load_config(path)
    assert cfg["notifications"]["sendkeys"] == ["K1"]


def test_load_config_bad_notifications_type():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('notifications: "写错了"\n')
        cfg = m.load_config(path)
    assert cfg["notifications"] == {"sendkeys": []}


def test_load_config_bad_sendkeys_type_falls_back_to_empty_list():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "config.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write('notifications:\n  sendkeys: "not-a-list"\n')
        cfg = m.load_config(path)
    assert cfg["notifications"]["sendkeys"] == []


# --------------------------------------------------------------------------
# send_notification：多接收人
# --------------------------------------------------------------------------

def _patch_post(status_record):
    """替换 requests.post，记录每次请求的 URL。"""
    original = m.requests.post

    def fake_post(url, **kwargs):
        status_record.append(url)
        return _FakeResp({"code": 0})

    m.requests.post = fake_post
    return lambda: setattr(m.requests, "post", original)


def _with_env_sendkey(value):
    """临时把 SENDKEY 设为 value(None 表示删除),返回还原函数。"""
    original = os.environ.get("SENDKEY")
    if value is None:
        os.environ.pop("SENDKEY", None)
    else:
        os.environ["SENDKEY"] = value

    def restore():
        if original is None:
            os.environ.pop("SENDKEY", None)
        else:
            os.environ["SENDKEY"] = original

    return restore


def _without_env_sendkey():
    """临时清掉 SENDKEY 环境变量,返回还原函数。"""
    return _with_env_sendkey(None)


def test_send_notification_pushes_to_every_key():
    posted = []
    restore_post = _patch_post(posted)
    restore_env = _without_env_sendkey()
    try:
        ok = m.send_notification([_bond("A转债", "甲公司")], None, ["KEY1", "KEY2"])
    finally:
        restore_post()
        restore_env()

    assert ok is True
    assert len(posted) == 2
    assert "KEY1" in posted[0] and "KEY2" in posted[1]


def test_send_notification_merges_env_key_without_mutating_input():
    """SENDKEY 环境变量自动并入，但不能污染调用方传入的列表。"""
    posted = []
    restore_post = _patch_post(posted)
    original_env = os.environ.get("SENDKEY")
    os.environ["SENDKEY"] = "ENVKEY"
    try:
        sendkeys = ["CFGKEY"]
        ok = m.send_notification([_bond("A转债", "甲公司")], None, sendkeys)
    finally:
        restore_post()
        if original_env is None:
            os.environ.pop("SENDKEY", None)
        else:
            os.environ["SENDKEY"] = original_env

    assert ok is True
    assert len(posted) == 2
    assert sendkeys == ["CFGKEY"], "不应把环境变量写回调用方的列表"


def test_send_notification_no_keys_returns_false():
    restore_env = _without_env_sendkey()
    try:
        assert m.send_notification([_bond("A转债")], None, []) is False
        assert m.send_notification([_bond("A转债")], None, None) is False
        # 配置里混入非法值时应被过滤，而不是在拼接 URL 时崩溃
        assert m.send_notification([_bond("A转债")], None, [None, "", 42]) is False
    finally:
        restore_env()


def test_send_notification_fails_when_any_recipient_fails():
    original_post = m.requests.post
    restore_env = _without_env_sendkey()

    def fake_post(url, **kwargs):
        if "BADKEY" in url:
            raise m.requests.RequestException("simulated failure")
        return _FakeResp({"code": 0})

    m.requests.post = fake_post
    try:
        ok = m.send_notification([_bond("A")], None, ["GOODKEY", "BADKEY"])
    finally:
        m.requests.post = original_post
        restore_env()

    assert ok is False


# --------------------------------------------------------------------------
# 推送原语:_resolve_sendkeys / send_alert
# --------------------------------------------------------------------------

def test_resolve_sendkeys_filters_bad_values_and_merges_env():
    restore_env = _with_env_sendkey("ENVKEY")
    try:
        keys = m._resolve_sendkeys(["KEY1", "  ", None, 42, "KEY2"])
    finally:
        restore_env()

    assert keys == ["KEY1", "KEY2", "ENVKEY"]


def test_resolve_sendkeys_deduplicates_env_key():
    restore_env = _with_env_sendkey("KEY1")
    try:
        keys = m._resolve_sendkeys(["KEY1"])
    finally:
        restore_env()

    assert keys == ["KEY1"]


def test_resolve_sendkeys_does_not_mutate_input():
    restore_env = _with_env_sendkey("ENVKEY")
    try:
        sendkeys = ["KEY1"]
        m._resolve_sendkeys(sendkeys)
    finally:
        restore_env()

    assert sendkeys == ["KEY1"], "不应把环境变量写回调用方的列表"


def test_send_alert_pushes_to_every_key():
    posted = []
    restore_post = _patch_post(posted)
    restore_env = _with_env_sendkey(None)
    try:
        ok = m.send_alert("标题", "正文", ["KEY1", "KEY2"])
    finally:
        restore_post()
        restore_env()

    assert ok is True
    assert len(posted) == 2
    assert posted[0].endswith("/KEY1.send") and posted[1].endswith("/KEY2.send")


def test_send_alert_without_any_key_returns_false():
    restore_env = _with_env_sendkey(None)
    try:
        assert m.send_alert("标题", "正文", None) is False
        assert m.send_alert("标题", "正文", [None, "", 42]) is False
    finally:
        restore_env()


# --------------------------------------------------------------------------
# build_message：任何一只债都不能从推送里消失
# --------------------------------------------------------------------------

def test_build_message_never_drops_a_bond():
    bonds = [_bond("A转债", "甲公司"), _bond("B转债", "乙公司")]
    analyses = [
        {"bond_name": "A转债", "score": 8, "suggestion": "强力申购", "reason": "基本面好"},
        None,   # 模型漏答
    ]
    title, desp = m.build_message(bonds, analyses)
    assert "AI 分析" in title
    assert "A转债" in desp
    assert "B转债" in desp            # 关键：未覆盖的债仍然出现
    assert "AI 未覆盖" in desp
    assert "基本面好" in desp


def test_build_message_fallback_when_no_analysis():
    bonds = [_bond("A转债", "甲公司")]
    title, desp = m.build_message(bonds, None)
    assert title == "🏦 今日有新债可申购！"
    assert "A转债" in desp
    assert "AI 分析暂时不可用" in desp


def test_build_message_all_none_analyses_falls_back():
    bonds = [_bond("A转债", "甲公司")]
    title, desp = m.build_message(bonds, [None])
    assert title == "🏦 今日有新债可申购！"
    assert "AI 分析暂时不可用" in desp


# --------------------------------------------------------------------------
# trigger_status:判断今天主链是否已成功
# --------------------------------------------------------------------------

def _bjt(*args):
    return datetime(*args, tzinfo=trigger_status.BJT)


def _run(created_at, conclusion="success", status="completed", html_url=None):
    return {"created_at": created_at, "conclusion": conclusion, "status": status,
            "html_url": html_url}


def _patch_runs(payload):
    """把 trigger_status.requests.get 换成返回固定 payload 的假实现。"""
    original = trigger_status.requests.get
    trigger_status.requests.get = lambda *a, **k: _FakeResp(payload)
    return lambda: setattr(trigger_status.requests, "get", original)


def _patch_runs_error():
    """让 trigger_status.requests.get 抛请求异常。"""
    original = trigger_status.requests.get

    def fake_get(*args, **kwargs):
        raise trigger_status.requests.RequestException("simulated failure")

    trigger_status.requests.get = fake_get
    return lambda: setattr(trigger_status.requests, "get", original)


def test_check_primary_ok_when_today_run_succeeded():
    restore = _patch_runs({"workflow_runs": [_run("2026-09-22T00:57:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_check_primary_failed_when_today_run_failed():
    """今天跑了但失败,是 FAILED 而不是 MISSING —— 原先两者混为一谈,
    告警因此把"跑挂了"误诊成"没触发",让人去查 PAT 而不是看运行日志。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T00:57:00Z", conclusion="failure")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_FAILED


def test_check_primary_ok_while_today_run_is_still_running():
    """主链正在跑的时候,兜底不能抢跑,否则会重复推送。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T00:57:00Z", conclusion=None, status="in_progress")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_check_primary_missing_when_only_yesterdays_run():
    """UTC 15:59 = 北京 23:59,算昨天。"""
    restore = _patch_runs({"workflow_runs": [_run("2026-09-21T15:59:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_MISSING


def test_check_primary_counts_utc16_as_today_in_beijing():
    """UTC 16:00 = 北京次日 00:00,算今天。"""
    restore = _patch_runs({"workflow_runs": [_run("2026-09-21T16:00:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_check_primary_unknown_on_request_error():
    restore = _patch_runs_error()
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_UNKNOWN


def test_check_primary_unknown_without_repo():
    original = os.environ.pop("GITHUB_REPOSITORY", None)
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo=None)
    finally:
        if original is not None:
            os.environ["GITHUB_REPOSITORY"] = original

    assert status == trigger_status.PRIMARY_UNKNOWN


def test_check_primary_unknown_on_unexpected_payload():
    restore = _patch_runs({"message": "Not Found"})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_UNKNOWN


def test_check_primary_failed_when_all_today_runs_failed():
    """今天有记录但全失败 → FAILED(与"压根没触发"分开)。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T02:10:00Z", conclusion="cancelled"),
        _run("2026-09-22T00:57:00Z", conclusion="failure")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_FAILED


def test_check_primary_ok_when_one_of_today_runs_succeeded():
    """一次失败一次成功(比如失败后重跑过)→ OK,不能因为有过失败就告警。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T02:10:00Z", conclusion="failure"),
        _run("2026-09-22T00:57:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_evaluate_primary_returns_newest_today_run_url():
    """API 按时间倒序返回,取今天最近一次运行的链接:失败时直接点进日志。"""
    newest = "https://github.com/owner/repo/actions/runs/2"
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T02:10:00Z", conclusion="failure", html_url=newest),
        _run("2026-09-22T00:57:00Z", conclusion="failure",
             html_url="https://github.com/owner/repo/actions/runs/1")]})
    try:
        verdict = trigger_status.evaluate_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                        repo="owner/repo", token="")
    finally:
        restore()

    assert verdict.status == trigger_status.PRIMARY_FAILED
    assert verdict.run_url == newest


def test_evaluate_primary_run_url_is_none_without_today_run():
    """昨天那次运行的链接不能拿来当"今天失败的运行"。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-21T15:59:00Z", conclusion="failure",
             html_url="https://github.com/owner/repo/actions/runs/old")]})
    try:
        verdict = trigger_status.evaluate_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                        repo="owner/repo", token="")
    finally:
        restore()

    assert verdict.status == trigger_status.PRIMARY_MISSING
    assert verdict.run_url is None


def test_evaluate_primary_run_url_is_none_when_field_missing():
    """运行记录里没有 html_url 时返回 None,而不是半个链接。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T00:57:00Z", conclusion="failure")]})
    try:
        verdict = trigger_status.evaluate_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                        repo="owner/repo", token="")
    finally:
        restore()

    assert verdict.status == trigger_status.PRIMARY_FAILED
    assert verdict.run_url is None


def test_trigger_status_exit_code_is_zero_only_when_ok():
    """workflow 门禁靠退出码判断:只有 OK 才算"主链没问题"。"""
    original = trigger_status.check_primary_today
    try:
        for status, expected in ((trigger_status.PRIMARY_OK, 0),
                                 (trigger_status.PRIMARY_MISSING, 1),
                                 (trigger_status.PRIMARY_UNKNOWN, 1)):
            trigger_status.check_primary_today = lambda *a, **k: status
            assert trigger_status.main() == expected
    finally:
        trigger_status.check_primary_today = original


def test_trigger_status_exit_code_nonzero_when_failed():
    """FAILED 也必须非 0:门禁放行兜底推送(宁可重复,不可漏报),
    区别只在告警文案,不在"推不推"。"""
    original = trigger_status.check_primary_today
    try:
        trigger_status.check_primary_today = lambda *a, **k: trigger_status.PRIMARY_FAILED
        assert trigger_status.main() == 1
    finally:
        trigger_status.check_primary_today = original


# --------------------------------------------------------------------------
# watchdog:主链没成功时告警
# --------------------------------------------------------------------------

def _patch_watchdog(status, alert_result=True, run_url=None):
    """替换看门狗的"查状态""读配置""发告警",返回 (calls, restore)。

    看门狗现在调的是 evaluate_primary_today(它要多带一个运行链接),
    所以这里替身也必须返回 PrimaryVerdict 而不是裸字符串。
    """
    calls = []
    original_evaluate = watchdog.trigger_status.evaluate_primary_today
    original_alert = watchdog.main.send_alert
    original_config = watchdog.main.load_config

    def fake_alert(title, desp, sendkeys=None):
        calls.append((title, desp, sendkeys))
        return alert_result

    watchdog.trigger_status.evaluate_primary_today = lambda *a, **k: \
        trigger_status.PrimaryVerdict(status, run_url)
    watchdog.main.send_alert = fake_alert
    watchdog.main.load_config = lambda *a, **k: {
        "notifications": {"sendkeys": ["KEY1"]}}

    def restore():
        watchdog.trigger_status.evaluate_primary_today = original_evaluate
        watchdog.main.send_alert = original_alert
        watchdog.main.load_config = original_config

    return calls, restore


def test_watchdog_stays_silent_when_primary_ok():
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_OK)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 0
    assert calls == []


def test_watchdog_alerts_when_primary_missing():
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_MISSING)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 0
    assert len(calls) == 1
    title, desp, sendkeys = calls[0]
    assert "触发链" in title
    assert "GITHUB_TOKEN" in desp
    assert sendkeys == ["KEY1"]


def test_watchdog_alert_differs_when_status_unknown():
    """查不到状态不能说成"主链挂了",否则是撒谎。"""
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_UNKNOWN)
    try:
        watchdog.run()
    finally:
        restore()

    assert len(calls) == 1
    assert "无法确认" in calls[0][0]


def test_watchdog_alert_distinguishes_failed_run_from_missing():
    """链跑了但失败,不能说成"没触发":那是误诊,会把人引向 PAT 而不是日志。"""
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_FAILED)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 0
    assert len(calls) == 1
    title, desp, _ = calls[0]
    assert "失败" in title
    assert "失败" in desp
    assert "没触发" in desp
    assert "无法确认" not in desp


def test_watchdog_failed_alert_includes_run_url():
    """失败告警必须能一键点进那次运行看日志。"""
    url = "https://github.com/owner/repo/actions/runs/123"
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_FAILED, run_url=url)
    try:
        watchdog.run()
    finally:
        restore()

    assert len(calls) == 1
    assert url in calls[0][1]


def test_watchdog_failed_alert_without_run_url_stays_clean():
    """拿不到链接时不能留一句"运行记录:None"半截话。"""
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_FAILED)
    try:
        watchdog.run()
    finally:
        restore()

    assert len(calls) == 1
    assert "运行记录" not in calls[0][1]


def test_watchdog_fails_when_alert_could_not_be_sent():
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_MISSING,
                                     alert_result=False)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 1
    assert len(calls) == 1


# --------------------------------------------------------------------------
# 告警链的依赖边界:告警不能跟着 AI 依赖一起死
# --------------------------------------------------------------------------

def test_alert_entry_point_carries_no_ai_dependency():
    """告警入口(watchdog)只依赖 requests + pyyaml:openai 装不上也必须能发出告警。

    探测的是 watchdog 而不是 main —— 否则将来谁在 watchdog.py 里加一行
    import analysis / import openai,这个门禁照样放行。
    """
    proc = subprocess.run([sys.executable, "-c",
                           "import sys, watchdog; "
                           "sys.exit(1 if ({'analysis', 'openai'} & set(sys.modules)) else 0)"],
                          cwd=os.path.dirname(os.path.abspath(__file__)),
                          capture_output=True, timeout=60)
    assert proc.returncode == 0


# --------------------------------------------------------------------------

if __name__ == "__main__":
    # Windows 控制台默认 GBK,打印含 emoji 的推送标题会抛 UnicodeEncodeError,
    # 造成 3 个推送测试在本地假失败(CI 的 Ubuntu 是 UTF-8,本来就没问题)。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    passed = failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"[PASS] {name}")
            passed += 1
        except Exception:
            print(f"[FAIL] {name}")
            traceback.print_exc()
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)
