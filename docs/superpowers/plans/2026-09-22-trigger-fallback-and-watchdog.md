# 早上触发链修复与失效告警 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 修复早上 8:57 的主触发链,并在它失效时既能兜底补推、又能发出告警,正常日子不重复推送。

**Architecture:** Cloudflare Worker 的 cron 改为每天 08:57 触发 `repository_dispatch`(主链)。新增 `trigger_status.py` 判断"今天主链是否已成功",返回三态;`schedule.yml` 用它做去重门禁(GitHub schedule 只在主链没成功时才补推);新增 `watchdog.yml` + `watchdog.py` 在 09:11 检查同一状态,异常时用 Server酱 告警。

**Tech Stack:** Python 3.12(标准库 + `requests`)、GitHub Actions、Cloudflare Workers cron、yaml/toml 配置。

**设计文档:** `docs/superpowers/specs/2026-09-22-trigger-fallback-and-watchdog-design.md`

## Global Constraints

- 依赖只用标准库 + `requests`(已在 `requirements.txt`),不新增依赖
- 测试不依赖网络:所有 HTTP 调用都用假响应替换,并在 `finally` 中还原
- 不引入任何持久化状态(不去重落盘、不写文件)
- 提交信息用单行 conventional commit,中文描述,不加正文、不加署名
- 提交直接落在 `master`
- **Cloudflare 的 cron 星期字段 `1 = 周日`;GitHub Actions 的标准语义 `1 = 周一`** —— 两处必须写注释说明,避免再踩
- 本地跑测试必须带 `PYTHONUTF8=1`(Windows 控制台默认 GBK,打印 🏦 会抛 `UnicodeEncodeError` 造成假失败);CI 是 UTF-8 不受影响
- 现有 43 个测试必须保持通过

---

### Task 1: main.py 抽出推送原语 + 新增 send_alert

把 `send_notification()` 里的"解析 SendKey"和"逐个推送"抽成可复用原语,供看门狗推送告警。同时修掉 `test_pipeline.py` 在 Windows 上的假失败,并纠正一处与代码不符的 docstring。

**Files:**
- Modify: `main.py:161-205`(整个 `send_notification`)
- Test: `test_pipeline.py`(新增测试 + 新 helper + `__main__` 块)

**Interfaces:**
- Consumes: 无(第一个任务)
- Produces:
  - `main._resolve_sendkeys(sendkeys=None) -> list[str]`
  - `main._push_serverchan(keys, title, desp) -> bool`
  - `main.send_notification(bonds, analyses=None, sendkeys=None) -> bool`(签名不变)
  - `main.send_alert(title, desp, sendkeys=None) -> bool`
  - `test_pipeline._with_env_sendkey(value) -> callable`

- [ ] **Step 1: 修复本地测试的编码假失败**

`test_pipeline.py` 末尾的 `if __name__ == "__main__":` 块第一行插入:

```python
if __name__ == "__main__":
    # Windows 控制台默认 GBK,打印含 emoji 的推送标题会抛 UnicodeEncodeError,
    # 造成 3 个推送测试在本地假失败(CI 的 Ubuntu 是 UTF-8,本来就没问题)。
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    tests = [(n, f) for n, f in sorted(globals().items())
```

- [ ] **Step 2: 跑一遍,确认基线是绿的**

Run: `PYTHONUTF8=1 python test_pipeline.py 2>&1 | tail -2`
Expected: `43 passed, 0 failed`(修复前是 `40 passed, 3 failed`)

- [ ] **Step 3: 写失败测试**

在 `test_pipeline.py` 的 `_without_env_sendkey` 定义处,替换成通用版本(原有行为不变):

```python
def _with_env_sendkey(value):
    """临时把 SENDKEY 设为 value(None 表示删除),返回还原函数。"""
    original = os.environ.get("SENDKEY")
    if value is None:
        os.environ.pop("SENDKEY", None)
    else:
        os.environ["SENDKEY"] = value

    def restore():
        if original is None:
            os.environ.pop("SENDKEY", None)
        else:
            os.environ["SENDKEY"] = original

    return restore


def _without_env_sendkey():
    """临时清掉 SENDKEY 环境变量,返回还原函数。"""
    return _with_env_sendkey(None)
```

在 `test_send_notification_fails_when_any_recipient_fails` 之后、`# build_message` 分节之前,插入新分节:

```python
# --------------------------------------------------------------------------
# 推送原语:_resolve_sendkeys / send_alert
# --------------------------------------------------------------------------

def test_resolve_sendkeys_filters_bad_values_and_merges_env():
    restore_env = _with_env_sendkey("ENVKEY")
    try:
        keys = m._resolve_sendkeys(["KEY1", "  ", None, 42, "KEY2"])
    finally:
        restore_env()

    assert keys == ["KEY1", "KEY2", "ENVKEY"]


def test_resolve_sendkeys_deduplicates_env_key():
    restore_env = _with_env_sendkey("KEY1")
    try:
        keys = m._resolve_sendkeys(["KEY1"])
    finally:
        restore_env()

    assert keys == ["KEY1"]


def test_resolve_sendkeys_does_not_mutate_input():
    restore_env = _with_env_sendkey("ENVKEY")
    try:
        sendkeys = ["KEY1"]
        m._resolve_sendkeys(sendkeys)
    finally:
        restore_env()

    assert sendkeys == ["KEY1"], "不应把环境变量写回调用方的列表"


def test_send_alert_pushes_to_every_key():
    posted = []
    restore_post = _patch_post(posted)
    restore_env = _with_env_sendkey(None)
    try:
        ok = m.send_alert("标题", "正文", ["KEY1", "KEY2"])
    finally:
        restore_post()
        restore_env()

    assert ok is True
    assert len(posted) == 2
    assert posted[0].endswith("/KEY1.send") and posted[1].endswith("/KEY2.send")


def test_send_alert_without_any_key_returns_false():
    restore_env = _with_env_sendkey(None)
    try:
        assert m.send_alert("标题", "正文", None) is False
        assert m.send_alert("标题", "正文", [None, "", 42]) is False
    finally:
        restore_env()
```

- [ ] **Step 4: 跑测试,确认失败**

Run: `PYTHONUTF8=1 python -m pytest test_pipeline.py -k "resolve_sendkeys or send_alert" -v`
Expected: FAIL —`AttributeError: module 'main' has no attribute '_resolve_sendkeys'`

- [ ] **Step 5: 改写 main.py**

把 `main.py` 中 `send_notification`(第 161~205 行,含 docstring)整段替换为:

```python
def _resolve_sendkeys(sendkeys=None):
    """汇总可用的 SendKey:传入列表(过滤坏值) + SENDKEY 环境变量(去重)。

    返回新列表,不修改调用方传入的列表。
    """
    keys = [k for k in (sendkeys or []) if isinstance(k, str) and k.strip()]
    env_key = os.getenv("SENDKEY")
    if env_key and env_key not in keys:
        keys.append(env_key)
    return keys


def _push_serverchan(keys, title, desp):
    """向每个 SendKey 推送同一条消息,全部成功才返回 True。"""
    if not keys:
        print("[ERROR] 未配置任何 SendKey(config.yaml 的 notifications.sendkeys "
              "或 SENDKEY 环境变量),无法推送")
        return False

    payload = {"title": title, "desp": desp}
    success_count = 0
    for sendkey in keys:
        url = f"https://sctapi.ftqq.com/{sendkey}.send"
        try:
            resp = requests.post(url, data=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                print(f"[INFO] 推送成功 (SendKey: {sendkey[:12]}...): {title}")
                success_count += 1
            else:
                print(f"[ERROR] 推送失败 (SendKey: {sendkey[:12]}...): {result}")
        except requests.exceptions.RequestException as e:
            print(f"[ERROR] 推送异常 (SendKey: {sendkey[:12]}...): {e}")

    if success_count == len(keys):
        print(f"[INFO] 推送完成: {success_count}/{len(keys)} 成功")
        return True
    print(f"[ERROR] 推送未全部成功: {success_count}/{len(keys)} 成功")
    return False


def send_notification(bonds, analyses=None, sendkeys=None):
    """通过 Server酱 推送到微信,支持多个接收人。

    Args:
        bonds: 今日新债列表
        analyses: 与 bonds 等长的分析结果列表(元素为 dict 或 None)
        sendkeys: SendKey 列表;SENDKEY 环境变量会自动并入(去重)

    Returns:
        bool: 全部接收人推送成功才为 True
    """
    title, desp = build_message(bonds, analyses)
    return _push_serverchan(_resolve_sendkeys(sendkeys), title, desp)


def send_alert(title, desp, sendkeys=None):
    """推送一条自定义告警文案(不依赖新债数据)。

    Args:
        title: 告警标题
        desp: 告警正文
        sendkeys: SendKey 列表;SENDKEY 环境变量会自动并入(去重)

    Returns:
        bool: 全部接收人推送成功才为 True
    """
    return _push_serverchan(_resolve_sendkeys(sendkeys), title, desp)
```

注意:原文 docstring 写的是"至少一个推送成功即为 True",但代码是 `success_count == len(keys)`,即**全部**成功才为 True(`test_send_notification_fails_when_any_recipient_fails` 也是这么断言的)。上面的新 docstring 已纠正为"全部成功"。

- [ ] **Step 6: 跑全部测试,确认通过**

Run: `PYTHONUTF8=1 python test_pipeline.py 2>&1 | tail -2`
Expected: `48 passed, 0 failed`(43 + 5)

- [ ] **Step 7: 提交**

```bash
git add main.py test_pipeline.py
git commit -m "refactor: 抽出 SendKey 解析与推送原语,新增 send_alert"
```

---

### Task 2: trigger_status.py(判断主链今天是否已成功)

**Files:**
- Create: `trigger_status.py`
- Test: `test_pipeline.py`(新增分节)

**Interfaces:**
- Consumes: 无
- Produces:
  - `trigger_status.BJT`(`datetime.timezone`,UTC+8)
  - `trigger_status.PRIMARY_OK` / `PRIMARY_MISSING` / `PRIMARY_UNKNOWN`(字符串常量)
  - `trigger_status.check_primary_today(now=None, repo=None, token=None, timeout=30) -> str`
  - `trigger_status.main() -> int`(0 = OK,非 0 = 其他;供 workflow shell 门禁使用)
  - `trigger_status._to_bjt(value) -> datetime | None`

- [ ] **Step 1: 写失败测试**

`test_pipeline.py` 顶部 import 区(`from datetime import date`)改成:

```python
from datetime import date, datetime
```

并在 `import analysis` 之后加 `import trigger_status`。然后在文件末尾 `# ---` 分隔线的**上方**插入:

```python
# --------------------------------------------------------------------------
# trigger_status:判断今天主链是否已成功
# --------------------------------------------------------------------------

def _bjt(*args):
    return datetime(*args, tzinfo=trigger_status.BJT)


def _run(created_at, conclusion="success", status="completed"):
    return {"created_at": created_at, "conclusion": conclusion, "status": status}


def _patch_runs(payload):
    """把 trigger_status.requests.get 换成返回固定 payload 的假实现。"""
    original = trigger_status.requests.get
    trigger_status.requests.get = lambda *a, **k: _FakeResp(payload)
    return lambda: setattr(trigger_status.requests, "get", original)


def _patch_runs_error():
    """让 trigger_status.requests.get 抛请求异常。"""
    original = trigger_status.requests.get

    def fake_get(*args, **kwargs):
        raise trigger_status.requests.RequestException("simulated failure")

    trigger_status.requests.get = fake_get
    return lambda: setattr(trigger_status.requests, "get", original)


def test_check_primary_ok_when_today_run_succeeded():
    restore = _patch_runs({"workflow_runs": [_run("2026-09-22T00:57:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_check_primary_missing_when_today_run_failed():
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T00:57:00Z", conclusion="failure")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_MISSING


def test_check_primary_ok_while_today_run_is_still_running():
    """主链正在跑的时候,兜底不能抢跑,否则会重复推送。"""
    restore = _patch_runs({"workflow_runs": [
        _run("2026-09-22T00:57:00Z", conclusion=None, status="in_progress")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_check_primary_missing_when_only_yesterdays_run():
    """UTC 15:59 = 北京 23:59,算昨天。"""
    restore = _patch_runs({"workflow_runs": [_run("2026-09-21T15:59:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_MISSING


def test_check_primary_counts_utc16_as_today_in_beijing():
    """UTC 16:00 = 北京次日 00:00,算今天。"""
    restore = _patch_runs({"workflow_runs": [_run("2026-09-21T16:00:00Z")]})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_OK


def test_check_primary_unknown_on_request_error():
    restore = _patch_runs_error()
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_UNKNOWN


def test_check_primary_unknown_without_repo():
    original = os.environ.pop("GITHUB_REPOSITORY", None)
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo=None)
    finally:
        if original is not None:
            os.environ["GITHUB_REPOSITORY"] = original

    assert status == trigger_status.PRIMARY_UNKNOWN


def test_check_primary_unknown_on_unexpected_payload():
    restore = _patch_runs({"message": "Not Found"})
    try:
        status = trigger_status.check_primary_today(now=_bjt(2026, 9, 22, 9, 11),
                                                    repo="owner/repo", token="")
    finally:
        restore()

    assert status == trigger_status.PRIMARY_UNKNOWN


def test_trigger_status_exit_code_is_zero_only_when_ok():
    """workflow 门禁靠退出码判断:只有 OK 才算"主链没问题"。"""
    original = trigger_status.check_primary_today
    try:
        for status, expected in ((trigger_status.PRIMARY_OK, 0),
                                 (trigger_status.PRIMARY_MISSING, 1),
                                 (trigger_status.PRIMARY_UNKNOWN, 1)):
            trigger_status.check_primary_today = lambda *a, **k: status
            assert trigger_status.main() == expected
    finally:
        trigger_status.check_primary_today = original
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONUTF8=1 python -m pytest test_pipeline.py -k "check_primary or exit_code" -v`
Expected: collection error —`ModuleNotFoundError: No module named 'trigger_status'`

- [ ] **Step 3: 创建 trigger_status.py**

```python
"""判断今天早上 8:57 的主触发链是否已成功。

主链 = Cloudflare Worker 定时触发出来的 repository_dispatch 运行。
本模块只做判断:不推送、不写状态。三态返回值是为了让调用方诚实区分
"确认没成功"和"查不到"——兜底推送要偏保守(查不到就照推),
而告警不能把"查不到"说成"主链挂了"。
"""

import os
import sys
from datetime import datetime, timedelta, timezone

import requests

BJT = timezone(timedelta(hours=8))

PRIMARY_OK = "ok"            # 今天已有成功的运行,或主链正在运行
PRIMARY_MISSING = "missing"  # 今天没有成功记录(没有触发,或有但失败)
PRIMARY_UNKNOWN = "unknown"  # 查询本身失败,无法判断

_API = "https://api.github.com"
_PER_PAGE = 30


def _to_bjt(value):
    """把 GitHub 的 ISO 时间串换算成北京时间;解析不了返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).astimezone(BJT)


def check_primary_today(now=None, repo=None, token=None, timeout=30):
    """返回 PRIMARY_OK / PRIMARY_MISSING / PRIMARY_UNKNOWN。

    判定为 OK:今天的任一 repository_dispatch 运行满足
      - conclusion == "success",或
      - 还没跑完(status != "completed"),避免主链正在跑时兜底抢跑重复推送

    "今天"按北京时间算:UTC 16:00 已经是北京的次日 00:00。
    """
    repo = repo or os.getenv("GITHUB_REPOSITORY")
    if not repo:
        print("[WARN] 拿不到仓库信息(GITHUB_REPOSITORY 未设置),无法判断主链状态")
        return PRIMARY_UNKNOWN

    if token is None:
        token = os.getenv("GITHUB_TOKEN")

    today = (now or datetime.now(BJT)).astimezone(BJT).date()

    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.get(
            f"{_API}/repos/{repo}/actions/runs",
            headers=headers,
            params={"event": "repository_dispatch", "per_page": _PER_PAGE},
            timeout=timeout,
        )
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        print(f"[WARN] 查询主链运行记录失败: {e}")
        return PRIMARY_UNKNOWN
    except ValueError as e:
        print(f"[WARN] 主链运行记录不是合法 JSON: {e}")
        return PRIMARY_UNKNOWN

    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        print(f"[WARN] 主链运行记录结构异常: {str(data)[:200]}")
        return PRIMARY_UNKNOWN

    today_runs = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        created = _to_bjt(run.get("created_at"))
        if created is not None and created.date() == today:
            today_runs.append(run)

    if not today_runs:
        return PRIMARY_MISSING

    for run in today_runs:
        if run.get("status") != "completed" or run.get("conclusion") == "success":
            return PRIMARY_OK

    return PRIMARY_MISSING


def main():
    status = check_primary_today()
    print(f"[INFO] 今天的主链状态: {status}")
    return 0 if status == PRIMARY_OK else 1


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONUTF8=1 python -m pytest test_pipeline.py -k "check_primary or exit_code" -v`
Expected: 9 passed

- [ ] **Step 5: 跑全部测试,确认无回归**

Run: `PYTHONUTF8=1 python test_pipeline.py 2>&1 | tail -2`
Expected: `57 passed, 0 failed`(48 + 9)

- [ ] **Step 6: 提交**

```bash
git add trigger_status.py test_pipeline.py
git commit -m "feat: 新增主触发链状态判断(三态),供兜底去重与看门狗复用"
```

---

### Task 3: watchdog.py(主链失效时告警)

**Files:**
- Create: `watchdog.py`
- Test: `test_pipeline.py`(新增分节)

**Interfaces:**
- Consumes: `trigger_status.check_primary_today()`、`trigger_status.PRIMARY_*`、`main.send_alert()`、`main.load_config()`
- Produces:
  - `watchdog.MISSING_TITLE` / `MISSING_BODY` / `UNKNOWN_TITLE` / `UNKNOWN_BODY`
  - `watchdog.build_alert(status) -> tuple[str, str] | None`
  - `watchdog.run() -> int`(0 = 正常或告警已发出,1 = 告警发送失败)

- [ ] **Step 1: 写失败测试**

`test_pipeline.py` 顶部在 `import trigger_status` 之后加 `import watchdog`。然后在 `# ---` 分隔线上方插入:

```python
# --------------------------------------------------------------------------
# watchdog:主链没成功时告警
# --------------------------------------------------------------------------

def _patch_watchdog(status, alert_result=True):
    """替换看门狗的"查状态""读配置""发告警",返回 (calls, restore)。"""
    calls = []
    original_check = watchdog.trigger_status.check_primary_today
    original_alert = watchdog.main.send_alert
    original_config = watchdog.main.load_config

    def fake_alert(title, desp, sendkeys=None):
        calls.append((title, desp, sendkeys))
        return alert_result

    watchdog.trigger_status.check_primary_today = lambda *a, **k: status
    watchdog.main.send_alert = fake_alert
    watchdog.main.load_config = lambda *a, **k: {
        "notifications": {"sendkeys": ["KEY1"]}}

    def restore():
        watchdog.trigger_status.check_primary_today = original_check
        watchdog.main.send_alert = original_alert
        watchdog.main.load_config = original_config

    return calls, restore


def test_watchdog_stays_silent_when_primary_ok():
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_OK)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 0
    assert calls == []


def test_watchdog_alerts_when_primary_missing():
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_MISSING)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 0
    assert len(calls) == 1
    title, desp, sendkeys = calls[0]
    assert "触发链" in title
    assert "GITHUB_TOKEN" in desp
    assert sendkeys == ["KEY1"]


def test_watchdog_alert_differs_when_status_unknown():
    """查不到状态不能说成"主链挂了",否则是撒谎。"""
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_UNKNOWN)
    try:
        watchdog.run()
    finally:
        restore()

    assert len(calls) == 1
    assert "无法确认" in calls[0][0]


def test_watchdog_fails_when_alert_could_not_be_sent():
    calls, restore = _patch_watchdog(trigger_status.PRIMARY_MISSING,
                                     alert_result=False)
    try:
        code = watchdog.run()
    finally:
        restore()

    assert code == 1
    assert len(calls) == 1
```

- [ ] **Step 2: 跑测试,确认失败**

Run: `PYTHONUTF8=1 python -m pytest test_pipeline.py -k watchdog -v`
Expected: collection error —`ModuleNotFoundError: No module named 'watchdog'`

- [ ] **Step 3: 创建 watchdog.py**

```python
"""看门狗:主链(Cloudflare 定时触发)今天没成功时发微信告警。

判断逻辑在 trigger_status,推送原语在 main;本模块只把两者接起来。
告警文案刻意不依赖集思录数据,这样抓取接口也挂掉时告警仍能发出。
"""

import sys

import main
import trigger_status

MISSING_TITLE = "⚠️ 打新债提醒:早上触发链没生效"
MISSING_BODY = """今天早上 8:57 的定时触发没有成功执行,请依次检查:

1. Cloudflare Worker 的 GITHUB_TOKEN 是否过期(最常见原因)
2. Workers & Pages → bond-reminder-trigger 是否还在、cron 是否还在
3. 修好后重新部署:wrangler deploy

(今天的兜底提醒由 GitHub Actions 的另一条链补推,可能稍晚到达。)"""

UNKNOWN_TITLE = "⚠️ 打新债提醒:无法确认触发链状态"
UNKNOWN_BODY = """查不到今天的主链运行记录(可能是网络问题或 GitHub API 限流),
无法判断早上 8:57 的触发是否成功。

如果今天该有新债却没收到提醒,请检查 Cloudflare Worker 的 GITHUB_TOKEN 和 cron。"""


def build_alert(status):
    """按主链状态返回 (title, desp);主链正常时返回 None。"""
    if status == trigger_status.PRIMARY_MISSING:
        return MISSING_TITLE, MISSING_BODY
    if status == trigger_status.PRIMARY_UNKNOWN:
        return UNKNOWN_TITLE, UNKNOWN_BODY
    return None


def run():
    status = trigger_status.check_primary_today()
    print(f"[INFO] 今天的主链状态: {status}")

    alert = build_alert(status)
    if alert is None:
        print("[INFO] 主链正常,不告警")
        return 0

    title, desp = alert
    config = main.load_config()
    notifications = config.get("notifications")
    sendkeys = notifications.get("sendkeys") if isinstance(notifications, dict) else None

    if main.send_alert(title, desp, sendkeys):
        print("[INFO] 告警已发出")
        return 0
    print("[ERROR] 告警发送失败")
    return 1


if __name__ == "__main__":
    sys.exit(run())
```

- [ ] **Step 4: 跑测试,确认通过**

Run: `PYTHONUTF8=1 python -m pytest test_pipeline.py -k watchdog -v`
Expected: 4 passed

- [ ] **Step 5: 跑全部测试,确认无回归**

Run: `PYTHONUTF8=1 python test_pipeline.py 2>&1 | tail -2`
Expected: `61 passed, 0 failed`(57 + 4)

- [ ] **Step 6: 提交**

```bash
git add watchdog.py test_pipeline.py
git commit -m "feat: 新增触发链看门狗,主链失效时微信告警"
```

---

### Task 4: wrangler.toml cron 修复

**Files:**
- Modify: `wrangler.toml`(整个文件,只有 7 行)

**Interfaces:**
- Consumes: 无
- Produces: Cloudflare Worker 的 cron 表达式(部署后生效需人工 `wrangler deploy`)

- [ ] **Step 1: 改 cron 并写明星期字段差异**

`wrangler.toml` 全文替换为:

```toml
name = "bond-reminder-trigger"
main = "trigger.js"
compatibility_date = "2026-07-17"

[triggers]
# 注意:Cloudflare 的星期字段是 1 = 周日(和 GitHub Actions 相反,那边 1 = 周一)。
# 原来的 "57 0 * * 1-5" 实际是周日~周四,周五从来没触发过。
# 这里改用每天触发绕开歧义:周末没有新债时 main.py 不会推送。
crons = ["57 0 * * *"]
```

- [ ] **Step 2: 校验 TOML 语法**

Run: `python -c "import tomllib; d=tomllib.load(open('wrangler.toml','rb')); print(d['triggers']['crons'])"`
Expected: `['57 0 * * *']`

- [ ] **Step 3: 提交**

```bash
git add wrangler.toml
git commit -m "fix: Cloudflare cron 改为每天触发,绕开星期字段 1=周日 的歧义"
```

---

### Task 5: schedule.yml 兜底去重门禁

**Files:**
- Modify: `.github/workflows/schedule.yml`(整个文件)

**Interfaces:**
- Consumes: `trigger_status.main()` 的退出码(0 = 主链 OK)
- Produces: `steps.primary.outputs.ok`(字符串 `"true"` / `"false"` / 非 schedule 事件下为空)

- [ ] **Step 1: 改 cron 并加门禁**

`.github/workflows/schedule.yml` 全文替换为:

```yaml
name: 打新债提醒

on:
  schedule:
    # UTC 1:23 = 北京时间 9:23,周一到周五(主链 8:57 之后的兜底)。
    # GitHub 的 cron 是标准语义,1 = 周一;Cloudflare 是 1 = 周日,两者相反。
    # 刻意避开整点:官方文档说明整点是负载高峰,schedule 会被大幅延迟
    # (原配置 "0 1" 实测被延迟到 13:29~13:56,最晚到过 20:32)。
    - cron: "23 1 * * 1-5"
  workflow_dispatch:
  repository_dispatch:
    types:
      - check-new-bonds

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

permissions:
  contents: read    # actions/checkout 需要
  actions: read     # trigger_status 读本仓库的运行记录需要

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
        run: pip install -r requirements.txt

      - name: 打印触发信息
        run: |
          echo "触发事件: ${{ github.event_name }}"
          echo "当前时间: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"

      - name: 运行回归测试
        run: python test_pipeline.py

      - name: 判断主链是否已成功
        id: primary
        # 只对 schedule 事件去重。repository_dispatch 就是主链本身:
        # 若在这里查"今天有没有 dispatch 运行",会查到本次运行并判定 ok,
        # 从而把自己要发的提醒跳过 —— 必须靠事件判断挡住。
        if: github.event_name == 'schedule'
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          if python trigger_status.py; then
            echo "ok=true" >> "$GITHUB_OUTPUT"
          else
            echo "ok=false" >> "$GITHUB_OUTPUT"
          fi

      - name: 检查新债并推送
        # 非 schedule 事件该步骤被跳过、输出为空,因此照常推送
        if: steps.primary.outputs.ok != 'true'
        env:
          SENDKEY: ${{ secrets.SENDKEY }}
          DEEPSEEK_API_KEY: ${{ secrets.DEEPSEEK_API_KEY }}
        run: python main.py
```

- [ ] **Step 2: 校验 YAML 语法与门禁表达式**

Run: `python -c "import yaml; d=yaml.safe_load(open('.github/workflows/schedule.yml',encoding='utf-8')); print(d[True]['schedule'])"`
Expected: `[{'cron': '23 1 * * 1-5'}]`

注意必须用 `d[True]` 而不是 `d['on']`:PyYAML 按 YAML 1.1 把裸写的 `on` 解析成布尔 `True`。这是 GitHub workflow 文件的通例(旧版本文件同样如此),不是文件写错了,不要为了迁就校验命令去给 `on:` 加引号。

Run: `grep -n "event_name == 'schedule'" .github/workflows/schedule.yml`
Expected: 命中 1 行(门禁的事件判断存在)

- [ ] **Step 3: 提交**

```bash
git add .github/workflows/schedule.yml
git commit -m "fix: schedule 兜底挪到非整点,并加主链去重门禁"
```

---

### Task 6: watchdog.yml 新建

**Files:**
- Create: `.github/workflows/watchdog.yml`

**Interfaces:**
- Consumes: `watchdog.run()`(`GITHUB_REPOSITORY` 由 runner 自动提供,`GITHUB_TOKEN` 与 `SENDKEY` 由 env 传入)
- Produces: 工作日 09:11 的独立告警链(部署后自动生效,无需人工步骤)

- [ ] **Step 1: 创建 workflow**

```yaml
name: 触发链看门狗

on:
  schedule:
    # UTC 1:11 = 北京时间 9:11,周一到周五(主链 8:57 之后 14 分钟)。
    # 只订阅 schedule 与手动触发:如果它也订阅 repository_dispatch,
    # 查询"今天的主链运行"时会查到本次运行本身,从而永远不告警。
    - cron: "11 1 * * 1-5"
  workflow_dispatch:

env:
  FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true

permissions:
  contents: read    # actions/checkout 需要
  actions: read     # trigger_status 读本仓库的运行记录需要

jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - name: Checkout 代码
        uses: actions/checkout@v4

      - name: 安装 Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: 安装依赖
        run: pip install -r requirements.txt

      # 这里刻意不跑回归测试:告警链自己不能因为测试挂掉而发不出告警。
      - name: 检查主链并告警
        env:
          SENDKEY: ${{ secrets.SENDKEY }}
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: python watchdog.py
```

- [ ] **Step 2: 校验 YAML 语法**

Run: `python -c "import yaml; d=yaml.safe_load(open('.github/workflows/watchdog.yml',encoding='utf-8')); print(d[True]['schedule'], list(d['jobs']))"`
Expected: `[{'cron': '11 1 * * 1-5'}] ['check']`

同 Task 5:裸写的 `on` 被 PyYAML 解析成布尔 `True`,校验要用 `d[True]`。

- [ ] **Step 3: 用真实 API 验证判断逻辑(只读,不推送)**

只跑判断模块,确认它能对真实的历史运行记录得出结论。**不要**在本地跑 `python watchdog.py` —— 它会用 `config.yaml` 里的 SendKey 真的发出一条微信告警。

Run: `PYTHONUTF8=1 GITHUB_REPOSITORY=wangdaguo-tester/new-bond-reminder python trigger_status.py; echo "exit=$?"`
Expected: 打印 `[INFO] 今天的主链状态: missing`(自 2026-08-16 起确实没有 dispatch 运行),`exit=1`

- [ ] **Step 4: 提交**

```bash
git add .github/workflows/watchdog.yml
git commit -m "feat: 新增触发链看门狗 workflow"
```

---

### Task 7: 人工部署与端到端验证

无代码改动,不需要提交。这一节是给执行者的验收清单。

- [ ] **Step 1: 重新生成 GitHub PAT**

到 GitHub → Settings → Developer settings → Personal access tokens 新建(fine-grained 勾 `Contents: Read and write`,或 classic 勾 `repo`)。
**有效期不要再选默认的 30 天** —— 正是它导致主链在 2026-08-16 静默失效。

- [ ] **Step 2: 更新 Worker 密钥并重新部署**

```bash
wrangler login
wrangler secret put GITHUB_TOKEN   # 粘贴新 PAT
wrangler deploy                     # cron 改动必须重新部署才生效
```

Expected: `wrangler deploy` 输出中包含 `schedule: 57 0 * * *`

- [ ] **Step 3: 手动触发主链,确认端到端通**

Dashboard → Workers & Pages → `bond-reminder-trigger` → Triggers → "Trigger Now"。
Expected: GitHub Actions 出现一条 `event: repository_dispatch` 的运行,且"检查新债并推送"步骤被执行(没有被门禁跳过)。

- [ ] **Step 4: 验证告警链能发出来**

在 Actions 里手动 `workflow_dispatch` 运行"触发链看门狗"。
Expected: 因为此前没有 dispatch 运行,应该收到一条微信告警(文案以"⚠️ 打新债提醒"开头)。

- [ ] **Step 5: 验证去重门禁**

手动 `workflow_dispatch` 运行"打新债提醒"时**不会**去重、照常推送 —— 这是有意设计,方便你随时测推送。

schedule 事件的去重只能在真实调度里观察:下一个工作日那次运行(配置目标是 09:23,但送达时间见下方说明,可能晚很多),如果主链(8:57)已经成功,它的"检查新债并推送"步骤应该显示为 **skipped**;若主链没成功,该步骤会执行并补推。

- [ ] **Step 6: 确认下个交易日早上 8:57 收到推送**

Expected: **在 Step 1/Step 2 已完成(PAT + `wrangler deploy`)的前提下**,北京时间 8:57 左右收到新债提醒,且当天没有重复推送。

这条期望的前提是主链被修好。Cloudflare 那条链不修,8:57 的推送不会来;此时只有 GitHub 兜底链那条迟到的提醒 —— 而它的到达时间同样没有保证(见下)。

### 关于送达时间:这是缓解手段,不是承诺

- 把 cron 从整点挪开(`23 1 * * 1-5`)是 GitHub 官方建议的**缓解**手段,不是保证。官方文档只说这样做能"降低被延迟的概率",`schedule` 事件在负载高时仍可能晚到几分钟到**数小时**(本仓库实测:旧配置 `0 1` 被延迟到 13:29~13:56,最晚到过 20:32)。
- 而且**没有任何实测数据能证明 09:23 会比原来的整点更快**:历史记录里所有定时运行都来自旧的 `0 1`(分钟为 0),分钟 23 在本仓库从没被观察到过。因此验收时不要把"09:23 送达"当作预期结果。
- 结论:早上准时收到提醒**取决于 Cloudflare 主链被修好**(Step 1 的 PAT + Step 2 的 `wrangler deploy`)。修好之前只有 GitHub 兜底链那一(可能很迟的)条;看门狗告警虽然配置目标是 09:11,同样受 schedule 延迟影响,只保证"最终会发出",不保证"准点发出"。

---

## 完成标准

- [ ] `PYTHONUTF8=1 python test_pipeline.py` 输出 `71 passed, 0 failed`
- [ ] 6 个提交都在 `master` 上
- [ ] 主链(Cloudflare)`wrangler deploy` 后 cron 为 `57 0 * * *`
- [ ] 主链修好(PAT + `wrangler deploy`)后,工作日早上 8:57 收到推送,且当天只有一条
- [ ] 人为让主链失效时,看门狗能发出微信告警(只验收"发出且文案正确",不验收"准点" —— schedule 可能延迟数小时)
