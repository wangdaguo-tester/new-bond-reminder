import copy
import os
import sys
import requests
import yaml
from datetime import date


_DEFAULT_CONFIG = {
    "analysis": {"enabled": False, "model": "deepseek-chat"},
    "notifications": {"sendkeys": []},
}


def _cell_of(row):
    """安全取出集思录行里的 cell 字典，任何异常结构都返回空字典。"""
    if not isinstance(row, dict):
        return {}
    cell = row.get("cell")
    return cell if isinstance(cell, dict) else {}


def _merge_section(config, section):
    """把用户配置的某个段落与默认值合并。

    类型写错时回退默认值并告警，而不是让 ** 展开抛 TypeError 崩掉整个脚本。
    """
    user_value = config.get(section)
    if isinstance(user_value, dict):
        config[section] = {**_DEFAULT_CONFIG[section], **user_value}
    else:
        if user_value is not None:
            print(f"[WARN] config.yaml 中 {section} 应为字典，"
                  f"实际为 {type(user_value).__name__}，已回退默认值")
        config[section] = dict(_DEFAULT_CONFIG[section])


def load_config(config_path="config.yaml"):
    """加载用户配置文件，文件不存在或格式错误时返回默认空配置。"""
    if not os.path.exists(config_path):
        print("[WARN] config.yaml 不存在，使用默认空配置（关闭 AI 分析）")
        return copy.deepcopy(_DEFAULT_CONFIG)
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            raise ValueError("config.yaml 内容非字典格式")

        for section in ("analysis", "notifications"):
            _merge_section(config, section)

        sendkeys = config["notifications"].get("sendkeys")
        if not isinstance(sendkeys, list):
            print("[WARN] config.yaml 中 notifications.sendkeys 应为列表，已回退为空列表")
            config["notifications"]["sendkeys"] = []

        return config
    except (yaml.YAMLError, ValueError, OSError) as e:
        print(f"[WARN] config.yaml 解析失败: {e}，使用默认空配置")
        return copy.deepcopy(_DEFAULT_CONFIG)


def fetch_new_bonds():
    """从集思录获取当日可申购的新债列表。

    Returns:
        (bonds, error): error 为 None 表示请求与解析都成功（此时 bonds 为空
        代表今天真的没有新债）；error 非 None 表示数据不可信，调用方应报错退出，
        避免把「接口被拦/改版」误当成「今天没新债」而静默漏报。
    """
    url = "https://www.jisilu.cn/data/cbnew/pre_list/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        return [], f"请求失败: {e}"
    except ValueError as e:
        return [], f"响应不是合法 JSON（可能被反爬拦截或接口变更）: {e}"

    # 结构校验：把「接口异常」和「今天没有新债」区分开
    if not isinstance(data, dict) or "rows" not in data:
        return [], f"响应结构异常，缺少 rows 字段（可能被反爬拦截）: {str(data)[:200]}"
    rows = data.get("rows")
    if not isinstance(rows, list):
        return [], f"rows 字段类型异常: {type(rows).__name__}"

    today = date.today().strftime("%Y-%m-%d")
    today_bonds = [
        row for row in rows
        if _cell_of(row).get("apply_date") == today
    ]

    # 把抓取结果的形态写进日志，便于事后判断是「真没有」还是「抓瞎了」
    if not rows:
        return [], "集思录返回 0 条记录，无法确认是否被反爬拦截，停止以避免静默漏报"
    elif not today_bonds:
        apply_dates = sorted({
            cell.get("apply_date")
            for cell in map(_cell_of, rows)
            if cell.get("apply_date")
        })
        span = f"{apply_dates[0]} ~ {apply_dates[-1]}" if apply_dates else "无"
        print(f"[INFO] 集思录返回 {len(rows)} 条记录，申购日期范围 {span}，"
              f"今日（{today}）无匹配")

    return today_bonds, None


def get_bond_names(bonds):
    """从新债列表中提取名称，用于日志输出。"""
    return [_cell_of(b).get("bond_nm") or "未知新债" for b in bonds]


def build_message(bonds, analyses=None):
    """拼装推送文案。

    analyses 与 bonds 逐项对应（元素为 dict 或 None）。只要有一条分析成功就启用
    AI 版文案，其余债券单独降级显示，保证「今天有哪些债」这一事实永远完整，
    不会因为模型漏答而让某只债从推送里消失。
    """
    today_str = date.today().strftime("%Y-%m-%d")
    has_analysis = bool(analyses) and any(analyses)

    lines = ["🏦 今日有新债可申购！\n"]
    for index, bond in enumerate(bonds):
        cell = _cell_of(bond)
        bond_name = cell.get("bond_nm") or "未知新债"
        stock_nm = cell.get("stock_nm") or "未知"

        analysis = analyses[index] if has_analysis and index < len(analyses) else None
        if analysis:
            score = analysis.get("score")
            suggestion = analysis.get("suggestion") or "未知"
            reason = analysis.get("reason") or ""
            convert_price = cell.get("convert_price") or "未公布"
            lines.append(f"📊 {bond_name}")
            lines.append(f"   正股：{stock_nm} | 转股价：{convert_price}")
            lines.append(f"   🤖 AI评分：{score if score is not None else '?'}/10 — {suggestion}")
            if reason:
                lines.append(f"   💡 {reason}")
            lines.append("")
        else:
            note = "（AI 未覆盖，请自行判断）" if has_analysis else ""
            lines.append(f"📊 {bond_name} | 正股：{stock_nm}{note}")

    if not has_analysis:
        lines.append("\n⚠️ AI 分析暂时不可用，请自行判断")

    lines.append(f"\n📅 申购日期：{today_str}")
    title = "🏦 今日有新债可申购！（AI 分析）" if has_analysis else "🏦 今日有新债可申购！"
    return title, "\n".join(lines)


def _resolve_sendkeys(sendkeys=None):
    """汇总可用的 SendKey:传入列表(过滤坏值) + SENDKEY 环境变量(去重)。

    返回新列表,不修改调用方传入的列表。
    """
    keys = [k for k in (sendkeys or []) if isinstance(k, str) and k.strip()]
    env_key = os.getenv("SENDKEY")
    if env_key and env_key not in keys:
        keys.append(env_key)
    return keys


def _push_serverchan(keys, title, desp):
    """向每个 SendKey 推送同一条消息,全部成功才返回 True。"""
    if not keys:
        print("[ERROR] 未配置任何 SendKey(config.yaml 的 notifications.sendkeys "
              "或 SENDKEY 环境变量),无法推送")
        return False

    payload = {"title": title, "desp": desp}
    success_count = 0
    for sendkey in keys:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        try:
            resp = requests.post(url, data=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                print(f"[INFO] 推送成功 (SendKey: {sendkey[:12]}...): {title}")
                success_count += 1
            else:
                print(f"[ERROR] 推送失败 (SendKey: {sendkey[:12]}...): {result}")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] 推送异常 (SendKey: {sendkey[:12]}...): {e}")

    if success_count == len(keys):
        print(f"[INFO] 推送完成: {success_count}/{len(keys)} 成功")
        return True
    print(f"[ERROR] 推送未全部成功: {success_count}/{len(keys)} 成功")
    return False


def send_notification(bonds, analyses=None, sendkeys=None):
    """通过 Server酱 推送到微信,支持多个接收人。

    Args:
        bonds: 今日新债列表
        analyses: 与 bonds 等长的分析结果列表(元素为 dict 或 None)
        sendkeys: SendKey 列表;SENDKEY 环境变量会自动并入(去重)

    Returns:
        bool: 全部接收人推送成功才为 True
    """
    title, desp = build_message(bonds, analyses)
    return _push_serverchan(_resolve_sendkeys(sendkeys), title, desp)


def send_alert(title, desp, sendkeys=None):
    """推送一条自定义告警文案(不依赖新债数据)。

    Args:
        title: 告警标题
        desp: 告警正文
        sendkeys: SendKey 列表;SENDKEY 环境变量会自动并入(去重)

    Returns:
        bool: 全部接收人推送成功才为 True
    """
    return _push_serverchan(_resolve_sendkeys(sendkeys), title, desp)


def main():
    # 延迟导入:analysis 会拉起 openai,而告警链(watchdog.py)只 import 本模块。
    # 若把它放在模块顶层,openai 一旦装不上,主链、兜底和告警会一起哑掉。
    from analysis import analyze

    print("[INFO] 开始检查今日新债...")
    config = load_config()
    bonds, error = fetch_new_bonds()
    if error:
        print(f"[ERROR] 获取新债数据失败: {error}，不推送")
        return 1
    if not bonds:
        print("[INFO] 今日无新债可申购，不推送")
        return 0
    names = get_bond_names(bonds)
    print(f"[INFO] 发现新债: {names}")

    analyses = None
    if config.get("analysis", {}).get("enabled", False):
        print("[INFO] 正在调用 AI 分析...")
        analyses = analyze(bonds, config)
        if analyses and any(analyses):
            covered = sum(1 for a in analyses if a)
            print(f"[INFO] AI 分析完成: 覆盖 {covered}/{len(bonds)} 只新债")
        else:
            print("[WARN] AI 分析失败，降级为基础推送")
            analyses = None

    notifications = config.get("notifications")
    sendkeys = notifications.get("sendkeys") if isinstance(notifications, dict) else None
    ok = send_notification(bonds, analyses, sendkeys)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
