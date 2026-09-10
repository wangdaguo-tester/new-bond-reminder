# 多接收人微信推送 — 设计文档

**日期**: 2026-07-30
**状态**: 已批准

## 目标

支持向多个微信接收人推送新债提醒，在保留现有环境变量兼容的前提下，通过 config.yaml 管理 SendKey 列表。

## 变更范围

### config.yaml

新增 `notifications.sendkeys` 列表：

```yaml
notifications:
  sendkeys:
    - "SCT387450TPQJ0X0EqmHSXSCItqwEFdKyy"
    - "<已有的旧 SendKey>"
```

### main.py

1. `_DEFAULT_CONFIG` — 新增 `notifications: {"sendkeys": []}` 默认值
2. `load_config()` — 对 `notifications` 做 deep-merge（与 risk/analysis 一致）
3. `send_notification()` — 签名改为 `send_notification(bond_names, analyses=None, bonds=None, sendkeys=None)`，支持多 key 逐个推送
4. 兼容逻辑：config 有 sendkeys → 用 config；无 → fallback 到 `SENDKEY` 环境变量

### 容错

- 每个 key 独立推送，一个失败不影响其他
- 返回值：至少一个成功 → True；全部失败 → False
- 每次推送结果打印日志

## 不涉及

- trigger.js / wrangler.toml
- analysis.py
- requirements.txt
