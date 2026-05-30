import os
import requests


def fetch_new_bonds():
    """从集思录获取当日可申购的新债列表。返回空列表表示今天没有新债。"""
    url = "https://www.jisilu.cn/data/calendar/bond/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # 集思录返回的数据结构为 {"data": {"bond_list": [...]}}
        bonds = data.get("data", {}).get("bond_list", [])
        return bonds, None
    except requests.exceptions.RequestException as e:
        return [], str(e)


def get_bond_names(bonds):
    """从新债列表中提取名称，用于推送消息。"""
    # 兼容处理：如果传入的是 fetch_new_bonds() 的返回值 (list, error)，自动解包
    if isinstance(bonds, tuple):
        bonds, _ = bonds
    names = []
    for bond in bonds:
        # 集思录返回的字段可能是 bond_name 或 name
        name = bond.get("bond_name") if bond.get("bond_name") is not None else bond.get("name", "未知新债")
        names.append(name)
    return names


def send_notification(bond_names):
    """通过 Server酱 推送到微信。"""
    sendkey = os.getenv("SENDKEY")
    if not sendkey:
        print("[ERROR] 未设置 SENDKEY 环境变量，无法推送")
        return False

    url = f"https://sctapi.ftqq.com/{sendkey}.send"
    payload = {
        "title": "今日有新债可申购！",
        "desp": "、".join(bond_names),
    }
    try:
        resp = requests.post(url, data=payload, timeout=30)
        resp.raise_for_status()
        result = resp.json()
        if result.get("code") == 0:
            print(f"[INFO] 推送成功: {payload['desp']}")
            return True
        else:
            print(f"[ERROR] 推送失败: {result}")
            return False
    except requests.exceptions.RequestException as e:
        print(f"[ERROR] 推送异常: {e}")
        return False


def main():
    print("[INFO] 开始检查今日新债...")
    bonds, error = fetch_new_bonds()
    if error:
        print(f"[ERROR] 获取新债数据失败: {error}，不推送")
        return 1
    if not bonds:
        print("[INFO] 今日无新债可申购，不推送")
        return 0
    names = get_bond_names(bonds)
    print(f"[INFO] 发现新债: {names}")
    ok = send_notification(names)
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(main())
