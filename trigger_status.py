"""判断今天早上 8:57 的主触发链是否已成功。

主链 = Cloudflare Worker 定时触发出来的 repository_dispatch 运行。
本模块只做判断:不推送、不写状态。四态返回值是为了让调用方诚实区分
"没触发"、"跑了但失败"和"查不到"——兜底推送要偏保守(非 OK 就照推),
而告警既不能把"查不到"说成"主链挂了",也不能把"跑了但失败"说成"没触发"
(后者会给出错误的排查方向:让人去查 PAT,而真正的原因在运行日志里)。
"""

import os
import sys
from collections import namedtuple
from datetime import datetime, timedelta, timezone

import requests

BJT = timezone(timedelta(hours=8))

PRIMARY_OK = "ok"            # 今天已有成功的运行,或主链正在运行
PRIMARY_MISSING = "missing"  # 今天压根没有运行记录
PRIMARY_FAILED = "failed"    # 今天有运行记录,但全部失败
PRIMARY_UNKNOWN = "unknown"  # 查询本身失败,无法判断

_API = "https://api.github.com"
_PER_PAGE = 30

#: 状态 + 今天最近一次运行的链接(没有今天运行或该字段缺失时为 None)
PrimaryVerdict = namedtuple("PrimaryVerdict", ["status", "run_url"])


def _to_bjt(value):
    """把 GitHub 的 ISO 时间串换算成北京时间;解析不了返回 None。"""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return None
    return parsed.replace(tzinfo=timezone.utc).astimezone(BJT)


def evaluate_primary_today(now=None, repo=None, token=None, timeout=30):
    """返回 PrimaryVerdict(status, run_url)。

    判定为 OK:今天的任一 repository_dispatch 运行满足
      - conclusion == "success",或
      - 还没跑完(status != "completed"),避免主链正在跑时兜底抢跑重复推送

    否则:
      - 今天没有任何运行记录 → MISSING(链压根没触发)
      - 有运行记录但全部失败 → FAILED(链跑了,中途出错)

    run_url 取今天最近一次运行的 html_url(API 按时间倒序返回),供告警
    直接链到日志;今天没有运行、或该字段缺失时是 None。

    "今天"按北京时间算:UTC 16:00 已经是北京的次日 00:00。
    """
    repo = repo or os.getenv("GITHUB_REPOSITORY")
    if not repo:
        print("[WARN] 拿不到仓库信息(GITHUB_REPOSITORY 未设置),无法判断主链状态")
        return PrimaryVerdict(PRIMARY_UNKNOWN, None)

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
        return PrimaryVerdict(PRIMARY_UNKNOWN, None)
    except ValueError as e:
        print(f"[WARN] 主链运行记录不是合法 JSON: {e}")
        return PrimaryVerdict(PRIMARY_UNKNOWN, None)

    runs = data.get("workflow_runs") if isinstance(data, dict) else None
    if not isinstance(runs, list):
        print(f"[WARN] 主链运行记录结构异常: {str(data)[:200]}")
        return PrimaryVerdict(PRIMARY_UNKNOWN, None)

    today_runs = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        created = _to_bjt(run.get("created_at"))
        if created is not None and created.date() == today:
            today_runs.append(run)

    if not today_runs:
        return PrimaryVerdict(PRIMARY_MISSING, None)

    run_url = today_runs[0].get("html_url")
    if not isinstance(run_url, str) or not run_url:
        run_url = None

    for run in today_runs:
        if run.get("status") != "completed" or run.get("conclusion") == "success":
            return PrimaryVerdict(PRIMARY_OK, run_url)

    return PrimaryVerdict(PRIMARY_FAILED, run_url)


def check_primary_today(now=None, repo=None, token=None, timeout=30):
    """兼容入口:返回 PRIMARY_OK / PRIMARY_MISSING / PRIMARY_FAILED / PRIMARY_UNKNOWN。

    CLI 门禁(workflow 的 shell 判断)与既有测试依赖这个字符串契约;
    需要连带拿到失败运行的链接时,请改用 evaluate_primary_today。

    注意:MISSING 与 FAILED 都非 0 退出,兜底链因此照推 —— 这是有意的
    (宁可重复一条,不可漏掉一条),区别只体现在告警文案上。
    """
    verdict = evaluate_primary_today(now=now, repo=repo, token=token, timeout=timeout)
    return verdict.status


def main():
    status = check_primary_today()
    print(f"[INFO] 今天的主链状态: {status}")
    return 0 if status == PRIMARY_OK else 1


if __name__ == "__main__":
    sys.exit(main())
