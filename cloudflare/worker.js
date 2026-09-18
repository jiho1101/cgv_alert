const GITHUB_OWNER = "jiho1101";
const GITHUB_REPO = "cgv_alert";
const WORKFLOW_FILE = "cgv-alert.yml";
const GITHUB_REF = "main";

async function triggerGitHub(env) {
  if (!env.GITHUB_TOKEN) {
    throw new Error("GITHUB_TOKEN secret is missing");
  }

  const url =
    `https://api.github.com/repos/${GITHUB_OWNER}/${GITHUB_REPO}/actions/workflows/${WORKFLOW_FILE}/dispatches`;

  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/vnd.github+json",
      Authorization: `Bearer ${env.GITHUB_TOKEN}`,
      "X-GitHub-Api-Version": "2022-11-28",
      "User-Agent": "cgv-alert-cloudflare-trigger",
    },
    body: JSON.stringify({ ref: GITHUB_REF }),
  });

  if (response.status !== 204) {
    const body = await response.text();
    throw new Error(
      `GitHub workflow dispatch failed: HTTP ${response.status} ${body}`
    );
  }

  console.log("CGV Alert workflow dispatched successfully");
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(triggerGitHub(env));
  },

  async fetch() {
    return new Response(
      "CGV Alert trigger is running. Scheduled checks are handled by Cloudflare Cron.",
      {
        status: 200,
        headers: { "content-type": "text/plain; charset=UTF-8" },
      }
    );
  },
};
