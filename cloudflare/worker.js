const GITHUB_OWNER = "jiho1101";
const GITHUB_REPO = "cgv_alert";
const WORKFLOW_FILE = "cgv-alert.yml";
const GITHUB_REF = "main";
const COMMAND_VERSION = "5";

const DISCORD_COMMANDS = [
  {
    name: "상태",
    description: "CGV 알림 시스템의 현재 상태를 확인합니다.",
    type: 1,
    contexts: [0],
    integration_types: [0],
  },
  {
    name: "감시목록",
    description: "현재 감시 중인 영화와 주기를 확인합니다.",
    type: 1,
    contexts: [0],
    integration_types: [0],
  },
  {
    name: "즉시확인",
    description: "CGV 감시를 지금 즉시 한 번 실행합니다. (관리자)",
    type: 1,
    contexts: [0],
    integration_types: [0],
  },
  {
    name: "도움말",
    description: "CGV Alert 명령어 사용법을 확인합니다.",
    type: 1,
    contexts: [0],
    integration_types: [0],
  },
];

function jsonResponse(data, status = 200) {
  return new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=UTF-8" },
  });
}

function hexToBytes(hex) {
  if (!hex || hex.length % 2 !== 0) return null;
  const out = new Uint8Array(hex.length / 2);
  for (let i = 0; i < out.length; i += 1) {
    const value = Number.parseInt(hex.slice(i * 2, i * 2 + 2), 16);
    if (Number.isNaN(value)) return null;
    out[i] = value;
  }
  return out;
}

async function verifyDiscordRequest(request, body, publicKeyHex) {
  const signature = request.headers.get("X-Signature-Ed25519");
  const timestamp = request.headers.get("X-Signature-Timestamp");
  const publicKey = hexToBytes(publicKeyHex);
  const signatureBytes = hexToBytes(signature);

  if (!timestamp || !publicKey || !signatureBytes) return false;

  try {
    const key = await crypto.subtle.importKey(
      "raw",
      publicKey,
      { name: "Ed25519" },
      false,
      ["verify"],
    );
    const message = new TextEncoder().encode(timestamp + body);
    return await crypto.subtle.verify(
      { name: "Ed25519" },
      key,
      signatureBytes,
      message,
    );
  } catch (error) {
    console.error("Discord signature verification failed", error);
    return false;
  }
}

async function triggerGitHub(env, source = "cron") {
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
    body: JSON.stringify({ ref: GITHUB_REF, inputs: { source } }),
  });

  if (response.status !== 204) {
    const responseBody = await response.text();
    throw new Error(
      `GitHub workflow dispatch failed: HTTP ${response.status} ${responseBody}`,
    );
  }

  console.log("CGV Alert workflow dispatched successfully");
}

function hasAdministratorPermission(interaction) {
  const raw = interaction.member?.permissions;
  if (!raw) return false;
  try {
    return (BigInt(raw) & 8n) === 8n;
  } catch {
    return false;
  }
}

async function claimCooldown(env, key, seconds) {
  const row = await getState(env, `cooldown:${key}`);
  const previous = Date.parse(row?.value?.at || "");
  const now = Date.now();

  if (Number.isFinite(previous)) {
    const remainingMs = seconds * 1000 - (now - previous);
    if (remainingMs > 0) {
      return Math.ceil(remainingMs / 1000);
    }
  }

  await putState(env, `cooldown:${key}`, {
    at: new Date(now).toISOString(),
  });
  return 0;
}

function ephemeralContent(content) {
  return jsonResponse({
    type: 4,
    data: {
      content,
      flags: 64,
    },
  });
}

async function ensureDb(env) {
  if (!env.DB) throw new Error("D1 binding DB is missing");
  await env.DB.prepare(
    `CREATE TABLE IF NOT EXISTS app_state (
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL,
      updated_at TEXT NOT NULL
    )`,
  ).run();
}

async function getState(env, key) {
  await ensureDb(env);
  const row = await env.DB.prepare(
    "SELECT value, updated_at FROM app_state WHERE key = ?1",
  )
    .bind(key)
    .first();

  if (!row) return null;
  try {
    return { value: JSON.parse(row.value), updated_at: row.updated_at };
  } catch {
    return { value: row.value, updated_at: row.updated_at };
  }
}

async function putState(env, key, value) {
  await ensureDb(env);
  const now = new Date().toISOString();
  await env.DB.prepare(
    `INSERT INTO app_state (key, value, updated_at)
     VALUES (?1, ?2, ?3)
     ON CONFLICT(key) DO UPDATE SET
       value = excluded.value,
       updated_at = excluded.updated_at`,
  )
    .bind(key, JSON.stringify(value), now)
    .run();
}

async function recordCron(env) {
  if (!env.DB) return;
  try {
    await putState(env, "cron", { last_trigger_at: new Date().toISOString() });
  } catch (error) {
    console.error("Failed to save Cron status", error);
  }
}

async function ensureCommandsRegistered(env) {
  const missing = [];
  if (!env.DB) missing.push("DB");
  if (!env.DISCORD_APPLICATION_ID) missing.push("DISCORD_APPLICATION_ID");
  if (!env.DISCORD_BOT_TOKEN) missing.push("DISCORD_BOT_TOKEN");
  if (missing.length) {
    throw new Error(`Discord command setup missing: ${missing.join(", ")}`);
  }

  const current = await getState(env, "discord_commands");
  if (current?.value?.version === COMMAND_VERSION) {
    return {
      ok: true,
      action: "already_registered",
      version: COMMAND_VERSION,
      count: current?.value?.count ?? null,
    };
  }

  const response = await fetch(
    `https://discord.com/api/v10/applications/${env.DISCORD_APPLICATION_ID}/commands`,
    {
      method: "PUT",
      headers: {
        Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify(DISCORD_COMMANDS),
    },
  );

  const body = await response.text();
  if (!response.ok) {
    throw new Error(
      `Discord command registration failed: HTTP ${response.status} ${body.slice(0, 1200)}`,
    );
  }

  let registered = [];
  try {
    registered = JSON.parse(body);
  } catch {
    registered = [];
  }

  await putState(env, "discord_commands", {
    version: COMMAND_VERSION,
    registered_at: new Date().toISOString(),
    count: Array.isArray(registered) ? registered.length : null,
  });
  console.log("Discord slash commands registered");

  return {
    ok: true,
    action: "registered",
    version: COMMAND_VERSION,
    count: Array.isArray(registered) ? registered.length : null,
  };
}

function mergeStatus(previous, incoming) {
  const previousTargets = new Map(
    (previous?.targets || []).map((target) => [target.id, target]),
  );

  let incomingTargets = incoming.targets || [];
  if (incoming.preserve_targets && incomingTargets.length === 0) {
    incomingTargets = previous?.targets || [];
  }

  const mergedTargets = incomingTargets.map((target) => {
    const old = previousTargets.get(target.id) || {};
    return {
      ...old,
      ...target,
      last_success_at: target.last_success_at || old.last_success_at || null,
    };
  });

  return {
    ...previous,
    ...incoming,
    targets: mergedTargets,
    last_cgv_success_at:
      incoming.last_cgv_success_at ||
      previous?.last_cgv_success_at ||
      null,
  };
}

async function saveStatus(env, incoming) {
  const existing = await getState(env, "status");
  const merged = mergeStatus(existing?.value || null, incoming);
  await putState(env, "status", merged);
  return merged;
}

function authorizedStatusUpdate(request, env) {
  const expected = env.STATUS_API_TOKEN;
  if (!expected) return false;
  return request.headers.get("Authorization") === `Bearer ${expected}`;
}

function formatTime(value) {
  if (!value) return "아직 없음";
  try {
    return new Intl.DateTimeFormat("ko-KR", {
      timeZone: "Asia/Seoul",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date(value));
  } catch {
    return String(value);
  }
}

function statusLabel(status) {
  if (status === "error") return "🔴 이상";
  if (status === "warning") return "🟡 일시 오류";
  return "🟢 정상";
}

async function buildSystemStatus(env) {
  const [statusRow, cronRow] = await Promise.all([
    getState(env, "status"),
    getState(env, "cron"),
  ]);

  const status = statusRow?.value;
  const cron = cronRow?.value;

  if (!status) {
    return {
      title: "📡 CGV 알림 시스템 상태",
      description: "아직 GitHub Actions에서 상태 데이터가 전송되지 않았습니다.",
      color: 0xf1c40f,
    };
  }

  const health = status.health_summary || "normal";
  const recentError = status.recent_error || "없음";

  return {
    title: "📡 CGV 알림 시스템 상태",
    description: `**${statusLabel(health)}**`,
    color:
      health === "error" ? 0xe74c3c :
      health === "warning" ? 0xf1c40f :
      0x2ecc71,
    fields: [
      {
        name: "⏱️ Cloudflare Cron",
        value: cron?.last_trigger_at
          ? `마지막 호출: ${formatTime(cron.last_trigger_at)}`
          : "기록 없음",
        inline: false,
      },
      {
        name: "⚙️ 마지막 GitHub 실행",
        value: formatTime(status.last_run_at),
        inline: true,
      },
      {
        name: "✅ 마지막 정상 CGV 조회",
        value: formatTime(status.last_cgv_success_at),
        inline: true,
      },
      {
        name: "🎬 활성 감시",
        value: `${status.active_count ?? status.targets?.length ?? 0}개`,
        inline: true,
      },
      {
        name: "최근 오류",
        value: String(recentError).slice(0, 1000),
        inline: false,
      },
    ],
    footer: {
      text: "CGV Alert · Cloudflare + GitHub Actions",
    },
  };
}

async function buildWatchList(env) {
  const statusRow = await getState(env, "status");
  const status = statusRow?.value;
  const targets = status?.targets || [];

  if (!targets.length) {
    return [{
      title: "🎬 현재 감시 목록",
      description: "현재 활성화된 감시 대상이 없습니다.",
      color: 0x95a5a6,
    }];
  }

  const embeds = [];
  for (let i = 0; i < targets.length; i += 10) {
    const page = targets.slice(i, i + 10);
    embeds.push({
      title:
        targets.length > 10
          ? `🎬 현재 감시 목록 · ${i / 10 + 1}/${Math.ceil(targets.length / 10)}`
          : "🎬 현재 감시 목록",
      color: 0x3498db,
      fields: page.map((target) => ({
        name: `${statusLabel(target.health_status)} · ${target.label}`,
        value: [
          `**ID** \`${target.id}\``,
          `**극장** ${target.theater_name}`,
          `**날짜** ${target.date_text}`,
          `**현재 주기** ${target.interval_text}`,
          `**마지막 정상 확인** ${formatTime(target.last_success_at)}`,
        ].join("\n"),
        inline: false,
      })),
      footer: {
        text: `활성 감시 ${targets.length}개 · 지난 대상은 자동 정리`,
      },
    });
  }
  return embeds;
}

function buildHelpEmbed() {
  return {
    title: "🧭 CGV Alert 도움말",
    color: 0x5865f2,
    description: [
      "**/상태** — 시스템, Cron, GitHub, CGV 조회 상태",
      "**/감시목록** — 현재 영화/날짜/주기/ID 확인",
      "**/즉시확인** — 지금 CGV 확인 1회 실행 (관리자, 60초 쿨다운)",
    ].join("\n"),
    footer: {
      text: "관리 명령은 서버 Administrator 권한이 있는 사용자만 실행 가능",
    },
  };
}

async function handleDiscordInteraction(request, env) {
  if (!env.DISCORD_PUBLIC_KEY) {
    return new Response("DISCORD_PUBLIC_KEY secret is missing", { status: 500 });
  }

  const body = await request.text();
  const valid = await verifyDiscordRequest(
    request,
    body,
    env.DISCORD_PUBLIC_KEY,
  );

  if (!valid) {
    return new Response("invalid request signature", { status: 401 });
  }

  const interaction = JSON.parse(body);

  if (interaction.type === 1) {
    return jsonResponse({ type: 1 });
  }

  if (interaction.type !== 2) {
    return jsonResponse({
      type: 4,
      data: {
        content: "지원하지 않는 요청입니다.",
        flags: 64,
      },
    });
  }

  const command = interaction.data?.name;

  try {
    if (command === "상태") {
      const embed = await buildSystemStatus(env);
      return jsonResponse({
        type: 4,
        data: {
          embeds: [embed],
          flags: 64,
        },
      });
    }

    if (command === "감시목록") {
      const embeds = await buildWatchList(env);
      return jsonResponse({
        type: 4,
        data: {
          embeds,
          flags: 64,
        },
      });
    }

    if (command === "도움말") {
      return jsonResponse({
        type: 4,
        data: {
          embeds: [buildHelpEmbed()],
          flags: 64,
        },
      });
    }

    if (command === "즉시확인") {
      if (!hasAdministratorPermission(interaction)) {
        return ephemeralContent("이 명령어는 서버 관리자만 사용할 수 있습니다.");
      }

      const remaining = await claimCooldown(env, "manual-check", 60);
      if (remaining > 0) {
        return ephemeralContent(
          `이미 즉시 확인을 요청했습니다. ${remaining}초 뒤 다시 사용할 수 있습니다.`,
        );
      }

      await triggerGitHub(env, "discord_manual");
      return ephemeralContent(
        "🔎 즉시 확인을 요청했습니다. 완료되면 Discord로 결과를 다시 알려드립니다.",
      );
    }

    return ephemeralContent("알 수 없는 명령어입니다.");
  } catch (error) {
    console.error("Discord command failed", error);
    return jsonResponse({
      type: 4,
      data: {
        content: "상태를 불러오는 중 오류가 발생했습니다.",
        flags: 64,
      },
    });
  }
}

export default {
  async scheduled(controller, env, ctx) {
    ctx.waitUntil(
      (async () => {
        await recordCron(env);

        try {
          await ensureCommandsRegistered(env);
        } catch (error) {
          console.error("Discord command setup failed", error);
        }

        await triggerGitHub(env);
      })(),
    );
  },

  async fetch(request, env) {
    const url = new URL(request.url);

    if (
      request.method === "POST" &&
      url.pathname === "/discord/interactions"
    ) {
      return handleDiscordInteraction(request, env);
    }

    if (
      request.method === "POST" &&
      url.pathname === "/api/status"
    ) {
      if (!authorizedStatusUpdate(request, env)) {
        return new Response("Unauthorized", { status: 401 });
      }
      if (!env.DB) {
        return new Response("D1 binding DB is missing", { status: 503 });
      }

      try {
        const incoming = await request.json();
        await saveStatus(env, incoming);
        return jsonResponse({ ok: true });
      } catch (error) {
        console.error("Status update failed", error);
        return jsonResponse({ ok: false, error: String(error) }, 500);
      }
    }

    if (request.method === "GET" && url.pathname === "/health") {
      let discordCommandSetup;
      let discordCommandState = null;

      try {
        discordCommandSetup = await ensureCommandsRegistered(env);
      } catch (error) {
        discordCommandSetup = {
          ok: false,
          error: String(error),
        };
      }

      try {
        discordCommandState =
          (await getState(env, "discord_commands"))?.value || null;
      } catch {
        discordCommandState = null;
      }

      return jsonResponse({
        ok: true,
        service: "cgv-alert-trigger",
        version: COMMAND_VERSION,
        discord_commands: discordCommandSetup,
        discord_command_state: discordCommandState,
        now: new Date().toISOString(),
      });
    }

    return new Response(
      "CGV Alert trigger is running. Cron, Discord commands, and status API are available.",
      {
        status: 200,
        headers: { "content-type": "text/plain; charset=UTF-8" },
      },
    );
  },
};
