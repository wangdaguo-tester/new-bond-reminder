"""测试多接收人微信推送 — 发送一条测试消息到所有已配置的接收人。"""
import os
import sys
import requests
import yaml


def test_send():
    """读取 config.yaml + SENDKEY 环境变量，发送测试消息。"""
    # 读取 config 中的 sendkeys
    sendkeys = []
    if os.path.exists("config.yaml"):
        with open("config.yaml", "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
        sendkeys = config.get("notifications", {}).get("sendkeys", [])

    # 合并环境变量
    env_key = os.getenv("SENDKEY")
    if env_key and env_key not in sendkeys:
        sendkeys.append(env_key)

    if not sendkeys:
        print("[ERROR] 未配置任何 SendKey，无法测试")
        return 1

    print(f"[INFO] 共 {len(sendkeys)} 个 SendKey，开始测试推送...")

    title = "🧪 打新债提醒 — 测试消息"
    desp = "这是一条测试消息，用于验证多接收人推送功能是否正常。\n\n如果你收到了这条消息，说明配置正确 ✅"

    url_template = "https://sctapi.ftqq.com/{sendkey}.send"
    payload = {"title": title, "desp": desp}
    success_count = 0

    for sk in sendkeys:
        try:
            resp = requests.post(url_template.format(sendkey=sk), data=payload, timeout=30)
            resp.raise_for_status()
            result = resp.json()
            if result.get("code") == 0:
                print(f"[OK] 推送成功 (SendKey: {sk[:12]}...)")
                success_count += 1
            else:
                print(f"[FAIL] 推送失败 (SendKey: {sk[:12]}...): {result}")
        except requests.exceptions.RequestException as e:
            print(f"[FAIL] 推送异常 (SendKey: {sk[:12]}...): {e}")

    print(f"\n[INFO] 测试完成: {success_count}/{len(sendkeys)} 成功")
    return 0 if success_count > 0 else 1


if __name__ == "__main__":
    sys.exit(test_send())
