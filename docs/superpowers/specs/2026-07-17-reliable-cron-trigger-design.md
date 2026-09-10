# 可靠定时触发方案 — 设计文档

## 背景

当前项目依赖两路触发来运行每日打新债检查：

1. **主触发器**：外部免费 cron 服务 → `repository_dispatch` → 每周一至周五北京时间 8:57
2. **兜底**：GitHub Actions `schedule` cron → UTC 01:00（北京时间 9:00）

2026-07-17（周五）出现严重延迟：主触发器未触发，GitHub schedule 延迟 3 小时 6 分钟（UTC 04:06 才运行），导致用户在 12:06 才收到通知。

### 根因总结

| 环节 | 预期 | 实际 | 原因 |
|------|------|------|------|
| 外部 cron 服务 | 8:57 触发 | 未触发 | 免费 cron 服务不稳定，可能过期/限流/停服 |
| GitHub schedule (兜底) | 9:00 | 12:06 | GitHub Actions scheduled workflows 不保证准时，高峰期延迟可达数小时 |

## 目标

- **定时精度**：每周一至周五北京时间 8:57 ± 3 分钟内触发
- **可靠性**：不依赖单个免费服务的生命周期
- **可观测**：每次运行可追溯触发来源和时间
- **零成本**：全部使用各平台的免费额度

## 架构

```
Cloudflare Workers Cron Trigger              GitHub Actions (保留兜底)
         │                                          │
         │ UTC 00:57 (北京 8:57)                     │ UTC 01:00 (北京 9:00)
         │ 准时、可靠                                │ 不保证准时
         ▼                                          ▼
    POST /repos/{owner}/{repo}/dispatches ────► GitHub API
                                                repository_dispatch
                                                event: check-new-bonds
                                                      │
                                                      ▼
                                              .github/workflows/schedule.yml
                                                      │
                                                      ▼
                                                 main.py → 微信通知
```

### 为什么选 Cloudflare Workers？

Cloudflare Workers 的 Cron Triggers：

- **免费额度**：每天 1 次触发，免费 quota 高达 100k 次/天，完全够用
- **准时性**：Cloudflare 基础设施级调度，偏差通常在 1 分钟以内
- **永续性**：不像小厂 cron 服务会过期、停服、限制免费版。无需维护账号的活跃状态
- **代码简单**：只需 ~20 行 JavaScript

### GitHub schedule 为什么保留？

作为纵深防御的最后一层。Cloudflare 挂掉的概率极低，但保留 GitHub schedule 兜底，即使延迟几小时也比完全不通知强。

## 组件设计

### 1. GitHub Personal Access Token

- 类型：Fine-grained token
- 仓库范围：仅 `wangdaguo-tester/new-bond-reminder`
- 权限：`Contents: Read and write`
- 轮换策略：最长有效期 1 年，到期前需手动创建新 token 并更新 Cloudflare secret

### 2. Cloudflare Worker

文件：`trigger.js`（存放于项目根目录，随代码一起版本管理）

```javascript
export default {
  async scheduled(event, env, ctx) {
    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`;
    
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${env.GITHUB_TOKEN}`,
        'Accept': 'application/vnd.github+json',
        'User-Agent': 'cloudflare-bond-reminder/1.0',
        'X-GitHub-Api-Version': '2022-11-28',
      },
      body: JSON.stringify({ event_type: 'check-new-bonds' }),
    });

    if (!resp.ok) {
      const text = await resp.text().catch(() => '');
      console.error(`[${resp.status}] ${text || '(no body)'}`);
    }
  },
};
```

**依赖的外部服务**：仅 GitHub API（`api.github.com`）。不依赖任何第三方 cron 平台。

### 3. wrangler.toml（Cloudflare 部署配置）

```toml
name = "bond-reminder-trigger"
main = "trigger.js"
compatibility_date = "2026-07-17"

[triggers]
crons = ["57 0 * * 1-5"]   # UTC 00:57 = 北京 8:57, 周一到周五
```

### 4. Workflow 日志增强

在 `.github/workflows/schedule.yml` 中增加触发信息 step，每次运行打印：

- 触发事件类型（`repository_dispatch` 还是 `schedule`）
- 触发时间

```yaml
- name: 打印触发信息
  run: |
    echo "触发事件: ${{ github.event_name }}"
    echo "触发时间(GitHub): ${{ github.event.repository.updated_at }}"
    echo "当前时间: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
```

每次运行后打开 Actions 日志即可一眼判断：
- `repository_dispatch` = Cloudflare 准时触发
- `schedule` = Cloudflare 未触发，GitHub 兜底生效（需排查 Worker）

### 5. 部署流程（一次性）

```bash
npm install -g wrangler
wrangler login
wrangler secret put GITHUB_OWNER     # → wangdaguo-tester
wrangler secret put GITHUB_REPO      # → new-bond-reminder
wrangler secret put GITHUB_TOKEN     # → Fine-grained PAT
wrangler deploy
```

## 测试策略

### 首次部署测试

1. 部署 Worker 后，在 Cloudflare Dashboard → Workers → Triggers 点击 "Trigger Now" 手动触发
2. 打开 GitHub Actions 查看是否有新的 workflow run 被触发
3. 确认日志中 `触发事件: repository_dispatch`
4. 确认微信收到通知

### 长期验证

1. 部署后第一个周一早上确认 8:57 左右收到通知
2. 连续观察一周，确认每天的触发事件都是 `repository_dispatch`

## 风险 & 应对

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| GitHub PAT 过期 | 1 年一次 | Worker 触发失败，退化为 schedule 兜底 | 日历提醒提前一周续期 PAT |
| Cloudflare 宕机 | 极低 | 退化为 schedule 兜底 | GitHub schedule 保留 |
| `wrangler` 命令不可用 | 低 | 需要重新部署时受阻 | wrangler 通过 npm 全局安装，随时可重装 |

## 涉及文件

| 文件 | 变更 |
|------|------|
| `trigger.js` | **新增** — Cloudflare Worker 代码 |
| `wrangler.toml` | **新增** — Cloudflare 部署配置 |
| `.github/workflows/schedule.yml` | **修改** — 增加触发信息日志 step |
