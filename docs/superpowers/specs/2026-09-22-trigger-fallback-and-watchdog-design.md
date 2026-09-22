# 早上触发链修复与失效告警 — 设计文档

**日期**: 2026-09-22
**状态**: 已批准

## 背景

用户反馈:以前有新债的日子会收到两条提醒(早上 8:57、下午一点多),现在只剩下午那条。

排查 129 次 workflow 运行记录(仓库为 public,`/actions/runs` 可直接查询)后确认了四个独立的缺陷:

### 缺陷 1(主因):Cloudflare Worker 从 2026-08-17 起静默失效

```
2026-08-16 08:58  repository_dispatch   ← 最后一次
2026-08-17 10:19  schedule              ← 之后只剩 schedule,延迟 79 分钟
2026-09-22 13:54  schedule              ← 延迟 294 分钟
```

`repository_dispatch` 事件在 2026-08-16 之后一次都没有出现。没有该事件 = workflow 从未启动 = 早上那条提醒不可能发出。

时间线高度吻合 **PAT 过期**:Worker 于 2026-07-17 部署(本地 `.wrangler/tmp/deploy-lIZD77` 时间戳),GitHub 新建 PAT 的默认有效期为 30 天,即约 2026-08-16/17 到期,与最后一次成功、第一次缺失完全对齐。`trigger.js` 遇到非 2xx 只 `console.error`,不重试不告警,因此静默失效一个多月无人察觉。

### 缺陷 2:周五从未触发过

Cloudflare 的 cron 星期字段 `1 = 周日`,不是通常认知的 `1 = 周一`。官方文档原文:

> Days of the week go from 1 = Sunday to 7 = Saturday, which is different on some other cron systems (where 0 = Sunday and 6 = Saturday).

因此 `57 0 * * 1-5` 实际是**周日~周四**。运行记录完全印证:

```
7/26 Sun ✓   8/2 Sun ✓   8/9 Sun ✓   8/16 Sun ✓
7/27 Mon ✓   8/3 Mon ✓   8/10 Mon ✓
7/28 Tue ✓   8/4 Tue ✓   8/11 Tue ✓
7/29 Wed ✓   8/5 Wed ✓   8/12 Wed ✓
7/30 Thu ✓   8/6 Thu ✓   8/13 Thu ✓
7/31 Fri ✗   8/7 Fri ✗   8/14 Fri ✗   ← 连续三个周五全空
```

对比 2026-06 期间由旧外部 cron 服务产生的 dispatch(周一~周五,含周五、无周日),可见这是 Cloudflare 独有的语义差异。

### 缺陷 3:GitHub schedule 延迟 4.5~5 小时

GitHub 官方文档明确说明:

> The `schedule` event can be delayed during periods of high loads of GitHub Actions workflow runs. High load times include the start of every hour. To decrease the chance of delay, schedule your workflow to run at a different time of the hour.

现有配置 `0 1 * * 1-5` 正好踩在每个整点最拥堵的时段,实际送达集中在 13:29~13:56,最晚到过 20:32(已过申购窗口)。这就是用户看到的"下午一点多"那条。

### 缺陷 4:失效无告警

`trigger.js` 失败仅打日志,`main.py` 也没有任何"今天应该推送但没推"的检测。整条链断掉后唯一的信号是"用户觉得不对劲"。

## 目标

1. 修复早上 8:57 的主触发链(缺陷 1、2)
2. 降低 GitHub 兜底链的延迟(缺陷 3)
3. 主链失效时:既有兜底推送保证不漏提醒,又有独立告警告知"链坏了"(缺陷 4)
4. 正常情况下每天只有一条推送,不重复

## 触发链时间表

| 环节 | 文件 | 时间(北京) | 作用 |
|---|---|---|---|
| 主链 | `wrangler.toml` | 08:57 每天 | Cloudflare cron → `repository_dispatch` → 推送 |
| 告警 | `watchdog.yml`(新) | 09:11 周一~五 | 主链未成功 → 微信告警 |
| 兜底 | `schedule.yml` | 09:23 周一~五 | 主链未成功 → 补推提醒 |

主链改为**每天**触发(而非周一~五),是为了绕开 Cloudflare 的星期字段歧义 —— `2-6` 这种"正确但反直觉"的写法下次还会踩。每天触发不会多推:周末触发时 `main.py` 按 `apply_date == 今天` 过滤,没有新债就不推送。若某只债的申购日恰好落在周末,推送反而是正确行为。

告警与兜底都刻意避开整点(官方建议),且**告警早于兜底**,这样链路失效当天用户先收到告警,再收到补推,因果关系清楚。

## 变更范围

### wrangler.toml

```toml
[triggers]
crons = ["57 0 * * *"]
```

### trigger_status.py(新增)

单一职责:判断"今天主链是否已成功",不做任何推送。

```python
PRIMARY_OK = "ok"           # 今天有成功记录,或主链正在运行
PRIMARY_MISSING = "missing" # 今天没有成功记录(没有 / 失败)
PRIMARY_UNKNOWN = "unknown" # 查询本身失败,无法判断

def check_primary_today(now=None, repo=None, token=None, timeout=30) -> str
```

逻辑:

1. 确定"今天" —— 按北京时间(UTC+8)。跨日边界是 UTC 16:00 = 北京次日 00:00,不能直接用 UTC 日期。
2. `GET https://api.github.com/repos/{repo}/actions/runs?event=repository_dispatch&per_page=30`
3. 把每个 run 的 `created_at`(UTC)换算成北京时间,筛出日期等于今天的
4. 有任意一个 `conclusion == "success"`,或 `status != "completed"`(还在跑)→ `PRIMARY_OK`
5. 否则(没有今天的记录,或有但全部失败)→ `PRIMARY_MISSING`
6. 请求异常 / JSON 解析失败 / 拿不到 repo 信息 → `PRIMARY_UNKNOWN`

**为什么是三态而不是布尔**:两个消费方对"查不到"的诉求相反 —— 兜底推送宁可重复不可漏报(查不到就照推),而告警不能把"查不到"说成"主链挂了"(那是撒谎)。三态让各自都能诚实处理。

**为什么"还在跑"也算 OK**:避免主链 run 正在执行的窗口内兜底重复推送。

`repo` 默认取 `GITHUB_REPOSITORY` 环境变量,`token` 默认取 `GITHUB_TOKEN`;CI 中需显式传入默认 token —— 匿名调用 GitHub API 限额 60 次/小时,而 GitHub runner 共享出口 IP,很容易被限流。

CLI:`python trigger_status.py`,退出码 0 = `PRIMARY_OK`,非 0 = 其他(供 workflow 的 shell 门禁使用)。

### watchdog.py(新增)

入口脚本,约 30 行:`check_primary_today()` → 若为 `PRIMARY_MISSING` 或 `PRIMARY_UNKNOWN`,调 `main.send_alert()` 推一条告警。

告警文案**不依赖集思录数据**,所以即使抓取接口也挂了,告警仍能发出。内容包含状态原因和排查方向(PAT 是否过期 / Worker 是否还在 / cron 是否正确)。

### main.py

把现有 `send_notification()` 里的两段逻辑抽成可复用的私有原语,语义保持不变:

1. `_resolve_sendkeys(sendkeys=None) -> list[str]` — 过滤掉非字符串/空值,并入 `SENDKEY` 环境变量(去重),返回新列表,不修改入参
2. `_push_serverchan(keys, title, desp) -> bool` — 逐个推送,全部成功才返回 True;空 key 列表时打印错误并返回 False

新增 `send_alert(title, desp, sendkeys=None) -> bool` —— 复用上述原语推送任意文案,由调用方传入 sendkeys(`watchdog.py` 自己 `load_config()`,保持 `send_alert` 为纯推送原语)。

现有 4 个推送相关测试的语义与断言不变。

### .github/workflows/schedule.yml

1. cron `0 1 * * 1-5` → `23 1 * * 1-5`(09:23 北京),避开整点
2. 推送前新增去重门禁:

```yaml
- name: 判断主链是否已成功（仅 schedule 事件去重）
  id: primary
  if: github.event_name == 'schedule'
  run: |
    if python trigger_status.py; then
      echo "ok=true" >> "$GITHUB_OUTPUT"
    else
      echo "ok=false" >> "$GITHUB_OUTPUT"
    fi

- name: 检查新债并推送
  if: steps.primary.outputs.ok != 'true'
```

**必须避开的坑**:该 workflow 同时订阅 `repository_dispatch`。若门禁不判断事件类型,主链那次运行会查到"今天已有一个 dispatch 运行"——也就是它自己——从而判定 `ok` 并**跳过自己要发的提醒**。`if: github.event_name == 'schedule'` 就是防这个:非 schedule 事件下该步骤被跳过,输出为空,推送照常执行。手动 `workflow_dispatch` 因此也总是推送,便于测试。

### .github/workflows/watchdog.yml(新增)

只由自己的 cron(`11 1 * * 1-5`,即 09:11 北京)和 `workflow_dispatch` 触发,**不订阅 `repository_dispatch`** —— 它的运行不会污染 `event=repository_dispatch` 的状态查询,也不会在主链正常时产生噪音。

### 注释约定

两处 cron 的星期字段语义相反,必须在文件内写明:

- `wrangler.toml` — Cloudflare:**1 = 周日**
- `schedule.yml` — GitHub:标准语义,**1 = 周一**

## 容错与边界

| 情况 | 主链状态判断 | 兜底推送 | 告警 |
|---|---|---|---|
| 主链成功推送 | OK | 跳过 | 不发 |
| 主链成功但今日无新债 | OK | 跳过 | 不发 |
| 主链 run 推送失败(`main.py` 返回 1 → conclusion=failure) | MISSING | 补推 | 发 |
| 主链完全没触发 | MISSING | 补推 | 发 |
| 主链 run 正在执行中 | OK | 跳过 | 不发 |
| GitHub API 查询失败/限流 | UNKNOWN | 补推(宁可重复) | 发(说明是"无法确认") |

主链推送失败能被识别,是因为失败路径上 `main.py` 返回 1 → step 失败 → run `conclusion = failure`;而"今日无新债"返回 0,是健康状态,不会误判。

## 测试

`test_pipeline.py` 新增(沿用现有纯函数 + 假响应风格,不依赖网络):

1. 今天有 success run → OK
2. 今天只有 failed run → MISSING
3. 今天只有昨天的 run(含北京跨日边界:UTC 15:59 vs 16:00)→ MISSING
4. run 处于 in_progress → OK
5. 请求异常 → UNKNOWN
6. 拿不到 repo 信息 → UNKNOWN
7. `watchdog.main()`:OK 时不推送 / MISSING 时推送 / UNKNOWN 时推送
8. `_resolve_sendkeys` 的环境变量并入、去重、不修改入参

现有 43 个测试必须全部通过。

本地执行注意:Windows 控制台默认 GBK,现有 3 个推送测试在打印含 🏦 的标题时会抛 `UnicodeEncodeError` 而本地假失败(CI 的 Ubuntu 是 UTF-8,不受影响)。本次在 `test_pipeline.py` 的 `__main__` 块加一次 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`,让本地 `python test_pipeline.py` 能拿到真实信号。

## 不涉及

- Worker 内告警(已评估,覆盖不全:Worker 被删时它没机会发出任何东西)
- `trigger.js` 的重试逻辑
- 去重状态落盘(不引入任何持久化状态)
- `analysis.py`、`requirements.txt`
- `config.yaml` 中明文 SendKey 的处理(见下)

## 需人工执行的部署步骤

代码改动不会自动让 Cloudflare 生效,以下步骤需手动完成:

```bash
# 1. 生成新 PAT(fine-grained 勾 Contents: Read and write;或 classic 勾 repo)
#    ⚠️ 有效期不要再选默认的 30 天
wrangler login
wrangler secret put GITHUB_TOKEN   # → 新 PAT
wrangler deploy                    # 必须重新部署,cron 改动才会生效

# 2. 验证
wrangler tail                       # 观察下一次触发
```

## 遗留风险(不在本次范围)

仓库为 public,而 `config.yaml` 明文包含 SendKey,任何人都可读取并冒用推送。`1b00dbd` 曾试图清除该明文,随后被 `4d94450` revert。建议单独处理。
