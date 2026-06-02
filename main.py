import copy
import os
import sys
import requests
import yaml
from datetime import date

from analysis import analyze


_DEFAULT_CONFIG = {
    "portfolio": [],
    "risk": {"max_position_ratio": 0.3, "stop_loss": -0.05},
    "analysis": {"enabled": False, "model": "deepseek-chat"},
}


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
        config.setdefault("portfolio", _DEFAULT_CONFIG["portfolio"])
        config.setdefault("risk", _DEFAULT_CONFIG["risk"])
        config.setdefault("analysis", _DEFAULT_CONFIG["analysis"])
        # Deep-merge nested defaults
        config["risk"] = {**_DEFAULT_CONFIG["risk"], **config.get("risk", {})}
        config["analysis"] = {**_DEFAULT_CONFIG["analysis"], **config.get("analysis", {})}
        return config
    except (yaml.YAMLError, ValueError) as e:
        print(f"[WARN] config.yaml 解析失败: {e}，使用默认空配置")
        return copy.deepcopy(_DEFAULT_CONFIG)


def fetch_new_bonds():
    """从集思录获取当日可申购的新债列表。返回空列表表示今天没有新债。"""
    url = "https://www.jisilu.cn/data/cbnew/pre_list/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        today = date.today().strftime("%Y-%m-%d")
        all_rows = data.get("rows", [])
        today_bonds = [
            row for row in all_rows
            if row.get("cell", {}).get("apply_date") == today
        ]
        return today_bonds, None
    except requests.exceptions.RequestException as e:
        return [], str(e)


def get_bond_names(bonds):
    """从新债列表中提取名称，用于推送消息。"""
    if isinstance(bonds, tuple):
        bonds, _ = bonds
    names = []
    for bond in bonds:
        name = bond.get("cell", {}).get("bond_nm", "未知新债")
        names.append(name)
    return names


def send_notification(bond_names, analyses=None, bonds=None):
    """通过 Server酱 推送到微信。analyses 为 AI 分析结果列表。"""
    sendkey = os.getenv("SENDKEY")
    if not sendkey:
        print("[ERROR] 未设置 SENDKEY 环境变量，无法推送")
        return False

    today_str = date.today().strftime("%Y-%m-%d")

    bond_info = {}
    if bonds:
        for b in bonds:
            cell = b.get("cell", {})
            nm = cell.get("bond_nm", "")
            if nm:
                bond_info[nm] = cell

    if analyses:
        lines = ["🏦 今日有新债可申购！\n"]
        for a in analyses:
            score = a.get("score", "?")
            suggestion = a.get("suggestion", "未知")
            reason = a.get("reason", "")
            bond_name = a.get("bond_name", "未知转债")
            cell = bond_info.get(bond_name, {})
            stock_nm = cell.get("stock_nm", "未知")
            convert_price = cell.get("convert_price", "未公布")
            lines.append(f"📊 {bond_name}")
            lines.append(f"   正股：{stock_nm} | 转股价：{convert_price}")
            lines.append(f"   🤖 AI评分：{score}/10 — {suggestion}")
            lines.append(f"   💡 {reason}")
            lines.append("")
        lines.append(f"---\n📅 申购日期：{today_str}")
        title = "🏦 今日有新债可申购！（AI 分析）"
        desp = "\n".join(lines)
    else:
        title = "🏦 今日有新债可申购！"
        lines = ["🏦 今日有新债可申购！\n"]
        for name in bond_names:
            cell = bond_info.get(name, {})
            stock_nm = cell.get("stock_nm", "未知")
            lines.append(f"📊 {name} | 正股：{stock_nm}")
        if bond_names:
            lines.append(f"\n⚠️ AI 分析暂时不可用，请自行判断")
        lines.append(f"\n📅 申购日期：{today_str}")
        desp = "\n".join(lines)

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {"title": title, "desp": desp}
    try:
        resp = requests.post(url, data=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0:
            print(f"[INFO] 推送成功: {payload['title']}")
            return True
        else:
            print(f"[ERROR] 推送失败: {result}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] 推送异常: {e}")
        return False


def main():
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
        if analyses:
            print(f"[INFO] AI 分析完成: {len(analyses)} 只新债")
        else:
            print("[WARN] AI 分析失败，降级为基础推送")
    ok = send_notification(names, analyses, bonds)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
