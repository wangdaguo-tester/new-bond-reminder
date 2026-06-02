"""新债 AI 分析模块 — 行情获取 + DeepSeek 调用"""

import json
import os
import re
import requests
import openai
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


def _build_portfolio_summary(portfolio):
    """将持仓列表转为 AI 可读的文本摘要。"""
    lines = []
    for item in portfolio:
        type_label = {
            "stock_etf": "股票ETF", "bond_fund": "债券基金",
            "mixed_fund": "混合基金", "stock": "个股", "cash": "现金"
        }.get(item.get("type", ""), "未知")
        lines.append(
            f"- {item.get('name', '未知')}（{type_label}，代码 {item.get('code', 'N/A')}"
            f"，占比 {item.get('weight', 0) * 100:.0f}%）"
        )
    return "\n".join(lines)


def _build_bond_line(bond_info, market_data):
    """为单个新债构建一行描述文本。"""
    cell = bond_info.get("cell", {})
    bond_nm = cell.get("bond_nm", "未知转债")
    stock_nm = cell.get("stock_nm", "未知")
    convert_price = cell.get("convert_price", "未公布")
    scale = cell.get("issue_size", "未公布")

    line = f"- {bond_nm} | 正股: {stock_nm} | 转股价: {convert_price} | 规模: {scale}亿"

    if market_data:
        price = market_data.get("price", 0)
        premium = 0
        if isinstance(price, (int, float)) and price > 0 and isinstance(convert_price, (int, float)) and convert_price > 0:
            # 转股价值 = (正股价 / 转股价) * 100
            convert_value = (price / convert_price) * 100
            premium = round((100 - convert_value) / convert_value * 100, 1)
        line += f" | 正股价: {price}元 | 溢价率: {premium}%"
    else:
        line += " | 正股价: 暂无 | 溢价率: 暂无（行情数据缺失）"

    return line


def build_prompt(bonds, portfolio, risk):
    """构建发送给 DeepSeek 的分析 prompt。

    Args:
        bonds: 新债列表（集思录 raw rows）
        portfolio: config.yaml 中的 portfolio 列表
        risk: config.yaml 中的 risk dict

    Returns:
        str: 完整 prompt
    """
    summary = _build_portfolio_summary(portfolio)
    bond_lines = []
    for bond in bonds:
        cell = bond.get("cell", {})
        stock_cd = cell.get("stock_cd", "")
        market_data = get_market_data(stock_cd) if stock_cd else None
        bond_lines.append(_build_bond_line(bond, market_data))

    bonds_text = "\n".join(bond_lines)

    prompt = f"""你是一个专业的可转债分析助手，擅长从正股质地、转股价值、市场情绪等维度评估新债申购价值。

## 用户持仓概览
{summary}

## 风控约束
- 最大可接受亏损：{risk.get('stop_loss', -0.05) * 100:.0f}%
- 单只新债不超过总资金：{risk.get('max_position_ratio', 0.3) * 100:.0f}%

## 今日新债
{bonds_text}

## 分析要求
请从以下维度逐只分析并给出申购建议：
1. 正股质地（行业前景、基本面状况）
2. 转股价值与溢价率（当前是否有利）
3. 发行规模与中签率预估
4. 与用户现有持仓的相关性（是否过度集中）

## 输出格式
严格按照以下 JSON 格式输出，每只新债一个对象：

```json
{{
  "analyses": [
    {{
      "bond_name": "XX转债",
      "score": 7,
      "suggestion": "强力申购",
      "reason": "正股基本面良好，溢价率合理，建议参与申购"
    }}
  ]
}}
```

suggestion 必须是以下三个值之一："强力申购"、"谨慎申购"、"放弃申购"。
score 为 1-10 的整数。
只输出 JSON，不要输出其他内容。"""

    return prompt


def analyze(new_bonds, config):
    """对今日新债执行 AI 分析，返回每只债的分析结果。

    Args:
        new_bonds: fetch_new_bonds() 返回的今日新债列表
        config: 从 config.yaml 加载的完整配置 dict

    Returns:
        list[dict]: [{"bond_name": "...", "score": 7, "suggestion": "...", "reason": "..."}, ...]
        None: AI 不可用或分析失败时
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("[WARN] 未设置 DEEPSEEK_API_KEY，跳过 AI 分析")
        return None

    portfolio = config.get("portfolio", [])
    risk = config.get("risk", {})
    model = config.get("analysis", {}).get("model", "deepseek-chat")

    prompt = build_prompt(new_bonds, portfolio, risk)

    client = OpenAI(
        api_key=api_key,
        base_url="https://api.deepseek.com/v1",
    )

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "你是一个专业的可转债分析助手。请只输出 JSON，不要输出其他内容。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            timeout=120,
        )
        raw = response.choices[0].message.content.strip()

        # 用正则提取第一个 fenced code block，容错 preamble 和 trailing whitespace
        match = re.search(r'```(?:json)?\s*\n(.*?)```', raw, re.DOTALL)
        if match:
            raw = match.group(1).strip()

        result = json.loads(raw)
        return result.get("analyses", [])

    except (json.JSONDecodeError, KeyError) as e:
        print(f"[WARN] DeepSeek 返回格式解析失败: {e}")
        return None
    except (openai.APIConnectionError, openai.APIStatusError, openai.APITimeoutError) as e:
        print(f"[WARN] DeepSeek API 调用失败: {e}")
        return None
