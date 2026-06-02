# 打新债 AI 分析增强 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在打新债提醒工具中集成 DeepSeek AI 分析，推送时附带申购建议和评分。

**Architecture:** 单文件扩展。新增 `analysis.py`（DeepSeek 调用 + 东方财富行情）和 `config.yaml`（持仓配置），在 `main.py` 中串联：读取配置 → 获取新债 → AI 分析 → 推送。AI 失败时降级推送基础信息。

**Tech Stack:** Python 3.12, requests, pyyaml, openai SDK, DeepSeek Chat API, 东方财富 push2 行情接口, Server酱推送

---

## 文件结构

| 文件 | 操作 | 职责 |
|------|------|------|
| `config.yaml` | **创建** | 持仓配置、风控偏好、AI 开关 |
| `requirements.txt` | **创建** | Python 依赖声明 |
| `analysis.py` | **创建** | 行情获取 + DeepSeek 调用 + prompt 构建 |
| `main.py` | **修改** | 增加配置加载，串联 AI 分析，双模板推送 |

---

### Task 1: 创建 config.yaml

**Files:**
- Create: `config.yaml`

- [ ] **Step 1: 编写用户配置文件**

```yaml
# ===========================================
# 打新债提醒 - 用户配置文件
# ===========================================

# 你的基金/股票持仓（手动维护）
portfolio:
  - name: "沪深300ETF"
    type: "stock_etf"        # stock_etf / bond_fund / mixed_fund / stock / cash
    code: "510300"
    weight: 0.3              # 占总投资比例

  - name: "混合基金"
    type: "mixed_fund"
    code: "xxxxxx"
    weight: 0.5

  - name: "现金"
    type: "cash"
    weight: 0.2

# 风控偏好
risk:
  max_position_ratio: 0.3    # 单只新债不超过总资金的 30%
  stop_loss: -0.05           # 最大亏损 5%

# AI 分析设置
analysis:
  enabled: true              # false = 关闭 AI，纯推送新债名称
  model: "deepseek-chat"     # deepseek-chat 或 deepseek-reasoner
```

- [ ] **Step 2: 提交**

```bash
git add config.yaml
git commit -m "feat: add user config for portfolio and AI analysis"
```

---

### Task 2: 创建 requirements.txt

**Files:**
- Create: `requirements.txt`

- [ ] **Step 1: 编写依赖文件**

```
requests>=2.28.0
pyyaml>=6.0
openai>=1.0.0
```

- [ ] **Step 2: 提交**

```bash
git add requirements.txt
git commit -m "chore: add project dependencies"
```

---

### Task 3: 实现 analysis.py — get_market_data()

**Files:**
- Create: `analysis.py`

- [ ] **Step 1: 编写 get_market_data() 函数**

```python
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
            "price": data.get("f43", 0) / 100 if data.get("f43") else 0,      # 分→元
            "change_pct": data.get("f170", 0) / 100 if data.get("f170") else 0,  # 基点→百分比
            "pe": data.get("f167", 0) / 100 if data.get("f167") else None,      # PE TTM
            "pb": data.get("f46", 0) / 100 if data.get("f46") else None,        # PB
        }
    except (requests.RequestException, ValueError, KeyError):
        return None
```

- [ ] **Step 2: 提交**

```bash
git add analysis.py
git commit -m "feat: add get_market_data() with Eastmoney push2 API"
```

---

### Task 4: 实现 analysis.py — build_prompt()

**Files:**
- Modify: `analysis.py`（追加内容）

- [ ] **Step 1: 追加 build_prompt() 函数**

在 `analysis.py` 文件末尾追加以下代码：

```python
def _build_portfolio_summary(portfolio):
    """将持仓列表转为 AI 可读的文本摘要。"""
    lines = []
    for item in portfolio:
        type_label = {
            "stock_etf": "股票ETF", "bond_fund": "债券基金",
            "mixed_fund": "混合基金", "stock": "个股", "cash": "现金"
        }.get(item.get("type", ""), "未知")
        lines.append(
            f"- {item['name']}（{type_label}，代码 {item['code']}"
            f"，占比 {item['weight'] * 100:.0f}%）"
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
        if price > 0 and isinstance(convert_price, (int, float)) and convert_price > 0:
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
```

- [ ] **Step 2: 提交**

```bash
git add analysis.py
git commit -m "feat: add build_prompt() with portfolio and market data"
```

---

### Task 5: 实现 analysis.py — analyze()

**Files:**
- Modify: `analysis.py`（追加内容）

- [ ] **Step 1: 追加 analyze() 函数**

在 `analysis.py` 文件末尾追加以下代码：

```python
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

        # 去掉可能的 markdown 代码块标记
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[-1]  # 去掉第一行 ```json
            if raw.endswith("```"):
                raw = raw[:-3]  # 去掉末尾 ```

        result = json.loads(raw)
        return result.get("analyses", [])

    except (json.JSONDecodeError, KeyError) as e:
        print(f"[WARN] DeepSeek 返回格式解析失败: {e}")
        return None
    except Exception as e:
        print(f"[WARN] DeepSeek API 调用失败: {e}")
        return None
```

- [ ] **Step 2: 提交**

```bash
git add analysis.py
git commit -m "feat: add analyze() orchestrating DeepSeek call"
```

---

### Task 6: 修改 main.py — 集成 AI 分析

**Files:**
- Modify: `main.py`（原有 86 行，改约 40 行）

- [ ] **Step 1: 在 main.py 顶部增加导入和 load_config()**

在 `main.py` 第 2 行 `import requests` 之后插入 `import yaml`，在第 4 行 `send_notification` 之前插入 `load_config` 函数。最终修改为：

```python
import os
import sys
import requests
import yaml
from datetime import date

from analysis import analyze


def load_config(config_path="config.yaml"):
    """加载用户配置文件，文件不存在或格式错误时返回默认空配置。"""
    if not os.path.exists(config_path):
        print("[WARN] config.yaml 不存在，使用默认空配置（关闭 AI 分析）")
        return {
            "portfolio": [],
            "risk": {"max_position_ratio": 0.3, "stop_loss": -0.05},
            "analysis": {"enabled": False, "model": "deepseek-chat"},
        }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            raise ValueError("config.yaml 内容非字典格式")
        # 设置默认值，容忍缺失字段
        config.setdefault("portfolio", [])
        config.setdefault("risk", {"max_position_ratio": 0.3, "stop_loss": -0.05})
        config.setdefault("analysis", {"enabled": False, "model": "deepseek-chat"})
        return config
    except (yaml.YAMLError, ValueError) as e:
        print(f"[WARN] config.yaml 解析失败: {e}，使用默认空配置")
        return {
            "portfolio": [],
            "risk": {"max_position_ratio": 0.3, "stop_loss": -0.05},
            "analysis": {"enabled": False, "model": "deepseek-chat"},
        }
```

- [ ] **Step 2: 重写 send_notification() 支持 AI 消息模板**

将原有的 `send_notification(bond_names)` 函数替换为以下版本：

```python
def send_notification(bond_names, analyses=None):
    """通过 Server酱 推送到微信。analyses 为 AI 分析结果列表。"""
    sendkey = os.getenv("SENDKEY")
    if not sendkey:
        print("[ERROR] 未设置 SENDKEY 环境变量，无法推送")
        return False

    # 构建消息
    today_str = date.today().strftime("%Y-%m-%d")

    if analyses:
        # AI 分析可用：详细消息模板
        lines = ["🏦 今日有新债可申购！\n"]
        for a in analyses:
            score = a.get("score", "?")
            suggestion = a.get("suggestion", "未知")
            reason = a.get("reason", "")
            bond_name = a.get("bond_name", "未知转债")
            lines.append(f"📊 {bond_name}")
            lines.append(f"   🤖 AI评分：{score}/10 — {suggestion}")
            lines.append(f"   💡 {reason}")
            lines.append("")
        lines.append(f"---\n📅 申购日期：{today_str}")
        title = "🏦 今日有新债可申购！（AI 分析）"
        desp = "\n".join(lines)
    else:
        # 降级模式：仅名称推送
        title = "🏦 今日有新债可申购！"
        lines = ["🏦 今日有新债可申购！\n"]
        for name in bond_names:
            lines.append(f"📊 {name}")
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
```

- [ ] **Step 3: 改写 main() 函数集成 AI 分析流程**

将原有的 `main()` 函数（约第 68-79 行）替换为：

```python
def main():
    print("[INFO] 开始检查今日新债...")

    # ① 加载配置
    config = load_config()

    # ② 获取今日新债
    bonds, error = fetch_new_bonds()
    if error:
        print(f"[ERROR] 获取新债数据失败: {error}，不推送")
        return 1
    if not bonds:
        print("[INFO] 今日无新债可申购，不推送")
        return 0

    # ③ 提取名称（用于降级推送）
    names = get_bond_names(bonds)
    print(f"[INFO] 发现新债: {names}")

    # ④ AI 分析（如果配置开启且有 API Key）
    analyses = None
    if config.get("analysis", {}).get("enabled", False):
        print("[INFO] 正在调用 AI 分析...")
        analyses = analyze(bonds, config)
        if analyses:
            print(f"[INFO] AI 分析完成: {len(analyses)} 只新债")
        else:
            print("[WARN] AI 分析失败，降级为基础推送")

    # ⑤ 推送
    ok = send_notification(names, analyses)
    return 0 if ok else 1
```

- [ ] **Step 4: 删除原有的 send_notification 和 main 函数（已替换）**

确认 `main.py` 文件中没有重复的 `send_notification` 和 `main` 函数定义。删除第 41-80 行的旧版本（即原来两个函数的完整定义），确保只有 Step 2 和 Step 3 中的新版本。

**最终 main.py 完整内容应为：**

```python
import os
import sys
import requests
import yaml
from datetime import date

from analysis import analyze


def load_config(config_path="config.yaml"):
    """加载用户配置文件，文件不存在或格式错误时返回默认空配置。"""
    if not os.path.exists(config_path):
        print("[WARN] config.yaml 不存在，使用默认空配置（关闭 AI 分析）")
        return {
            "portfolio": [],
            "risk": {"max_position_ratio": 0.3, "stop_loss": -0.05},
            "analysis": {"enabled": False, "model": "deepseek-chat"},
        }
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        if not isinstance(config, dict):
            raise ValueError("config.yaml 内容非字典格式")
        config.setdefault("portfolio", [])
        config.setdefault("risk", {"max_position_ratio": 0.3, "stop_loss": -0.05})
        config.setdefault("analysis", {"enabled": False, "model": "deepseek-chat"})
        return config
    except (yaml.YAMLError, ValueError) as e:
        print(f"[WARN] config.yaml 解析失败: {e}，使用默认空配置")
        return {
            "portfolio": [],
            "risk": {"max_position_ratio": 0.3, "stop_loss": -0.05},
            "analysis": {"enabled": False, "model": "deepseek-chat"},
        }


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


def send_notification(bond_names, analyses=None):
    """通过 Server酱 推送到微信。analyses 为 AI 分析结果列表。"""
    sendkey = os.getenv("SENDKEY")
    if not sendkey:
        print("[ERROR] 未设置 SENDKEY 环境变量，无法推送")
        return False

    today_str = date.today().strftime("%Y-%m-%d")

    if analyses:
        lines = ["🏦 今日有新债可申购！\n"]
        for a in analyses:
            score = a.get("score", "?")
            suggestion = a.get("suggestion", "未知")
            reason = a.get("reason", "")
            bond_name = a.get("bond_name", "未知转债")
            lines.append(f"📊 {bond_name}")
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
            lines.append(f"📊 {name}")
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
    ok = send_notification(names, analyses)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 5: 提交**

```bash
git add main.py
git commit -m "feat: integrate DeepSeek AI analysis into main workflow"
```

---

### Task 7: 更新 GitHub Actions 工作流

**Files:**
- Modify: `.github/workflows/schedule.yml`

- [ ] **Step 1: 添加 pyyaml 和 openai 依赖安装**

将 `schedule.yml` 中的依赖安装步骤（`pip install requests`）修改为：

```yaml
      - name: 安装依赖
        run: pip install requests pyyaml openai
```

**修改后的完整 workflow 文件：**

```yaml
name: 打新债提醒

on:
  schedule:
    # UTC 1:00 = 北京时间 9:00, 周一到周五
    - cron: "0 1 * * 1-5"
  workflow_dispatch:

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

jobs:
  check-and-notify:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout 代码
        uses: actions/checkout@v4

      - name: 安装 Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 安装依赖
        run: pip install requests pyyaml openai

      - name: 检查新债并推送
        env:
          SENDKEY: ${{ secrets.SENDKEY }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python main.py
```

- [ ] **Step 2: 提交**

```bash
git add .github/workflows/schedule.yml
git commit -m "ci: add pyyaml, openai deps and DEEPSEEK_API_KEY secret"
```

---

### Task 8: 本地功能验证

**Files:** 无新建，验证所有已修改文件

- [ ] **Step 1: 语法检查**

```bash
python -c "import py_compile; py_compile.compile('main.py', doraise=True); py_compile.compile('analysis.py', doraise=True); print('语法检查通过')"
```

- [ ] **Step 2: 安装依赖**

```bash
pip install requests pyyaml openai
```

- [ ] **Step 3: 模拟运行（不带 API Key，验证降级逻辑）**

```bash
export SENDKEY="" 2>/dev/null || set SENDKEY=
python main.py
```

预期输出应包含 `[WARN] 未设置 SENDKEY 环境变量`，不应出现 Python traceback。

- [ ] **Step 4: 验证 load_config 容错逻辑**

```bash
# 临时重命名 config.yaml，验证降级
mv config.yaml config.yaml.bak
python -c "from main import load_config; c = load_config(); print('analysis enabled:', c['analysis']['enabled'])"
# 输出预期: analysis enabled: False
mv config.yaml.bak config.yaml
```

- [ ] **Step 5: 验收**

确认所有步骤无 Python 异常，降级逻辑按预期工作。
