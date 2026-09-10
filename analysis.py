"""新债 AI 分析模块 — 行情获取 + DeepSeek 调用"""

import json
import os
import re

import openai
import requests
from openai import OpenAI


# 模型可以给出的申购建议枚举
SUGGESTIONS = ("强力申购", "谨慎申购", "放弃申购")

# 从模型输出里提取 JSON 的两种兜底写法
_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)

# 接口用这些字符表示「无数据」，不能参与算术
_MISSING_TEXT = {"", "-", "--", "—", "－"}


def _parse_number(value, *, scale=1.0, positive_only=False):
    """把外部接口返回的字段稳妥地解析成 float。

    集思录和东方财富都用字符串返回数值，并用 "-"、"--"、"" 或 0 表示
    停牌/亏损/无数据。直接拿去做除法会抛 TypeError（字符串不受支持），
    这里统一归为 None，让调用方走「暂无」分支。

    Args:
        value: 原始字段值
        scale: 缩放系数（如 分→元 为 100）
        positive_only: 为 True 时非正数也算缺失

    Returns:
        float 或 None
    """
    if isinstance(value, bool) or value is None:
        return None

    if isinstance(value, (int, float)):
        num = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text in _MISSING_TEXT:
            return None
        try:
            num = float(text)
        except ValueError:
            return None
    else:
        return None

    num /= scale
    if positive_only and num <= 0:
        return None
    return num


def get_market_data(stock_code):
    """通过东方财富 push2 接口获取正股实时行情。

    Args:
        stock_code: 6位股票代码，如 "600519"

    Returns:
        dict: {"price": 1850.0, "change_pct": 2.5, "pe": 35.2, "pb": 8.1}
              取不到的字段为 None
        None: 代码非法或请求失败时
    """
    if not isinstance(stock_code, str) or len(stock_code) != 6 or not stock_code.isdigit():
        return None

    # 判断交易所：6开头=上海(1)，其他=深圳/北交所(0)
    secid = f"{'1' if stock_code.startswith('6') else '0'}.{stock_code}"

    url = "https://push2.eastmoney.com/api/qt/stock/get"
    # 字段 ID 已用真实接口逐个核对过：f46 是「今开价」而非 PB，
    # f167 是「市净率」而非 PE —— 原实现把这两个搞反了。
    params = {
        "secid": secid,
        "fields": "f43,f170,f164,f167",  # 最新价, 涨跌幅, 市盈率(TTM), 市净率
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None

    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, dict):
        return None

    return {
        "price": _parse_number(data.get("f43"), scale=100, positive_only=True),
        "change_pct": _parse_number(data.get("f170"), scale=100),
        "pe": _parse_number(data.get("f164"), scale=100, positive_only=True),
        "pb": _parse_number(data.get("f167"), scale=100, positive_only=True),
    }


def _market_snapshot(cell):
    """汇总单只新债正股的行情指标。

    以集思录自带字段打底（它同时给出权威的转股价值 pma_rt、评级和正股涨跌幅），
    再用东方财富实时行情覆盖 —— 东财能补上集思录没有的市盈率。
    两条来源都拿不到时字段为 None，由展示层降级，不会伪装成 0。
    """
    snapshot = {
        "price": _parse_number(cell.get("price"), positive_only=True),
        "change_pct": _parse_number(cell.get("increase_rt")),
        "pb": _parse_number(cell.get("pb"), positive_only=True),
        "pe": None,
        "source": "jisilu",
    }

    stock_code = cell.get("stock_id")
    quote = get_market_data(stock_code) if stock_code else None
    if quote:
        for field in ("price", "change_pct", "pe", "pb"):
            if quote.get(field) is not None:
                snapshot[field] = quote[field]
        snapshot["source"] = "jisilu+eastmoney"

    return snapshot


def _build_bond_line(index, cell, snapshot):
    """为单个新债构建一行描述文本。

    行首的 index 会要求模型原样回填，用于把分析结果对齐回债券，
    避免模型改名或漏答导致的信息错位。
    """
    bond_nm = cell.get("bond_nm") or "未知转债"
    stock_nm = cell.get("stock_nm") or "未知"
    rating = cell.get("rating_cd") or "未知"
    convert_price = _parse_number(cell.get("convert_price"), positive_only=True)
    issue_size = _parse_number(cell.get("amount"), positive_only=True)

    parts = [
        f"[{index}] {bond_nm}",
        f"正股: {stock_nm}",
        f"评级: {rating}",
        f"转股价: {convert_price}元" if convert_price is not None else "转股价: 未公布",
        f"发行规模: {issue_size}亿" if issue_size is not None else "发行规模: 未公布",
    ]

    price = snapshot.get("price")
    parts.append(f"正股价: {price}元" if price is not None else "正股价: 暂无")

    # 转股价值优先用集思录的 pma_rt（权威），缺失时才用现价自己算
    convert_value = _parse_number(cell.get("pma_rt"), positive_only=True)
    if convert_value is None and price is not None and convert_price:
        convert_value = price / convert_price * 100

    if convert_value is not None:
        # 新债发行价固定 100 元，故以 100 为基准算溢价率
        premium = round((100 - convert_value) / convert_value * 100, 1)
        parts.append(f"转股价值: {convert_value:.2f}元")
        parts.append(f"溢价率: {premium}%")
    else:
        parts.append("转股价值: 暂无")
        parts.append("溢价率: 暂无")

    change_pct = snapshot.get("change_pct")
    if change_pct is not None:
        parts.append(f"正股涨跌幅: {change_pct}%")
    pb = snapshot.get("pb")
    if pb is not None:
        parts.append(f"PB: {pb}")
    pe = snapshot.get("pe")
    if pe is not None:
        parts.append(f"PE(TTM): {pe}")

    return "- " + " | ".join(parts)


def build_prompt(bonds):
    """构建发送给 DeepSeek 的分析 prompt（仅基于新债自身信息）。

    Args:
        bonds: 新债列表（集思录 raw rows）

    Returns:
        str: 完整 prompt
    """
    bond_lines = []
    for index, bond in enumerate(bonds):
        raw_cell = bond.get("cell") if isinstance(bond, dict) else None
        cell = raw_cell if isinstance(raw_cell, dict) else {}
        bond_lines.append(_build_bond_line(index, cell, _market_snapshot(cell)))

    bonds_text = "\n".join(bond_lines)

    prompt = f"""你是一个专业的可转债分析助手，擅长从正股质地、转股价值、市场情绪等维度评估新债申购价值。

## 今日新债
{bonds_text}

## 分析要求
请对上面列出的每一只新债，从以下维度分析并给出申购建议：
1. 正股质地（行业前景、基本面状况、估值水平）
2. 转股价值与溢价率（当前是否有利）
3. 发行规模、评级与中签率预估

## 输出格式
严格按照以下 JSON 格式输出。index 必须原样回填上面每行开头的方括号编号；
必须覆盖上面列出的全部 {len(bonds)} 只新债，不要遗漏，也不要编造不存在的新债。

```json
{{
  "analyses": [
    {{
      "index": 0,
      "bond_name": "XX转债",
      "score": 7,
      "suggestion": "强力申购",
      "reason": "正股基本面良好，溢价率合理，建议参与申购"
    }}
  ]
}}
```

suggestion 必须是以下三个值之一："强力申购"、"谨慎申购"、"放弃申购"。
score 为 1-10 的整数。reason 请控制在 40 字以内。
只输出 JSON，不要输出其他内容。"""

    return prompt


def _norm_name(value):
    """债券名归一化，用于模型输出与真实名称的兜底匹配。"""
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", "", value).strip()


def _bond_name_of(bond):
    """安全取出债券名。"""
    raw_cell = bond.get("cell") if isinstance(bond, dict) else None
    cell = raw_cell if isinstance(raw_cell, dict) else {}
    return cell.get("bond_nm") or "未知转债"


def _normalize_analysis(item, bond_name):
    """校验并规范化单条分析结果。

    债券名一律以集思录的真实名称为准（不采信模型返回的名字），
    score 越界时收敛到 1-10；suggestion 不在枚举内时原样保留并截断，
    而不是替模型编一个建议。
    """
    try:
        score = int(item.get("score"))
    except (TypeError, ValueError):
        score = None
    if score is not None:
        score = max(1, min(10, score))

    suggestion = item.get("suggestion")
    suggestion = suggestion.strip() if isinstance(suggestion, str) else ""
    if suggestion not in SUGGESTIONS:
        suggestion = suggestion[:20] if suggestion else "未知建议"

    reason = item.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""

    return {
        "bond_name": bond_name,
        "score": score,
        "suggestion": suggestion,
        "reason": reason,
    }


def align_analyses(items, bonds):
    """把模型返回的分析结果对齐到输入债券列表。

    优先按 item["index"]（与 prompt 中的编号一致）对齐；编号缺失或越界时，
    退化为按 bond_name 匹配。对不上的条目一律丢弃，防止模型编造的新债被推送出去；
    没被覆盖的位置为 None，由调用方降级显示。

    Returns:
        list[dict | None]: 与 bonds 等长、逐项对应。
    """
    aligned = [None] * len(bonds)
    unplaced = []

    for item in items:
        if not isinstance(item, dict):
            continue
        index = item.get("index")
        # bool 是 int 的子类，必须显式排除，否则 True 会被当成下标 1
        if isinstance(index, bool) or not isinstance(index, int):
            unplaced.append(item)
            continue
        if 0 <= index < len(bonds) and aligned[index] is None:
            aligned[index] = item
        else:
            unplaced.append(item)

    for item in unplaced:
        target = _norm_name(item.get("bond_name"))
        if not target:
            continue
        for i, slot in enumerate(aligned):
            if slot is None and _norm_name(_bond_name_of(bonds[i])) == target:
                aligned[i] = item
                break

    return [
        _normalize_analysis(item, _bond_name_of(bonds[i])) if item else None
        for i, item in enumerate(aligned)
    ]


def _extract_json(raw):
    """从模型输出中提取 JSON 文本，容忍围栏标记和前后寒暄。"""
    match = _JSON_FENCE_RE.search(raw)
    if match:
        return match.group(1).strip()
    match = _JSON_OBJECT_RE.search(raw)
    if match:
        return match.group(0)
    return raw.strip()


def analyze(new_bonds, config):
    """对今日新债执行 AI 分析。

    Args:
        new_bonds: fetch_new_bonds() 返回的今日新债列表
        config: 从 config.yaml 加载的完整配置 dict

    Returns:
        list[dict | None]: 与 new_bonds 等长，元素为分析结果或 None（未覆盖）
        None: AI 不可用或整体分析失败时
    """
    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        print("[WARN] 未设置 DEEPSEEK_API_KEY，跳过 AI 分析")
        return None

    model = config.get("analysis", {}).get("model", "deepseek-chat")
    prompt = build_prompt(new_bonds)

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
        raw = response.choices[0].message.content
    except openai.OpenAIError as e:
        print(f"[WARN] DeepSeek API 调用失败: {e}")
        return None
    except Exception as e:
        # 兜住 choices 为空、响应结构变化等非 API 异常
        print(f"[WARN] DeepSeek 调用出现异常: {type(e).__name__}: {e}")
        return None

    if not isinstance(raw, str) or not raw.strip():
        print("[WARN] DeepSeek 返回内容为空")
        return None

    try:
        result = json.loads(_extract_json(raw))
    except (json.JSONDecodeError, TypeError) as e:
        print(f"[WARN] DeepSeek 返回格式解析失败: {e}；原文片段: {raw[:200]!r}")
        return None

    if not isinstance(result, dict) or not isinstance(result.get("analyses"), list):
        print(f"[WARN] DeepSeek 返回结构异常（缺少 analyses 数组）: {str(result)[:200]}")
        return None

    aligned = align_analyses(result["analyses"], new_bonds)
    covered = sum(1 for a in aligned if a)
    if covered < len(new_bonds):
        print(f"[WARN] AI 仅覆盖 {covered}/{len(new_bonds)} 只新债，未覆盖的将降级显示")
    if covered == 0:
        print("[WARN] AI 返回的条目无法与任何新债对齐，视为分析失败")
        return None

    return aligned
