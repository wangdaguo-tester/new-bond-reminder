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
