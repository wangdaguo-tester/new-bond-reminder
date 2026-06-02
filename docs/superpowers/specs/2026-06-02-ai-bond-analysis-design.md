# 打新债 AI 分析增强 — 设计文档

## 概述

在现有打新债提醒工具基础上，集成 DeepSeek AI 分析能力。有新债时不仅推送名称，还附带 AI 申购建议和评分；AI 不可用时降级推送基础信息。

## 动机

当前工具只推送新债名称，用户仍需自行判断是否申购。用户有明确的收益目标（10% 收益、最多 5% 亏损），借助 DeepSeek 的金融分析能力，可以在推送中直接给出申购建议。

## 架构

### 整体流程

```
GitHub Actions (每天 9:00 触发)
    │
    ▼
main.py
    │
    ├── ① 读取 config.yaml → 获取持仓 + 风控偏好
    │
    ├── ② 请求集思录 API → 获取当日新债列表
    │       │
    │       └── 无新债 → 打印日志，退出
    │
    ├── ③ 有新债 → 聚合数据（新债信息 + config 持仓）
    │       │
    │       ├── 正常 → ④ 构建 prompt → DeepSeek 分析
    │       │              │
    │       │              ├── 成功 → ⑤ 带 AI 建议推送微信（所有新债都推）
    │       │              └── 失败 → ⑤ 降级推送（名称 + "AI 分析暂时不可用"）
    │       │
    │       └── 异常 → 降级推送
    │
    └── ⑥ 记录日志到 stdout（GitHub Actions 可见）
```

### 项目结构

```
new-bond-reminder/
├── .github/workflows/schedule.yml   # GitHub Actions 定时配置（不变）
├── main.py                          # 主编排逻辑，串联各步骤
├── analysis.py                      # DeepSeek 分析调用 + 行情数据获取
├── config.yaml                      # 持仓配置、风控偏好、AI 开关
└── requirements.txt                 # requests + pyyaml + openai
```

## 数据流

### config.yaml → main.py → analysis.py → DeepSeek API

```
config.yaml         集思录 API
    │                    │
    ▼                    ▼
  main.py  ──聚合数据──►  analysis.py
    ▲                        │
    │                    DeepSeek
    │                        │
    │                        ▼
    │               AI 分析结果 (dict)
    │                        │
    └────────────────────────┘
    │
    ▼
  Server酱 → 微信推送
```

## config.yaml 设计

```yaml
# 用户基金/股票持仓
portfolio:
  - name: "沪深300ETF"
    type: "stock_etf"        # stock_etf / bond_fund / mixed_fund / stock / cash
    code: "510300"
    weight: 0.3              # 占总投资比例

  - name: "XX混合基金"
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

# AI 分析开关
analysis:
  enabled: true
  model: "deepseek-chat"     # deepseek-chat / deepseek-reasoner
```

- `analysis.enabled: false` 可随时关闭 AI，退回到纯推送模式
- `risk` 字段与用户在 deepseek.py 中的理财偏好一致
- 持仓由用户手动维护，简单可靠

## AI Prompt 设计

### 输入参数

| 数据 | 来源 | 说明 |
|------|------|------|
| 用户持仓概况 | config.yaml | 基金/ETF 名称、类型、权重 |
| 风控约束 | config.yaml | 最大亏损 5%、单只上限 30% |
| 新债基本信息 | 集思录 API | 债券名称、正股名称、转股价、发行规模、申购日期 |
| 市场行情 | 东方财富 push2 实时行情接口 | 正股当前价，计算转股价值和溢价率 |

### Prompt 模板结构

```
系统角色：你是一个专业的可转债分析助手，擅长从正股质地、
        转股价值、市场情绪等维度评估新债申购价值。

用户持仓概览：{portfolio_summary}
风控约束：最大亏损 {stop_loss}，单只不超过总资金 {max_position_ratio}

今日新债：
- {bond_name} | 正股: {stock_name} | 转股价: {convert_price} |
  正股价: {stock_price} | 溢价率: {premium_rate}% | 规模: {scale}亿

请从以下维度分析并给出申购建议：
1. 正股质地（行业前景、基本面状况）
2. 转股价值与溢价率（当前是否有利）
3. 发行规模与中签率预估
4. 与用户现有持仓的相关性（是否过度集中）

输出格式（JSON）：
{
  "score": 7,
  "suggestion": "强力申购" | "谨慎申购" | "放弃申购",
  "reason": "2-3句话的简要理由"
}
```

### 调用方式

- 使用 OpenAI 兼容 SDK（与 deepseek.py 一致）
- API Key 从环境变量 `DEEPSEEK_API_KEY` 读取
- JSON 模式要求 AI 结构化输出，便于代码解析

## 推送消息模板

### AI 可用时

```
🏦 今日有新债可申购！

📊 XX转债
   正股：XX股份 | 转股价：XX元 | 溢价率：XX%
   🤖 AI评分：7/10 — 强力申购
   💡 正股基本面良好，溢价率合理，建议参与

📊 YY转债
   ...

---
📅 申购日期：2026-06-02
```

### AI 不可用时（降级）

```
🏦 今日有新债可申购！

📊 XX转债 | 正股：XX股份
📊 YY转债 | 正股：YY科技

⚠️ AI 分析暂时不可用，请自行判断
📅 申购日期：2026-06-02
```

## analysis.py 职责

```
analysis.py
├── get_market_data(stock_code) → dict | None
│     通过东方财富 push2 接口获取正股实时行情
│     URL: https://push2.eastmoney.com/api/qt/stock/get
│     返回: {price, change_pct, pe, pb} 或 None（失败时）
│
├── build_prompt(bonds, portfolio, risk) → str
│     根据模板构建完整 prompt，将行情数据填入
│
└── analyze(new_bonds, config) → list[dict] | None
       编排：取行情 → 构建 prompt → 调 DeepSeek → 解析 JSON 结果
       异常或无法解析时返回 None，由 main.py 触发降级推送
```

## 容错策略

| 环节 | 异常处理 |
|------|----------|
| config.yaml 不存在或格式错误 | 打印错误日志，降级为纯推送模式 |
| 市场行情 API 失败 | 仅使用集思录数据构建 prompt，标注"行情数据缺失" |
| DeepSeek API 失败 | 降级推送基础信息，标注"AI 分析暂时不可用" |
| DeepSeek 返回格式不符 | 尝试解析，失败则降级 |
| 所有新债分析都失败 | 推送完整新债列表 + "AI 分析不可用" |

原则：**分析失败不阻塞推送**。

## 需用户手动操作

1. 在 GitHub Secrets 中添加 `DEEPSEEK_API_KEY`
2. 编辑 `config.yaml` 填入自己的持仓信息
3. 可选：调整 `analysis.enabled` 和 `risk` 参数

## 依赖变更

| 依赖 | 用途 | 变更 |
|------|------|------|
| `requests` | HTTP 请求 | 已有 |
| `pyyaml` | 解析 config.yaml | 新增 |
| `openai` | DeepSeek SDK 调用 | 新增 |
| Python 3.12 | 运行时 | 不变 |

## 测试策略

- `analysis.py` 中的函数可独立单元测试：mock DeepSeek 和行情 API
- `main.py` 集成测试通过 GitHub Actions workflow_dispatch 手动触发验证
- 配置解析错误场景测试
