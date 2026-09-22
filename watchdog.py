"""看门狗:主链(Cloudflare 定时触发)今天没成功时发微信告警。

判断逻辑在 trigger_status,推送原语在 main;本模块只把两者接起来。
告警文案刻意不依赖集思录数据,这样抓取接口也挂掉时告警仍能发出。
本模块只依赖 requests + yaml(main.py 的 AI 依赖是函数内延迟导入的),
所以 openai 装不上也不会拖垮告警链。
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

FAILED_TITLE = "⚠️ 打新债提醒:早上触发跑了但失败了"
FAILED_BODY = """今天早上 8:57 的定时触发跑起来了,但运行结果是失败 —— 这不是"没触发",
而是链跑起来之后中途出错,所以今天很可能没收到新债提醒。

常见原因(按排查顺序):
1. 集思录抓取失败(接口被反爬拦截或改版,main.py 会直接返回 1)
2. 微信推送失败(Server酱 的 SendKey 失效或额度用尽)
3. AI 分析异常(analysis.py 调用模型报错,流程中断)

请打开下面这次运行的日志,确认失败卡在哪一步,再针对性修复。

(今天的兜底提醒由 GitHub Actions 的另一条链补推,可能稍晚到达。)"""

UNKNOWN_TITLE = "⚠️ 打新债提醒:无法确认触发链状态"
UNKNOWN_BODY = """查不到今天的主链运行记录(可能是网络问题或 GitHub API 限流),
无法判断早上 8:57 的触发是否成功。

如果今天该有新债却没收到提醒,请检查 Cloudflare Worker 的 GITHUB_TOKEN 和 cron。"""


def _append_run_url(body, run_url):
    """有运行链接时追加到正文末尾;没有链接就原样返回,不留半截提示。"""
    if not run_url:
        return body
    return f"{body}\n\n运行记录:{run_url}"


def build_alert(status, run_url=None):
    """按主链状态返回 (title, desp);主链正常时返回 None。

    run_url 只在 FAILED 时用得上:链跑了但失败,点进去看日志才知道卡在哪。
    """
    if status == trigger_status.PRIMARY_MISSING:
        return MISSING_TITLE, MISSING_BODY
    if status == trigger_status.PRIMARY_FAILED:
        return FAILED_TITLE, _append_run_url(FAILED_BODY, run_url)
    if status == trigger_status.PRIMARY_UNKNOWN:
        return UNKNOWN_TITLE, UNKNOWN_BODY
    return None


def run():
    verdict = trigger_status.evaluate_primary_today()
    print(f"[INFO] 今天的主链状态: {verdict.status}")

    alert = build_alert(verdict.status, verdict.run_url)
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
