const GITHUB_OWNER = "jiho1101";
const GITHUB_REPO = "cgv_alert";
const WORKFLOW_FILE = "cgv-alert.yml";
const GITHUB_REF = "main";
const COMMAND_VERSION = "18";
const MONITORING_STATS_KEY = "monitoring_stats_24h_v2";
const EMERGENCY_STATE_KEY = "emergency_fallback_v1";
const EMERGENCY_STALE_MS = 8 * 60 * 1000;
const GITHUB_DISPATCH_PROBE_KEY = "github_dispatch_probe_v1";
const DISPATCH_ACK_GRACE_MS = 4 * 60 * 1000;

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
    description: "주기를 무시하고 활성 감시 대상을 지금 모두 확인합니다. (관리자)",
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

async function clearGlobalDiscordCommands(env) {
  const response = await fetch(
    `https://discord.com/api/v10/applications/${env.DISCORD_APPLICATION_ID}/commands`,
    {
      method: "PUT",
      headers: {
        Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
        "Content-Type": "application/json",
      },
      body: JSON.stringify([]),
    },
  );

  const body = await response.text();
  if (!response.ok) {
    throw new Error(
      `Discord global command cleanup failed: HTTP ${response.status} ${body.slice(0, 1200)}`,
    );
  }

  await putState(env, "discord_commands_global_cleared", {
    version: COMMAND_VERSION,
    cleared_at: new Date().toISOString(),
  });
  console.log("Discord global slash commands cleared");
}

async function registerDiscordCommands(env, guildId = null) {
  const endpoint = guildId
    ? `https://discord.com/api/v10/applications/${env.DISCORD_APPLICATION_ID}/guilds/${guildId}/commands`
    : `https://discord.com/api/v10/applications/${env.DISCORD_APPLICATION_ID}/commands`;

  const response = await fetch(endpoint, {
    method: "PUT",
    headers: {
      Authorization: `Bot ${env.DISCORD_BOT_TOKEN}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(DISCORD_COMMANDS),
  });

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

  const stateKey = guildId
    ? `discord_commands:guild:${guildId}`
    : "discord_commands";

  await putState(env, stateKey, {
    version: COMMAND_VERSION,
    scope: guildId ? "guild" : "global",
    guild_id: guildId || null,
    registered_at: new Date().toISOString(),
    count: Array.isArray(registered) ? registered.length : null,
  });

  console.log(
    guildId
      ? `Discord guild slash commands registered for ${guildId}`
      : "Discord global slash commands registered",
  );

  return {
    ok: true,
    action: "registered",
    scope: guildId ? "guild" : "global",
    version: COMMAND_VERSION,
    count: Array.isArray(registered) ? registered.length : null,
  };
}

async function ensureCommandsRegistered(env) {
  const missing = [];
  if (!env.DB) missing.push("DB");
  if (!env.DISCORD_APPLICATION_ID) missing.push("DISCORD_APPLICATION_ID");
  if (!env.DISCORD_BOT_TOKEN) missing.push("DISCORD_BOT_TOKEN");
  if (missing.length) {
    throw new Error(`Discord command setup missing: ${missing.join(", ")}`);
  }

  const guildRow = await getState(env, "discord_guild");
  const guildId = guildRow?.value?.guild_id || null;
  const stateKey = guildId
    ? `discord_commands:guild:${guildId}`
    : "discord_commands";
  const current = await getState(env, stateKey);

  if (current?.value?.version === COMMAND_VERSION) {
    return {
      ok: true,
      action: "already_registered",
      scope: guildId ? "guild" : "global",
      version: COMMAND_VERSION,
      count: current?.value?.count ?? null,
    };
  }

  const result = await registerDiscordCommands(env, guildId);

  if (guildId) {
    const cleared = await getState(env, "discord_commands_global_cleared");
    if (cleared?.value?.version !== COMMAND_VERSION) {
      await clearGlobalDiscordCommands(env);
    }
  }

  return result;
}

async function rememberGuildAndRegister(env, interaction) {
  const guildId = interaction?.guild_id;
  if (!guildId) return;

  try {
    const existing = await getState(env, "discord_guild");
    if (existing?.value?.guild_id !== guildId) {
      await putState(env, "discord_guild", {
        guild_id: guildId,
        learned_at: new Date().toISOString(),
      });
    }

    const channelId = String(interaction?.channel_id || "").trim();
    if (channelId) {
      await putState(env, "discord_alert_channel", {
        guild_id: guildId,
        channel_id: channelId,
        learned_at: new Date().toISOString(),
      });
    }

    const current = await getState(
      env,
      `discord_commands:guild:${guildId}`,
    );
    if (current?.value?.version !== COMMAND_VERSION) {
      await registerDiscordCommands(env, guildId);
    }

    const cleared = await getState(env, "discord_commands_global_cleared");
    if (cleared?.value?.version !== COMMAND_VERSION) {
      await clearGlobalDiscordCommands(env);
    }
  } catch (error) {
    console.error("Failed to learn/register Discord guild commands", error);
  }
}


function classifyMonitoringSample(status) {
  const modes = (status?.targets || [])
    .map((target) => String(target?.detection_mode || ""))
    .filter(Boolean);

  if (modes.includes("failed")) return "failed";

  const fallbackModes = new Set([
    "cgv_official",
    "text_fallback",
    "cloudflare_fallback",
    "api_protection",
    "source_unhealthy",
    "fallback",
  ]);
  if (modes.some((mode) => fallbackModes.has(mode))) return "fallback";

  const structuredModes = new Set([
    "public_primary",
    "empty_unconfirmed",
    "structured",
  ]);
  if (modes.some((mode) => structuredModes.has(mode))) return "structured";

  // checker 자체가 실패해서 target 결과를 만들지 못한 실행도 실패로 센다.
  if (status?.health_summary === "error" && status?.preserve_targets) {
    return "failed";
  }
  return null;
}

function summarizeMonitoringSamples(samples) {
  const now = Date.now();
  const cutoff = now - 24 * 60 * 60 * 1000;
  const recent = (Array.isArray(samples) ? samples : []).filter((sample) => {
    const at = Date.parse(sample?.at || "");
    return Number.isFinite(at) && at >= cutoff && at <= now + 5 * 60 * 1000;
  });

  const counts = {
    structured: 0,
    fallback: 0,
    failed: 0,
  };
  for (const sample of recent) {
    if (sample?.mode in counts) counts[sample.mode] += 1;
  }

  const total = counts.structured + counts.fallback + counts.failed;
  const monitoringSuccess = counts.structured + counts.fallback;
  return {
    total,
    structured: counts.structured,
    fallback: counts.fallback,
    failed: counts.failed,
    monitoring_success_rate:
      total > 0 ? Math.round((monitoringSuccess / total) * 1000) / 10 : null,
    structured_rate:
      total > 0 ? Math.round((counts.structured / total) * 1000) / 10 : null,
  };
}

async function recordMonitoringSample(env, incoming) {
  if (!env.DB) return null;

  const mode = classifyMonitoringSample(incoming);
  if (!mode) return null;

  const rawAt = String(incoming?.last_run_at || new Date().toISOString());
  const parsedAt = Date.parse(rawAt);
  const at = Number.isFinite(parsedAt)
    ? new Date(parsedAt).toISOString()
    : new Date().toISOString();

  const key = MONITORING_STATS_KEY;
  const row = await getState(env, key);
  const previous = Array.isArray(row?.value?.samples)
    ? row.value.samples
    : [];

  const cutoff = Date.now() - 24 * 60 * 60 * 1000;
  const samples = previous.filter((sample) => {
    const ts = Date.parse(sample?.at || "");
    return Number.isFinite(ts) && ts >= cutoff && sample?.at !== at;
  });
  samples.push({ at, mode });

  // 5분 주기 기준 하루 최대 288회이므로 여유 있게 제한한다.
  const bounded = samples
    .sort((a, b) => Date.parse(a.at) - Date.parse(b.at))
    .slice(-400);

  const summary = summarizeMonitoringSamples(bounded);
  await putState(env, key, {
    samples: bounded,
    summary,
    updated_at: new Date().toISOString(),
  });
  return summary;
}

async function getMonitoringStats(env) {
  if (!env.DB) return summarizeMonitoringSamples([]);
  try {
    const row = await getState(env, MONITORING_STATS_KEY);
    return summarizeMonitoringSamples(row?.value?.samples || []);
  } catch {
    return summarizeMonitoringSamples([]);
  }
}

function monitoringStatsText(stats) {
  if (!stats?.total) {
    return "새 감시체계 기준 집계를 시작했습니다. 아직 조회 표본이 없습니다.";
  }
  const success =
    stats.monitoring_success_rate == null
      ? "-"
      : `${stats.monitoring_success_rate}%`;
  const structured =
    stats.structured_rate == null
      ? "-"
      : `${stats.structured_rate}%`;
  return [
    `새 체계 조회 ${stats.total}회 · 구조화 ${stats.structured} · 보조 ${stats.fallback} · 실패 ${stats.failed}`,
    `감시 성공률 ${success} · 구조화 비율 ${structured}`,
  ].join("\n");
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
      last_primary_success_at:
        target.last_primary_success_at ||
        old.last_primary_success_at ||
        target.last_structured_success_at ||
        old.last_structured_success_at ||
        null,
      last_structured_success_at:
        target.last_structured_success_at ||
        old.last_structured_success_at ||
        target.last_primary_success_at ||
        old.last_primary_success_at ||
        null,
      priority_session_keys: Array.isArray(target.priority_session_keys)
        ? target.priority_session_keys
        : (Array.isArray(old.priority_session_keys)
          ? old.priority_session_keys
          : []),
      priority_session_snapshot_at:
        target.priority_session_snapshot_at ||
        old.priority_session_snapshot_at ||
        null,
      pinned_movie_code:
        target.pinned_movie_code ||
        old.pinned_movie_code ||
        null,
    };
  });

  return {
    ...previous,
    ...incoming,
    targets: mergedTargets,
    last_primary_success_at:
      incoming.last_primary_success_at ||
      previous?.last_primary_success_at ||
      incoming.last_cgv_success_at ||
      previous?.last_cgv_success_at ||
      null,
    last_cgv_success_at:
      incoming.last_cgv_success_at ||
      previous?.last_cgv_success_at ||
      incoming.last_primary_success_at ||
      previous?.last_primary_success_at ||
      null,
    last_monitoring_success_at:
      incoming.last_monitoring_success_at ||
      previous?.last_monitoring_success_at ||
      incoming.last_primary_success_at ||
      previous?.last_primary_success_at ||
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


function decodeHtmlEntities(text) {
  return String(text || "")
    .replace(/&nbsp;/gi, " ")
    .replace(/&amp;/gi, "&")
    .replace(/&lt;/gi, "<")
    .replace(/&gt;/gi, ">")
    .replace(/&quot;/gi, '"')
    .replace(/&#39;/gi, "'");
}

function renderedHtmlToText(html) {
  return decodeHtmlEntities(
    String(html || "")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, " ")
      .replace(/<style\b[^>]*>[\s\S]*?<\/style>/gi, " ")
      .replace(/<noscript\b[^>]*>[\s\S]*?<\/noscript>/gi, " ")
      .replace(/<br\s*\/?>/gi, "\n")
      .replace(/<\/(div|p|li|section|article|h[1-6]|tr)>/gi, "\n")
      .replace(/<[^>]+>/g, " ")
  )
    .replace(/[ \t]+/g, " ")
    .replace(/\n\s*\n+/g, "\n")
    .trim();
}

async function browserBudgetState(env) {
  const day = new Date().toISOString().slice(0, 10);
  const key = `browser_budget:${day}`;
  const row = await getState(env, key);
  const usedMs = Number(row?.value?.used_ms || 0);
  return { key, usedMs: Number.isFinite(usedMs) ? usedMs : 0 };
}


function browserBudgetText(budget) {
  const safeLimitMs = 8 * 60 * 1000;
  const usedMs = Math.max(0, Number(budget?.usedMs || 0));
  const usedMin = Math.round((usedMs / 60000) * 10) / 10;
  const remainingMin =
    Math.round((Math.max(0, safeLimitMs - usedMs) / 60000) * 10) / 10;
  return `${usedMin}분 사용 · 안전 한도까지 ${remainingMin}분 남음`;
}

async function handleBrowserCheck(request, env) {
  if (!authorizedStatusUpdate(request, env)) {
    return new Response("Unauthorized", { status: 401 });
  }
  if (!env.BROWSER) {
    return jsonResponse(
      { ok: false, error: "Browser Run binding is missing" },
      503,
    );
  }

  let input;
  try {
    input = await request.json();
  } catch {
    return jsonResponse({ ok: false, error: "Invalid JSON" }, 400);
  }

  const siteNo = String(input?.site_no || "").trim();
  const siteName = String(input?.site_name || "").trim();
  const playYmd = String(input?.play_ymd || "").trim();

  if (!/^\d{4}$/.test(siteNo) || !/^\d{8}$/.test(playYmd)) {
    return jsonResponse({ ok: false, error: "Invalid CGV parameters" }, 400);
  }
  if (!siteName || siteName.length > 60) {
    return jsonResponse({ ok: false, error: "Invalid CGV site name" }, 400);
  }

  // Workers Free의 10분/일 한도에 닿기 전에 8분에서 안전 차단한다.
  const budget = await browserBudgetState(env);
  const SAFE_DAILY_BROWSER_MS = 8 * 60 * 1000;
  if (budget.usedMs >= SAFE_DAILY_BROWSER_MS) {
    return jsonResponse(
      {
        ok: false,
        error: "Daily Browser Run safety budget reached",
        used_ms: budget.usedMs,
      },
      429,
    );
  }

  const cgvUrl =
    "https://cgv.co.kr/cnm/movieBook/cinema" +
    `?siteNo=${encodeURIComponent(siteNo)}` +
    `&siteNm=${encodeURIComponent(siteName)}` +
    `&scnYmd=${encodeURIComponent(playYmd)}`;

  let rendered;
  try {
    rendered = await env.BROWSER.quickAction("content", {
      url: cgvUrl,
      gotoOptions: {
        waitUntil: "networkidle2",
        timeout: 15000,
      },
      rejectResourceTypes: ["image", "font", "media"],
    });
  } catch (error) {
    return jsonResponse(
      {
        ok: false,
        error: `Browser Run failed: ${String(error).slice(0, 300)}`,
      },
      502,
    );
  }

  const browserMs = Number(
    rendered?.headers?.get?.("X-Browser-Ms-Used") || 0,
  );
  const html = await rendered.text();
  if (browserMs > 0 && Number.isFinite(browserMs)) {
    await putState(env, budget.key, {
      used_ms: budget.usedMs + browserMs,
      last_used_ms: browserMs,
      updated_at: new Date().toISOString(),
    });
  }

  const text = renderedHtmlToText(html);
  return jsonResponse({
    ok: true,
    source: "cloudflare_browser_run",
    browser_ms: Number.isFinite(browserMs) ? browserMs : 0,
    text: text.slice(0, 120000),
  });
}


function emergencyNormalize(text) {
  return String(text || "").toLocaleLowerCase("ko-KR")
    .replace(/[^0-9a-z가-힣]/gi, "");
}

function emergencyInt(value, fallback = 0) {
  const parsed = Number.parseInt(String(value ?? ""), 10);
  return Number.isFinite(parsed) ? parsed : fallback;
}

function emergencyTargets(status) {
  const out = [];
  for (const raw of status?.targets || []) {
    const theaterCode = String(raw?.theater_code || "");
    const dates = Array.isArray(raw?.priority_dates) ? raw.priority_dates : [];
    const aliases = Array.isArray(raw?.movie_aliases)
      ? raw.movie_aliases
      : [raw?.label || ""];
    if (!/^\d{4}$/.test(theaterCode) || !dates.length) continue;
    for (const value of dates) {
      const playDate = String(value || "");
      if (!/^\d{8}$/.test(playDate)) continue;
      out.push({
        id: String(raw.id || ""),
        label: String(raw.label || raw.id || "영화"),
        theater_code: theaterCode,
        theater_name: String(raw.theater_name || "CGV"),
        play_date: playDate,
        aliases: aliases.map(emergencyNormalize).filter(Boolean),
        pinned_movie_code: String(raw.pinned_movie_code || ""),
        require_sale_open: Boolean(raw.require_sale_open),
        min_remaining_seats: emergencyInt(raw.min_remaining_seats, 0),
        screen_keywords: Array.isArray(raw.screen_keywords)
          ? raw.screen_keywords
          : [],
        priority_session_keys: Array.isArray(raw.priority_session_keys)
          ? raw.priority_session_keys.map(String)
          : null,
      });
    }
  }
  return out.filter((target) => target.id && target.aliases.length);
}

function emergencyStart(value) {
  let raw = String(value || "").replace(/:/g, "");
  if (!/^\d{3,4}$/.test(raw)) return null;
  raw = raw.padStart(4, "0");
  const hour = emergencyInt(raw.slice(0, 2), -1);
  const minute = emergencyInt(raw.slice(2), -1);
  if (hour < 0 || hour > 29 || minute < 0 || minute > 59) return null;
  return String(hour).padStart(2, "0") + ":" + String(minute).padStart(2, "0");
}

async function emergencyFetchPage(theaterCode, playDate) {
  const url = new URL("https://mcp.aka.page/api/cgv/timetable");
  url.searchParams.set("playDate", playDate);
  url.searchParams.set("theaterCode", theaterCode);
  url.searchParams.set("limit", "200");
  let response;
  let payload = null;
  try {
    response = await fetch(url.toString(), {
      headers: { Accept: "application/json" },
      signal: AbortSignal.timeout(15000),
    });
    try {
      payload = await response.json();
    } catch {
      payload = null;
    }
  } catch (error) {
    return { ok: false, status: null, rows: [], error: String(error) };
  }

  let rawRows = null;
  if (payload && typeof payload === "object" && !Array.isArray(payload)) {
    if (Array.isArray(payload.data)) rawRows = payload.data;
    else if (payload.data && typeof payload.data === "object") {
      for (const key of ["timetable", "items", "results"]) {
        if (Array.isArray(payload.data[key])) {
          rawRows = payload.data[key];
          break;
        }
      }
    }
    if (rawRows === null) {
      for (const key of ["timetable", "items", "results"]) {
        if (Array.isArray(payload[key])) {
          rawRows = payload[key];
          break;
        }
      }
    }
  }
  if (response.status !== 200 || rawRows === null) {
    return {
      ok: false,
      status: response.status,
      rows: [],
      error: "invalid response/status",
    };
  }

  const rows = rawRows.map((row) => ({
    movie_code: String(row?.movieCode || row?.movNo || ""),
    movie_name: String(row?.movieName || row?.movNm || row?.prodNm || ""),
    theater_code: String(row?.theaterCode || row?.siteNo || ""),
    play_date: String(row?.playDate || row?.scnYmd || ""),
    start_time: String(row?.startTime || row?.scnsrtTm || ""),
    schedule_id: String(row?.scheduleId || row?.scnSseq || ""),
    total_seats: row?.totalSeats ?? row?.stcnt,
    remaining_seats:
      row?.remainingSeats ?? row?.frSeatCnt ?? row?.frtmpSeatCnt,
  }));
  const identities = new Set();
  for (const row of rows) {
    if (
      !row.movie_code || !row.movie_name || !row.theater_code ||
      !row.play_date || !row.start_time || !row.schedule_id ||
      row.theater_code !== theaterCode || row.play_date !== playDate
    ) {
      return {
        ok: false,
        status: response.status,
        rows: [],
        error: "invalid structure",
      };
    }
    const identity = [
      row.movie_code, row.theater_code, row.play_date,
      row.schedule_id, row.start_time,
    ].join("|");
    if (identities.has(identity)) {
      return {
        ok: false,
        status: response.status,
        rows: [],
        error: "identity collision",
      };
    }
    identities.add(identity);
  }
  return { ok: true, status: response.status, rows, error: null };
}

function emergencySessions(rows, target) {
  if ((target.screen_keywords || []).length) return [];
  const aliases = new Set(target.aliases || []);
  const pinned = String(target.pinned_movie_code || "");
  const sessions = [];
  const seen = new Set();
  for (const row of rows) {
    if (
      row.theater_code !== target.theater_code ||
      row.play_date !== target.play_date
    ) continue;
    const movieName = emergencyNormalize(row.movie_name);
    if (pinned ? row.movie_code !== pinned : !aliases.has(movieName)) continue;
    const start = emergencyStart(row.start_time);
    if (!start) continue;
    const total = emergencyInt(row.total_seats, -1);
    const remaining = emergencyInt(row.remaining_seats, -1);
    if (
      target.require_sale_open &&
      !(total > 0 && remaining > 0 && remaining <= total)
    ) continue;
    if (
      target.min_remaining_seats > 0 &&
      remaining < target.min_remaining_seats
    ) continue;
    const key = target.theater_code + "|" + target.play_date + "|" +
      target.id + "|public|" + row.movie_code + "|" +
      row.schedule_id + "|" + start;
    if (seen.has(key)) continue;
    seen.add(key);
    sessions.push({ key, start_time: start });
  }
  sessions.sort((a, b) => a.start_time.localeCompare(b.start_time));
  return sessions;
}

async function emergencyDiscordChannel(env) {
  const remembered = await getState(env, "discord_alert_channel");
  const rememberedId = String(remembered?.value?.channel_id || "");
  if (rememberedId) return rememberedId;
  const guild = await getState(env, "discord_guild");
  const guildId = String(guild?.value?.guild_id || "");
  if (!guildId || !env.DISCORD_BOT_TOKEN) return null;
  try {
    const response = await fetch(
      "https://discord.com/api/v10/guilds/" + guildId,
      { headers: { Authorization: "Bot " + env.DISCORD_BOT_TOKEN } },
    );
    if (!response.ok) return null;
    const data = await response.json();
    const channelId = String(data?.system_channel_id || "");
    if (channelId) {
      await putState(env, "discord_alert_channel", {
        guild_id: guildId,
        channel_id: channelId,
        source: "system_channel",
        learned_at: new Date().toISOString(),
      });
      return channelId;
    }
  } catch (error) {
    console.error("Emergency channel lookup failed", error);
  }
  return null;
}

async function emergencySendDiscord(env, items) {
  const embeds = items.slice(0, 10).map((item) => ({
    title: "🚨 CGV 비상 감시 · 예매 오픈 감지",
    description: "**" + item.target.label + "**",
    color: 0xe67e22,
    fields: [
      { name: "극장", value: item.target.theater_name, inline: true },
      { name: "상영일", value: item.target.play_date, inline: true },
      {
        name: "새 회차",
        value: item.sessions.map((row) => "• " + row.start_time).join("\n"),
        inline: false,
      },
      {
        name: "감지 경로",
        value: "GitHub Actions 실행 공백 중 Cloudflare 직접 구조화 감시",
        inline: false,
      },
    ],
    footer: { text: "좌석 수 변화는 신규 회차로 처리하지 않습니다." },
  }));
  if (!embeds.length) return false;

  if (env.DISCORD_WEBHOOK_URL) {
    const webhook = await fetch(env.DISCORD_WEBHOOK_URL, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ embeds }),
    });
    if (webhook.status === 200 || webhook.status === 204) return true;
  }

  const channelId = await emergencyDiscordChannel(env);
  if (!channelId || !env.DISCORD_BOT_TOKEN) return false;
  const response = await fetch(
    "https://discord.com/api/v10/channels/" + channelId + "/messages",
    {
      method: "POST",
      headers: {
        Authorization: "Bot " + env.DISCORD_BOT_TOKEN,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ embeds }),
    },
  );
  return response.ok;
}

async function performEmergencyCheck(env, dryRun = false) {
  const statusRow = await getState(env, "status");
  const status = statusRow?.value;
  const targets = emergencyTargets(status);
  if (!status || !targets.length) {
    return { ok: false, dry_run: dryRun, error: "emergency config unavailable" };
  }
  const stateRow = await getState(env, EMERGENCY_STATE_KEY);
  const previous = stateRow?.value || {};
  const baseline = structuredClone(previous.baseline || {});
  const notified = structuredClone(previous.notified || {});
  const groups = new Map();
  for (const target of targets) {
    const pageKey = target.theater_code + "|" + target.play_date;
    if (!groups.has(pageKey)) groups.set(pageKey, []);
    groups.get(pageKey).push(target);
  }

  const pageResults = [];
  const targetResults = [];
  const newItems = [];
  const current = new Map();
  let structuralValid = true;
  let firstError = null;

  for (const [pageKey, pageTargets] of groups.entries()) {
    const [theaterCode, playDate] = pageKey.split("|");
    const page = await emergencyFetchPage(theaterCode, playDate);
    pageResults.push({
      theater_code: theaterCode,
      play_date: playDate,
      status: page.status,
      structural_valid: page.ok,
      rows: page.rows.length,
      error: page.error,
    });
    if (!page.ok) {
      structuralValid = false;
      firstError = firstError || page.error;
      continue;
    }

    for (const target of pageTargets) {
      const sessions = emergencySessions(page.rows, target);
      current.set(target.id, sessions);
      const base = new Set([
        ...(baseline[target.id] || []),
        ...(notified[target.id] || []),
      ]);
      if (Array.isArray(target.priority_session_keys)) {
        for (const key of target.priority_session_keys) base.add(key);
      } else if (base.size === 0) {
        for (const session of sessions) base.add(session.key);
      }
      const fresh = sessions.filter((session) => !base.has(session.key));
      if (!fresh.length) {
        for (const session of sessions) base.add(session.key);
        baseline[target.id] = [...base].slice(-1000);
      }
      targetResults.push({
        id: target.id,
        sessions: sessions.length,
        new_sessions: fresh.length,
      });
      if (fresh.length) newItems.push({ target, sessions: fresh });
    }
  }

  if (dryRun) {
    return {
      ok: structuralValid,
      dry_run: true,
      structural_valid: structuralValid,
      source_pages: pageResults,
      target_results: targetResults,
      would_notify_count: newItems.length,
      error: firstError,
    };
  }

  let alertSent = false;
  if (newItems.length) alertSent = await emergencySendDiscord(env, newItems);
  if (alertSent) {
    for (const item of newItems) {
      const known = new Set(notified[item.target.id] || []);
      const base = new Set(baseline[item.target.id] || []);
      for (const session of current.get(item.target.id) || []) {
        known.add(session.key);
        base.add(session.key);
      }
      notified[item.target.id] = [...known].slice(-1000);
      baseline[item.target.id] = [...base].slice(-1000);
    }
  }

  const now = new Date().toISOString();
  await putState(env, EMERGENCY_STATE_KEY, {
    ...previous,
    version: 1,
    active: true,
    activated_at:
      previous.active && previous.activated_at ? previous.activated_at : now,
    last_checked_at: now,
    last_github_run_at: status.last_run_at || null,
    baseline,
    notified,
    source_pages: pageResults,
    target_results: targetResults,
    last_error:
      firstError ||
      (newItems.length && !alertSent ? "Discord alert failed" : null),
    last_alert_at: alertSent ? now : previous.last_alert_at || null,
  });
  return {
    ok: structuralValid && (!newItems.length || alertSent),
    dry_run: false,
    structural_valid: structuralValid,
    active: true,
    notified_count: alertSent ? newItems.length : 0,
    error: firstError,
  };
}


async function previousDispatchGapReason(env) {
  const [probeRow, statusRow] = await Promise.all([
    getState(env, GITHUB_DISPATCH_PROBE_KEY),
    getState(env, "status"),
  ]);
  const probe = probeRow?.value;
  if (!probe?.success || !probe?.dispatched_at) return null;

  const dispatchedAt = Date.parse(probe.dispatched_at);
  if (!Number.isFinite(dispatchedAt)) return null;
  if (Date.now() - dispatchedAt < DISPATCH_ACK_GRACE_MS) return null;

  const lastRunAt = Date.parse(statusRow?.value?.last_run_at || "");
  if (!Number.isFinite(lastRunAt) || lastRunAt < dispatchedAt) {
    return "previous_dispatch_unacknowledged";
  }
  return null;
}

async function saveDispatchProbe(env, dispatchedAt, success, error = null) {
  await putState(env, GITHUB_DISPATCH_PROBE_KEY, {
    dispatched_at: dispatchedAt,
    success: Boolean(success),
    error: error ? String(error).slice(0, 500) : null,
    recorded_at: new Date().toISOString(),
  });
}

async function maybeRunEmergencyFallback(
  env,
  dispatchError = null,
  forceReason = null,
) {
  const [statusRow, emergencyRow] = await Promise.all([
    getState(env, "status"),
    getState(env, EMERGENCY_STATE_KEY),
  ]);
  const status = statusRow?.value;
  const previous = emergencyRow?.value || {};
  const lastRun = Date.parse(status?.last_run_at || "");
  const age = Number.isFinite(lastRun)
    ? Math.max(0, Date.now() - lastRun)
    : Number.POSITIVE_INFINITY;
  if (
    !dispatchError &&
    !forceReason &&
    Number.isFinite(lastRun) &&
    age < EMERGENCY_STALE_MS
  ) {
    if (previous.active) {
      await putState(env, EMERGENCY_STATE_KEY, {
        ...previous,
        active: false,
        recovered_at: new Date().toISOString(),
        last_github_run_at: status.last_run_at || null,
      });
    }
    return { ok: true, active: false, skipped: true };
  }
  const result = await performEmergencyCheck(env, false);
  return {
    ...result,
    reason:
      forceReason ||
      (dispatchError ? "dispatch_failed" : "github_status_stale"),
    github_age_seconds: Number.isFinite(age)
      ? Math.floor(age / 1000)
      : null,
  };
}

function emergencyStatusText(emergency) {
  if (emergency?.active) {
    return "**활성** · 핵심 날짜 Cloudflare 직접 구조화 감시\n마지막 확인: " +
      formatTime(emergency.last_checked_at) +
      (emergency.last_error ? "\n최근 오류: " + emergency.last_error : "");
  }
  return "대기 · GitHub 실행 상태가 " +
    Math.round(EMERGENCY_STALE_MS / 60000) +
    "분 이상 갱신되지 않거나 dispatch 실패 시 자동 전환" +
    (emergency?.last_checked_at
      ? "\n최근 비상 확인: " + formatTime(emergency.last_checked_at)
      : "");
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
  if (status === "error") return "🔴 장애 지속";
  if (status === "warning") return "🟠 일시 확인 실패";
  if (status === "fallback") return "🟡 보조 감시 중";
  return "🟢 정상";
}

function detectionModeLabel(mode) {
  const labels = {
    public_primary: "🟢 제3자 구조화 정상",
    empty_unconfirmed: "🔵 미래 0건 · 미확정 감시",
    cgv_official: "🟡 CGV 공식 구조화 보조",
    text_fallback: "🟠 CGV 텍스트 보조",
    cloudflare_fallback: "🟠 Browser Run 비상 보조",
    api_protection: "🛡️ 제3자 API 보호모드",
    source_unhealthy: "⚠️ 소스 Sentinel 이상",
    failed: "🔴 확인 실패",
    not_checked: "⚪ 이번 주기 미조회",
    structured: "🟢 구조화 정상",
    fallback: "🟡 보조 감시",
  };
  return labels[String(mode || "")] || "⚪ 상태 미확인";
}

function sentinelLabel(status) {
  if (status === "unhealthy") return "⚠️ Sentinel 이상";
  if (status === "healthy") return "✅ Sentinel 정상";
  return "➖ Sentinel 주기 사이";
}

function targetDetectionText(target) {
  const parts = [detectionModeLabel(target?.detection_mode)];
  if (
    target?.api_guard_active &&
    target?.detection_mode !== "api_protection"
  ) {
    parts.push("🛡️ API 보호모드");
  }
  parts.push(sentinelLabel(target?.source_sentinel_status));
  return parts.join(" · ");
}

async function buildSystemStatus(env) {
  const [
    statusRow,
    cronRow,
    monitoringStats,
    browserBudget,
    emergencyRow,
  ] = await Promise.all([
    getState(env, "status"),
    getState(env, "cron"),
    getMonitoringStats(env),
    browserBudgetState(env),
    getState(env, EMERGENCY_STATE_KEY),
  ]);

  const status = statusRow?.value;
  const cron = cronRow?.value;
  const emergency = emergencyRow?.value || null;

  if (!status) {
    return {
      title: "📡 CGV 알림 시스템 상태",
      description: "아직 GitHub Actions에서 상태 데이터가 전송되지 않았습니다.",
      color: 0xf1c40f,
    };
  }

  const targets = status.targets || [];
  const health = status.health_summary || "normal";
  const recentError = status.recent_error || "없음";
  const fallbackModes = new Set([
    "cgv_official",
    "text_fallback",
    "cloudflare_fallback",
    "api_protection",
    "source_unhealthy",
    "fallback",
  ]);
  const fallbackActive =
    health === "fallback" ||
    Boolean(emergency?.active) ||
    targets.some(
      (target) =>
        fallbackModes.has(String(target?.detection_mode || "")) ||
        target?.api_guard_active,
    );

  const description =
    health === "error"
      ? "**🔴 장애 지속**"
      : health === "warning"
        ? "**🟠 확인 필요**"
        : fallbackActive
          ? "**🟡 보조/보호 감시 중**"
          : "**🟢 정상**";

  const modeLines = targets.map(
    (target) =>
      `**${target.label || target.id}** — ${targetDetectionText(target)}`,
  );
  const modeText = (modeLines.join("\n") || "활성 감시 대상 없음").slice(
    0,
    1024,
  );

  return {
    title: "📡 CGV 알림 시스템 상태",
    description,
    color:
      health === "error" ? 0xe74c3c :
      (health === "warning" || fallbackActive) ? 0xf1c40f :
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
        name: "🚨 GitHub 장애 비상 감시",
        value: emergencyStatusText(emergency),
        inline: false,
      },
      {
        name: "🛡️ 마지막 감시 성공",
        value: formatTime(
          status.last_monitoring_success_at ||
          status.last_primary_success_at ||
          status.last_cgv_success_at,
        ),
        inline: true,
      },
      {
        name: "✅ 마지막 제3자 구조화 정상",
        value: formatTime(
          status.last_primary_success_at || status.last_cgv_success_at,
        ),
        inline: true,
      },
      {
        name: "🎬 활성 감시",
        value: `${status.active_count ?? targets.length}개`,
        inline: true,
      },
      {
        name: "🔎 현재 감지 경로",
        value: modeText,
        inline: false,
      },
      {
        name: "📊 최근 24시간 감시 · 새 체계",
        value: monitoringStatsText(monitoringStats),
        inline: false,
      },
      {
        name: "🌐 Browser Run 비상 경로",
        value: browserBudgetText(browserBudget),
        inline: false,
      },
      {
        name: fallbackActive ? "최근 참고사항" : "최근 오류",
        value: String(recentError).slice(0, 1000),
        inline: false,
      },
    ],
    footer: {
      text: "CGV Alert · Cloudflare + GitHub Actions + Emergency Fallback",
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
          `**감지 상태** ${detectionModeLabel(target.detection_mode)}`,
          `**소스 검증** ${sentinelLabel(target.source_sentinel_status)}`,
          `**API 보호** ${
            target.api_guard_active
              ? `활성 · ${formatTime(target.api_guard_until)}까지`
              : "비활성"
          }`,
          `**마지막 감시 성공** ${formatTime(target.last_success_at)}`,
          `**마지막 제3자 구조화 정상** ${formatTime(
            target.last_primary_success_at ||
            target.last_structured_success_at,
          )}`,
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
      "**/즉시확인** — 주기를 무시하고 활성 감시 대상 전체를 즉시 조회 (관리자, 60초 쿨다운)",
    ].join("\n"),
    footer: {
      text: "즉시확인은 전체 설정 날짜를 강제로 조회하고 완료 결과를 다시 알려줍니다.",
    },
  };
}

async function handleDiscordInteraction(request, env, ctx) {
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

  if (interaction.guild_id && ctx) {
    ctx.waitUntil(rememberGuildAndRegister(env, interaction));
  }

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
        "🔎 즉시 확인을 요청했습니다. 주기를 무시하고 활성 감시 대상을 모두 확인한 뒤 Discord로 결과를 다시 알려드립니다.",
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

        let previousGapReason = null;
        try {
          previousGapReason = await previousDispatchGapReason(env);
          const emergency = await maybeRunEmergencyFallback(
            env,
            null,
            previousGapReason,
          );
          console.log(
            "CGV emergency pre-dispatch check",
            JSON.stringify(emergency),
          );
        } catch (error) {
          console.error("CGV emergency pre-dispatch check failed", error);
        }

        const dispatchedAt = new Date().toISOString();
        try {
          await triggerGitHub(env);
          await saveDispatchProbe(env, dispatchedAt, true);
        } catch (error) {
          console.error("CGV Alert workflow dispatch failed", error);
          try {
            await saveDispatchProbe(
              env,
              dispatchedAt,
              false,
              error,
            );
          } catch (probeError) {
            console.error("GitHub dispatch probe save failed", probeError);
          }

          try {
            const emergency = await maybeRunEmergencyFallback(
              env,
              error,
              "current_dispatch_failed",
            );
            console.log(
              "CGV emergency dispatch-failure check",
              JSON.stringify(emergency),
            );
          } catch (fallbackError) {
            console.error("CGV emergency fallback failed", fallbackError);
          }
        }
      })(),
    );
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (
      request.method === "POST" &&
      url.pathname === "/discord/interactions"
    ) {
      return handleDiscordInteraction(request, env, ctx);
    }

    if (
      request.method === "POST" &&
      url.pathname === "/api/runtime-check"
    ) {
      if (!authorizedStatusUpdate(request, env)) {
        return new Response("Unauthorized", { status: 401 });
      }
      const [browserBudget, monitoringStats, emergencyRow] =
        await Promise.all([
          browserBudgetState(env),
          getMonitoringStats(env),
          getState(env, EMERGENCY_STATE_KEY),
        ]);
      return jsonResponse({
        ok: true,
        version: COMMAND_VERSION,
        browser_binding: Boolean(env.BROWSER),
        browser_budget: {
          used_ms: browserBudget.usedMs,
          safe_limit_ms: 8 * 60 * 1000,
          remaining_ms: Math.max(
            0,
            8 * 60 * 1000 - Number(browserBudget.usedMs || 0),
          ),
        },
        monitoring_stats_24h: monitoringStats,
        emergency_fallback: {
          stale_after_seconds: Math.floor(EMERGENCY_STALE_MS / 1000),
          state: emergencyRow?.value || null,
        },
        now: new Date().toISOString(),
      });
    }

    if (
      request.method === "POST" &&
      url.pathname === "/api/emergency-check"
    ) {
      if (!authorizedStatusUpdate(request, env)) {
        return new Response("Unauthorized", { status: 401 });
      }
      try {
        const result = await performEmergencyCheck(env, true);
        return jsonResponse(result, result.ok ? 200 : 502);
      } catch (error) {
        return jsonResponse(
          { ok: false, dry_run: true, error: String(error) },
          500,
        );
      }
    }

    if (
      request.method === "GET" &&
      url.pathname === "/api/emergency-seen"
    ) {
      if (!authorizedStatusUpdate(request, env)) {
        return new Response("Unauthorized", { status: 401 });
      }
      const row = await getState(env, EMERGENCY_STATE_KEY);
      return jsonResponse({
        ok: true,
        notified: row?.value?.notified || {},
        active: Boolean(row?.value?.active),
        updated_at: row?.updated_at || null,
      });
    }

    if (
      request.method === "POST" &&
      url.pathname === "/api/browser-check"
    ) {
      return handleBrowserCheck(request, env);
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
        const monitoringStats = await recordMonitoringSample(env, incoming);
        return jsonResponse({
          ok: true,
          monitoring_stats_24h: monitoringStats,
        });
      } catch (error) {
        console.error("Status update failed", error);
        return jsonResponse({ ok: false, error: String(error) }, 500);
      }
    }

    if (request.method === "GET" && url.pathname === "/health") {
      let discordCommandSetup;
      let discordCommandState = null;
      const [monitoringStats, browserBudget, emergencyRow] =
        await Promise.all([
          getMonitoringStats(env),
          browserBudgetState(env),
          getState(env, EMERGENCY_STATE_KEY),
        ]);

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
        browser_binding: Boolean(env.BROWSER),
        browser_budget: {
          used_ms: browserBudget.usedMs,
          safe_limit_ms: 8 * 60 * 1000,
        },
        monitoring_stats_24h: monitoringStats,
        emergency_fallback: {
          stale_after_seconds: Math.floor(EMERGENCY_STALE_MS / 1000),
          state: emergencyRow?.value || null,
        },
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
