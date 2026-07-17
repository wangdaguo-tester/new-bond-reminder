# Cloudflare Workers 可靠定时触发 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 Cloudflare Workers Cron 替代不稳定的外部 cron 服务，确保每周一至周五北京时间 8:57 准时触发打新债检查。

**Architecture:** Cloudflare Worker 通过 cron trigger 每天 UTC 00:57 调用 GitHub API 的 `repository_dispatch` 端点，触发 workflow 中的 `check-new-bonds` 事件。GitHub Actions 的 `schedule` cron 保留作为兜底。

**Tech Stack:** Cloudflare Workers (JavaScript), GitHub REST API, Wrangler CLI

## Global Constraints

- 定时精度：UTC 00:57（北京 8:57），周一到周五
- 零成本：仅使用 Cloudflare 免费额度（每天 1 次触发，免费 quota 100k/天）
- GitHub schedule `0 1 * * 1-5` 保留不动，作为兜底

---

### Task 1: 创建 Cloudflare Worker 代码 (`trigger.js`)

**Files:**
- Create: `trigger.js`

**Interfaces:**
- Consumes: 环境变量 `GITHUB_OWNER`, `GITHUB_REPO`, `GITHUB_TOKEN`（通过 `wrangler secret put` 注入）
- Produces: `export default { scheduled }` — Cloudflare Workers cron handler

- [ ] **Step 1: 写入 Worker 代码**

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

- [ ] **Step 2: 验证文件语法**

Run: `node --check trigger.js`
Expected: Exit code 0，无输出（语法正确）

- [ ] **Step 3: Commit**

```bash
git add trigger.js
git commit -m "feat: add Cloudflare Worker for reliable cron trigger"
```

---

### Task 2: 创建 Wrangler 部署配置 (`wrangler.toml`)

**Files:**
- Create: `wrangler.toml`

**Interfaces:**
- Consumes: `trigger.js`（Task 1 创建的文件）
- Produces: Cloudflare Workers 部署配置，包含 cron 表达式 `"57 0 * * 1-5"`

- [ ] **Step 1: 写入 wrangler.toml**

```toml
name = "bond-reminder-trigger"
main = "trigger.js"
compatibility_date = "2026-07-17"

[triggers]
crons = ["57 0 * * 1-5"]
```

- [ ] **Step 2: 验证 TOML 语法**

Run: `python3 -c "import tomllib; tomllib.load(open('wrangler.toml','rb'))" 2>&1 || python3 -c "import toml; toml.load('wrangler.toml')" 2>&1 || python3 -c "import tomli; tomli.load(open('wrangler.toml','rb'))" 2>&1`
Expected: 无报错输出（TOML 格式正确）。如果三个 import 都失败则手动目视检查即可。

- [ ] **Step 3: Commit**

```bash
git add wrangler.toml
git commit -m "feat: add wrangler.toml for Cloudflare Worker deployment"
```

---

### Task 3: 修改 Workflow 增加触发来源日志

**Files:**
- Modify: `.github/workflows/schedule.yml`

**Interfaces:**
- Consumes: GitHub Actions context (`github.event_name`, `github.event.repository.updated_at`)
- Produces: 每次 workflow 运行输出触发事件类型和时间

- [ ] **Step 1: 在 `检查新债并推送` step 之前插入诊断 step**

在 `.github/workflows/schedule.yml` 中，找到 `- name: 检查新债并推送`（第 30 行），在它**前面**插入：

```yaml
      - name: 打印触发信息
        run: |
          echo "触发事件: ${{ github.event_name }}"
          echo "当前时间: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
```

修改后的相关部分应为：

```yaml
      - name: 安装依赖
        run: pip install -r requirements.txt

      - name: 打印触发信息
        run: |
          echo "触发事件: ${{ github.event_name }}"
          echo "当前时间: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"

      - name: 检查新债并推送
        env:
          SENDKEY: ${{ secrets.SENDKEY }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python main.py
```

- [ ] **Step 2: 验证 YAML 语法**

Run: `python3 -c "import yaml; yaml.safe_load(open('.github/workflows/schedule.yml'))" 2>&1`
Expected: 无报错输出（YAML 格式正确）

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/schedule.yml
git commit -m "feat: add trigger source logging to workflow"
```

---

## 部署指引（按此顺序手动执行）

以下步骤不在自动化任务中，需人工完成：

```bash
# 1. 安装 wrangler CLI（一次性）
npm install -g wrangler

# 2. 登录 Cloudflare（一次性，会打开浏览器）
wrangler login

# 3. 注入密钥（需先在 GitHub 创建 Fine-grained PAT）
wrangler secret put GITHUB_OWNER     # → wangdaguo-tester
wrangler secret put GITHUB_REPO      # → new-bond-reminder
wrangler secret put GITHUB_TOKEN     # → github_pat_xxxx

# 4. 部署 Worker
wrangler deploy

# 5. 验证：在 Cloudflare Dashboard → Workers & Pages → bond-reminder-trigger → Triggers
#    点击 "Trigger Now" 手动触发，然后去 GitHub Actions 查看是否有新的 workflow run
```

## 完成标准

- [ ] `trigger.js` 和 `wrangler.toml` 已提交到仓库
- [ ] `.github/workflows/schedule.yml` 包含触发信息日志 step
- [ ] `wrangler deploy` 成功部署 Worker
- [ ] 手动 "Trigger Now" 后 GitHub Actions 出现 `event: repository_dispatch` 的 run
- [ ] 微信收到通知
- [ ] 下一个交易日北京时间 8:57 左右自动收到通知
