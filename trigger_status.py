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
