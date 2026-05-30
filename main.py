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
        return bonds
    except Exception as e:
        print(f"[ERROR] 获取新债数据失败: {e}")
        return []
