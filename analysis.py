"""新债 AI 分析模块 — 行情获取 + DeepSeek 调用"""

import json
import os
import requests
from openai import OpenAI


def get_market_data(stock_code):
    """通过东方财富 push2 接口获取正股实时行情。

    Args:
        stock_code: 6位股票代码，如 "600519"

    Returns:
        dict: {"price": 1850.00, "change_pct": 2.50, "pe": 35.2, "pb": 8.1}
        None: 请求失败时
    """
    # Early validation: must be a 6-character numeric string
    if not isinstance(stock_code, str) or len(stock_code) != 6 or not stock_code.isdigit():
        return None

    # 判断交易所：6开头=上海(1)，其他=深圳(0)
    market = "1" if stock_code.startswith("6") else "0"
    secid = f"{market}.{stock_code}"

    url = "https://push2.eastmoney.com/api/qt/stock/get"
    params = {
        "secid": secid,
        "fields": "f43,f170,f167,f46",  # 最新价, 涨跌幅, PE(TTM), PB
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json().get("data")
        if not data:
            return None

        return {
            "price": data.get("f43", 0) / 100 if data.get("f43") else None,      # 分→元
            "change_pct": data.get("f170", 0) / 100 if data.get("f170") else None,  # 基点→百分比
            "pe": data.get("f167", 0) / 100 if data.get("f167") else None,      # PE TTM
            "pb": data.get("f46", 0) / 100 if data.get("f46") else None,        # PB
        }
    except (requests.RequestException, ValueError):
        return None
