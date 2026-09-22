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

**表里的时间是 cron 配置目标,不是送达承诺。** 避开整点是官方文档建议的**缓解**手段,不是保证:官方只说这样能"降低被延迟的概率",`schedule` 事件在负载高时仍可能晚到几分钟到数小时。而且 09:11 / 09:23 这两个分钟点在本仓库**没有任何实测数据**——历史上所有定时运行都来自旧的整点配置(`0 1`,实测延迟到 13:29~13:56,最晚 20:32)。因此"早上准时收到提醒"取决于 Cloudflare 主链被修好(PAT + `wrangler deploy`),不能指望 GitHub 兜底链准点。

## 变更范围

### wrangler.toml

```toml
[triggers]
crons = ["57 0 * * *"]
```

### trigger_status.py(新增)

单一职责:判断"今天主链跑得怎么样",不做任何推送。

```python
PRIMARY_OK = "ok"           # 今天有成功记录,或主链正在运行
PRIMARY_MISSING = "missing" # 今天压根没有运行记录(链没触发)
PRIMARY_FAILED = "failed"   # 今天有运行记录,但全部失败(链跑了,中途出错)
PRIMARY_UNKNOWN = "unknown" # 查询本身失败,无法判断

PrimaryVerdict = namedtuple("PrimaryVerdict", ["status", "run_url"])

def evaluate_primary_today(now=None, repo=None, token=None, timeout=30) -> PrimaryVerdict
def check_primary_today(now=None, repo=None, token=None, timeout=30) -> str   # 兼容入口
```

`evaluate_primary_today` 是真实实现(一次 HTTP 调用),`check_primary_today` 是薄包装,只返回 `.status` —— CLI 门禁和既有测试依赖这个字符串契约;需要拿到失败运行的链接时用前者。`run_url` 取今天最近一次运行的 `html_url`(API 按时间倒序返回),今天没有运行或该字段缺失时为 `None`。

逻辑:

1. 确定"今天" —— 按北京时间(UTC+8)。跨日边界是 UTC 16:00 = 北京次日 00:00,不能直接用 UTC 日期。
2. `GET https://api.github.com/repos/{repo}/actions/runs?event=repository_dispatch&per_page=30`
3. 把每个 run 的 `created_at`(UTC)换算成北京时间,筛出日期等于今天的
4. 今天没有任何运行记录 → `PRIMARY_MISSING`
5. 有任意一个 `conclusion == "success"`,或 `status != "completed"`(还在跑)→ `PRIMARY_OK`
6. 否则(有今天的记录,且全部 completed 但都不成功)→ `PRIMARY_FAILED`
7. 请求异常 / JSON 解析失败 / 拿不到 repo 信息 → `PRIMARY_UNKNOWN`

**为什么是四态而不是布尔**:两个消费方对"查不到"的诉求相反 —— 兜底推送宁可重复不可漏报(非 OK 就照推),而告警不能把"查不到"说成"主链挂了"(那是撒谎)。同样地,告警也不能把"链跑了但失败"说成"链没触发":前者的排查方向是**打开运行日志看卡在哪一步**,后者才是**查 PAT / Worker / cron**。混为一谈就是误诊 —— 而这正是"三态"版本的真实缺陷(`main.py` 在抓取失败或推送失败时返回 1,当天会被报成"没触发")。

`PRIMARY_FAILED` 与 `PRIMARY_MISSING` 一样让 CLI 退出码非 0,所以兜底链照推 —— 这是有意的(宁可重复一条,不可漏掉一条),两者的区别只体现在告警文案上。

**为什么"还在跑"也算 OK**:避免主链 run 正在执行的窗口内兜底重复推送。

`repo` 默认取 `GITHUB_REPOSITORY` 环境变量,`token` 默认取 `GITHUB_TOKEN`;CI 中需显式传入默认 token —— 匿名调用 GitHub API 限额 60 次/小时,而 GitHub runner 共享出口 IP,很容易被限流。

CLI:`python trigger_status.py`,退出码 0 = `PRIMARY_OK`,非 0 = 其他(供 workflow 的 shell 门禁使用)。

### watchdog.py(新增)

入口脚本:`evaluate_primary_today()` → 若为 `PRIMARY_MISSING` / `PRIMARY_FAILED` / `PRIMARY_UNKNOWN`,调 `main.send_alert()` 推一条告警,三种异常各有独立文案。

告警文案**不依赖集思录数据**,所以即使抓取接口也挂了,告警仍能发出。内容包含状态原因和排查方向:

- `MISSING` — 链压根没触发:查 PAT 是否过期 / Worker 是否还在 / cron 是否正确
- `FAILED` — 链跑了但失败:明确写出"这不是没触发",列出集思录抓取失败 / 推送失败 / AI 分析异常三种常见原因,并把失败那次的运行链接附在正文末尾(取 `PrimaryVerdict.run_url`)
- `UNKNOWN` — 查不到:说明是"无法确认",不冒充"主链挂了"

`watchdog.py` 只依赖 `requests` + `yaml`,不碰 AI 依赖栈 —— `main.py` 对 `analysis`(会拉起 `openai`)采用函数内延迟导入,否则 `openai` 一旦装不上,主链、兜底和告警会一起哑掉(正是本项目要消灭的复合静默失效)。

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

3. 回归测试步骤挪到**推送之后**(job 的最后一步)。GitHub Actions 里任何一步失败都会跳过它后面的所有步骤,测试若排在前面,一次测试失败就会把当天的最后一道提醒整个吞掉 —— 那正是本链条要消灭的静默漏报。放到最后,提醒已经发出,测试失败只会让本次运行标红(便于在 Actions 页面发现),不会再挡住提醒。(`watchdog.yml` 同理,它干脆不跑测试。)

### .github/workflows/watchdog.yml(新增)

只由自己的 cron(`11 1 * * 1-5`,即 09:11 北京)和 `workflow_dispatch` 触发,**不订阅 `repository_dispatch`** —— 它的运行不会污染 `event=repository_dispatch` 的状态查询,也不会在主链正常时产生噪音。

**安装依赖步骤刻意只装 `requests` + `pyyaml`,不是 `pip install -r requirements.txt`。** 后者含 `openai`,一旦装不上(未钉版本的 `openai>=1.0.0` 变得不兼容、PyPI 抖动),job 会在 `watchdog.py` 跑起来之前就死掉,一条告警都发不出 —— 那是与缺陷 4 同一类的复合静默失效,只是从 `import` 挪到了安装步骤。**不要为了"和其它 workflow 保持一致"把它改回 `requirements.txt`。**

### 注释约定

两处 cron 的星期字段语义相反,必须在文件内写明:

- `wrangler.toml` — Cloudflare:**1 = 周日**
- `schedule.yml` — GitHub:标准语义,**1 = 周一**

## 容错与边界

| 情况 | 主链状态判断 | 兜底推送 | 告警 |
|---|---|---|---|
| 主链成功推送 | OK | 跳过 | 不发 |
| 主链成功但今日无新债 | OK | 跳过 | 不发 |
| 主链 run 正在执行中 | OK | 跳过 | 不发 |
| 主链 run 推送失败(`main.py` 返回 1 → conclusion=failure) | FAILED | 补推 | 发(说明是"跑了但失败",附运行链接) |
| 主链 run 被取消 / 结论非 success 的其他情形 | FAILED | 补推 | 发(同上) |
| 主链完全没触发 | MISSING | 补推 | 发(排查 PAT / Worker / cron) |
| GitHub API 查询失败/限流 | UNKNOWN | 补推(宁可重复) | 发(说明是"无法确认") |

主链推送失败能被识别,是因为失败路径上 `main.py` 返回 1 → step 失败 → run `conclusion = failure`;而"今日无新债"返回 0,是健康状态,不会误判。"fail 了"与"没触发"分开,是因为两者的排查方向完全不同:前者要打开运行日志,后者才要查 PAT 和 Worker。

### 已接受的残留风险

**检查时刻仍在执行的 run 被判定为 OK。** 这是为了不抢跑重复推送(见上表),代价是:若某次 run 卡住很久、最终以失败结束,而检查(09:11 告警 / 09:23 兜底)恰好发生在它执行期间,那么这一天既不会补推也不会告警,只能等到用户自己发现。窗口很窄 —— 主链实测运行时长 14~37 秒,而检查点在 8:57 之后的 14 / 26 分钟。不为此引入持久化状态(见"不涉及")。

## 测试

`test_pipeline.py` 新增(沿用现有纯函数 + 假响应风格,不依赖网络):

1. 今天有 success run → OK
2. 今天只有 failed run → FAILED(跑了但失败,不能再报成 MISSING)
3. 今天既有失败也有成功(例如失败后重跑过)→ OK
4. 今天只有昨天的 run(含北京跨日边界:UTC 15:59 vs 16:00)→ MISSING
5. run 处于 in_progress → OK
6. 请求异常 → UNKNOWN;拿不到 repo 信息 → UNKNOWN
7. `evaluate_primary_today` 返回今天最近一次运行的 `html_url`;今天没有运行时 `run_url` 为 None;字段缺失时也是 None
8. `watchdog.run()`:OK 时不推送 / MISSING、FAILED、UNKNOWN 时各推一条、三条文案互不相同
9. FAILED 告警正文包含运行链接;拿不到链接时不出现"运行记录:None"这类半截提示
10. `_resolve_sendkeys` 的环境变量并入、去重、不修改入参
11. `import main` 不会连带 import `analysis`(告警链不依赖 AI 依赖栈)

既有测试必须全部通过。唯一必须改动的既有用例是 `test_check_primary_missing_when_today_run_failed` —— 它断言的正是被本次修复推翻的行为(失败 → MISSING),已更名为 `test_check_primary_failed_when_today_run_failed` 并断言 FAILED。

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
