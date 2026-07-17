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
