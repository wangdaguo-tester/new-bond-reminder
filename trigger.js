// 定时触发 GitHub 的新债检查。
//
// 环境变量:
//   GITHUB_OWNER / GITHUB_REPO  见 wrangler.toml 的 [vars]
//   GITHUB_TOKEN                wrangler secret put(需 Contents: Read and write)
//   SERVERCHAN_SENDKEY          wrangler secret put,失败时用来推微信告警
//
// 为什么失败要告警:这条链曾经在 2026-08-17 之后静默失效一个多月 ——
// 只打一行日志,没有任何人看得见。告警是让"链坏了"这件事自己冒出来的唯一方式。

// 只有这三个缺失才该中止:它们任一为空,dispatch 都不可能成功。
// SERVERCHAN_SENDKEY 刻意不在这里 —— 告警是辅助设施,没配它顶多是失败时没人喊,
// 绝不能因为缺一个告警 key 就把真正的提醒一起掐掉。
const REQUIRED = ["GITHUB_OWNER", "GITHUB_REPO", "GITHUB_TOKEN"];

async function alert(env, text) {
  if (!env.SERVERCHAN_SENDKEY) {
    console.error("[alert] 没有 SERVERCHAN_SENDKEY,无法推送告警(仅记录日志)");
    return;
  }

  const body = new URLSearchParams({
    title: "⚠️ 打新债提醒:早上触发失败",
    desp: text,
  });

  try {
    const resp = await fetch(`https://sctapi.ftqq.com/${env.SERVERCHAN_SENDKEY}.send`, {
      method: "POST",
      body,
    });
    if (!resp.ok) {
      console.error(`[alert] Server酱返回 ${resp.status}`);
    }
  } catch (e) {
    console.error(`[alert] 告警发送异常: ${e}`);
  }
}

export default {
  async scheduled(event, env, ctx) {
    // 缺变量要立刻说出来。以前这里会安静地拼出 /repos/undefined/undefined/...,
    // 收到 404 只打一行日志,于是整条链失效了没人知道。
    const missing = REQUIRED.filter((key) => !env[key]);
    if (missing.length > 0) {
      const msg = `缺少环境变量:${missing.join(", ")}。检查 wrangler.toml 的 [vars] 和 wrangler secret list。`;
      console.error(`[ERROR] ${msg}`);
      await alert(env, msg);
      return;
    }

    const url = `https://api.github.com/repos/${env.GITHUB_OWNER}/${env.GITHUB_REPO}/dispatches`;

    try {
      const resp = await fetch(url, {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GITHUB_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "cloudflare-bond-reminder/1.0",
          "X-GitHub-Api-Version": "2022-11-28",
        },
        body: JSON.stringify({ event_type: "check-new-bonds" }),
      });

      if (!resp.ok) {
        const text = await resp.text().catch(() => "");
        const msg =
          `GitHub 触发失败 [${resp.status}] ${text || "(no body)"}\n\n` +
          `401/403 多为 GITHUB_TOKEN 过期或权限不足(需 Contents: Read and write);404 多为仓库名不对。`;
        console.error(`[ERROR] ${msg}`);
        await alert(env, msg);
        return;
      }

      console.log(`[OK] 已触发 ${env.GITHUB_OWNER}/${env.GITHUB_REPO} 的新债检查`);
    } catch (e) {
      const msg = `请求 GitHub 异常: ${e}`;
      console.error(`[ERROR] ${msg}`);
      await alert(env, msg);
    }
  },
};
