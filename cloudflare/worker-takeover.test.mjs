import assert from "node:assert/strict";
import { classifyMonitoringSample, summarizeMonitoringSamples } from "./worker.js";
assert.equal(classifyMonitoringSample({collection_evidence_version: 1,
  collection: {outcome: "unconfirmed"}}), "unconfirmed");
assert.equal(classifyMonitoringSample({collection_evidence_version: 1,
  collection: {outcome: "partial"}}), "failed");
assert.equal(classifyMonitoringSample({collection_evidence_version: 1,
  collection: {outcome: "not_due"}}), null);
assert.equal(classifyMonitoringSample({targets: [{detection_mode: "empty_unconfirmed"}]}), "unconfirmed");
const statsAt = new Date().toISOString();
const truthfulStats = summarizeMonitoringSamples([
  {at: statsAt, mode: "structured"}, // Historical transport-only sample.
  {at: statsAt, mode: "unconfirmed", evidence_version: 1},
  {at: statsAt, mode: "failed", evidence_version: 1},
]);
assert.equal(truthfulStats.total, 2);
assert.equal(truthfulStats.unconfirmed, 1);
assert.equal(truthfulStats.monitoring_success_rate, 0);
console.log("ACTUAL_COLLECTION_STATS PASS: empty/partial/not_due/legacy evidence");
import { priorityPrimaryFailed } from "./worker.js";

const today = "20261012";
const base = {
  last_run_at: "2026-10-12T09:55:00+09:00",
  targets: [{
    id: "avengers-doomsday-ulsan-samsan",
    priority_dates: ["20261218"],
    checked_page_count: 1,
    priority_session_snapshot_at: null,
  }],
};
assert.equal(priorityPrimaryFailed(base, today), true,
  "A successful Actions job with failed primary data must activate takeover");
assert.equal(priorityPrimaryFailed({
  ...base,
  targets: [{ ...base.targets[0], priority_session_snapshot_at: "2026-10-12T09:55:00+09:00" }],
}, today), false, "Healthy primary must not double-poll");
assert.equal(priorityPrimaryFailed({
  ...base,
  targets: [{ ...base.targets[0], checked_page_count: 0 }],
}, today), false, "Not-due runs must not be labeled source failures");
assert.equal(priorityPrimaryFailed({
  ...base,
  targets: [{ ...base.targets[0], priority_dates: ["20261011"] }],
}, today), false, "Expired watch dates must not trigger takeover");
assert.equal(priorityPrimaryFailed({
  ...base,
  targets: [{ ...base.targets[0], priority_dates: ["20261218"], checked_page_count: 1,
    priority_session_snapshot_at: null }, { ...base.targets[0],
    priority_session_snapshot_at: "2026-10-12T09:55:00+09:00" }],
}, today), true, "One failed target is enough to activate backup");

assert.equal(priorityPrimaryFailed({
  ...base,
  targets: [{ ...base.targets[0],
    priority_session_snapshot_at: "2026-10-12T09:50:00+09:00" }],
}, today), true, "Preserved previous success must not hide a new 502 incident");

console.log("SOURCE_TAKEOVER_TEST PASS: current/stale success, failed, healthy, not due, expired, mixed");

import { independentSnapshotRows } from "./worker.js";
const NOW = Date.parse("2026-10-12T01:40:00Z");
const MANIFEST = {
  schema_version: 1,
  generated_at: "2026-10-12T01:10:00Z",
  source_updated_at: { screenings: "2026-10-11T20:00:00Z" },
};
const CGV_ROW = {
  provider: "CGV", cinema_code: "0128",
  movie_title: "어벤져스: 둠스데이", source_movie_code: "30009999",
  play_date: "2026-12-18", start_dt: "10:20",
  total_seat_cnt: 440, remain_seat_cnt: 439,
  url: "https://cgv.co.kr/cnm/movieBook/movie?movNo=30009999&scnYmd=20261218&siteNo=0128&scnsNo=009&scnSseq=1",
};
const SNAPSHOT = {
  cinema: { cinema_code: "0128", provider: "CGV" },
  screenings: [CGV_ROW],
};
const page = independentSnapshotRows(MANIFEST, SNAPSHOT, "0128", "20261218", NOW);
assert.equal(page.length, 1);
assert.equal(page[0].schedule_id, "2026121801281");
assert.equal(page[0].remaining_seats, 439);
assert.deepEqual(
  independentSnapshotRows(MANIFEST,
    { ...SNAPSHOT, screenings: [{ ...CGV_ROW, remain_seat_cnt: 1 }] },
    "0128", "20261218", NOW).map(r =>
      [r.movie_code, r.play_date, r.start_time, r.schedule_id]
    ),
  page.map(r => [r.movie_code, r.play_date, r.start_time, r.schedule_id]),
  "seat-only changes do not alter booking identity",
);
assert.equal(independentSnapshotRows(MANIFEST, SNAPSHOT, "0128", "20261219", NOW).length, 0);
assert.throws(() => independentSnapshotRows(
  MANIFEST, { ...SNAPSHOT, screenings: [CGV_ROW, CGV_ROW] }, "0128", "20261218", NOW),
  /collision/,
);
assert.throws(() => independentSnapshotRows(
  MANIFEST, { ...SNAPSHOT, screenings: [{ ...CGV_ROW, remain_seat_cnt: 900 }] }, "0128", "20261218", NOW),
  /seat|invalid/,
);
assert.throws(() => independentSnapshotRows(
  MANIFEST, { ...SNAPSHOT, screenings: [{ ...CGV_ROW,
    url: CGV_ROW.url.replace("siteNo=0128", "siteNo=0056") }] }, "0128", "20261218", NOW),
  /identity mismatch/,
);
assert.throws(() => independentSnapshotRows(
  { ...MANIFEST, generated_at: "2026-10-11T20:00:00Z" },
  SNAPSHOT, "0128", "20261218", NOW
), /manifest stale/);
console.log("WORKER_INDEPENDENT_FALLBACK_TEST PASS: identity, seats, duplicate, URL, staleness");

/* Regression: a stalled D1 write or failed third-party feed must not hold up
   the 5-minute GitHub dispatch. No real network, state, or Discord calls. */
import worker from "./worker.js";
{
  const savedFetch = globalThis.fetch;
  const savedConsoleError = console.error;
  const savedConsoleLog = console.log;
  const calls = [];
  let releaseDb;
  const dbGate = new Promise((resolve) => { releaseDb = resolve; });
  let completion;
  const blockedDb = {
    prepare() {
      calls.push("db");
      return {
        run: async () => { await dbGate; },
        bind() { return this; },
        first: async () => { await dbGate; return null; },
      };
    },
  };
  try {
    globalThis.fetch = async (url, init) => {
      calls.push("fetch");
      assert.match(String(url), /\/actions\/workflows\/cgv-alert\.yml\/dispatches$/);
      assert.equal(init?.method, "POST");
      return { status: 204 };
    };
    // This uses an intentionally empty fake DB, so the resulting
    // "emergency config unavailable" belongs ONLY to this test.
    // Never emit it as if it were an actual Cloudflare production log.
    console.error = () => {};
    console.log = () => {};
    await worker.scheduled({}, {
      GITHUB_TOKEN: "test-token", DB: blockedDb,
    }, {
      waitUntil(promise) { completion = promise; },
    });
    // The atomic D1 gate is allowed only a bounded 500ms wait.
    const deadline = Date.now() + 2000;
    while (!calls.includes("fetch") && Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(calls.filter((event) => event === "fetch").length, 1,
      "A stalled D1 claim must fail open within 2 seconds and dispatch");
    assert.equal(calls.filter((event) => event === "fetch").length, 1,
      "Each cron tick must dispatch exactly once");
    assert.ok(completion, "Cron must register its async work");
    releaseDb();
    await completion;
  } finally {
    releaseDb();
    globalThis.fetch = savedFetch;
    console.error = savedConsoleError;
    console.log = savedConsoleLog;
  }
}
console.log("CRON_DISPATCH_FIRST_TEST PASS: bounded D1 claim cannot prevent GitHub dispatch");

import { emergencySessions } from "./worker.js";
const identityTarget = {
  id: "identity-check", theater_code: "0128", play_date: "20991028",
  aliases: ["실제영화"], require_sale_open: true, min_remaining_seats: 1,
};
const identityRow = {
  movie_code: "30001440", movie_name: "실제 영화", theater_code: "0128",
  play_date: "20991028", start_time: "10:00", schedule_id: "2099102801281",
  total_seats: 441, remaining_seats: 440,
};
assert.equal(emergencySessions([identityRow], identityTarget).length, 1);
for (const schedule_id of ["", "1", "209910280128", "2099102901281", "2099102899991"]) {
  assert.equal(emergencySessions([{ ...identityRow, schedule_id }], identityTarget).length, 0);
}
assert.deepEqual(emergencySessions([{ ...identityRow, remaining_seats: 1 }], identityTarget),
  emergencySessions([identityRow], identityTarget));
console.log("EMERGENCY_COMPOSITE_ID_TEST PASS: missing sequence, wrong date/site, seat-only stability");


/* Production regression: two Cron arrivals in the same 5-minute window must
   reserve exactly one dispatch across Worker isolates. One reused D1 key must
   permit a new dispatch in the next window without leaking DB records. */
{
  const originalFetch = globalThis.fetch;
  const originalNow = Date.now;
  const originalError = console.error;
  const originalLog = console.log;
  const stored = new Map();
  let networkCalls = 0;
  const sqlDb = {
    prepare(sql) {
      return {
        bind(...parameters) {
          return {
            async run() {
              if (/CREATE TABLE/i.test(sql)) return { meta: { changes: 0 } };
              const [key, value, at] = parameters;
              if (/DELETE FROM app_state/i.test(sql)) {
                const row = stored.get(key);
                if (row?.value === value) {
                  stored.delete(key);
                  return { meta: { changes: 1 } };
                }
                return { meta: { changes: 0 } };
              }
              if (sql.includes("WHERE CAST(app_state.value AS INTEGER)")) {
                const old = stored.get(key);
                if (!old || Number(old.value) < Number(value)) {
                  stored.set(key, { value, updated_at: at });
                  return { meta: { changes: 1 } };
                }
                return { meta: { changes: 0 } };
              }
              if (/INSERT INTO app_state/i.test(sql)) {
                stored.set(key, { value, updated_at: at });
                return { meta: { changes: 1 } };
              }
              throw new Error("unexpected SQL: " + sql.slice(0, 80));
            },
            async first() {
              const row = stored.get(parameters[0]);
              return row ? { ...row } : null;
            },
          };
        },
        async run() {
          if (/CREATE TABLE/i.test(sql)) return { meta: { changes: 0 } };
          throw new Error("unexpected direct run");
        },
      };
    },
  };
  const env = { GITHUB_TOKEN: "test", DB: sqlDb };
  const promises = [];
  const ctx = {
    waitUntil(promise) { promises.push(promise); },
  };
  try {
    const base = Math.floor(originalNow() / 300000) * 300000 + 10000;
    Date.now = () => base;
    globalThis.fetch = async (url) => {
      assert.ok(String(url).endsWith("/actions/workflows/cgv-alert.yml/dispatches"));
      networkCalls++;
      return { status: 204 };
    };
    console.error = () => {};
    console.log = () => {};
    await Promise.all([
      worker.scheduled({}, env, ctx),
      worker.scheduled({}, env, ctx),
    ]);
    await Promise.all(promises.splice(0));
    assert.equal(networkCalls, 1, "same 5-minute slot must dispatch once");
    assert.equal(stored.get("github_cron_slot_claim_v1")?.value,
      String(Math.floor(base / 300000)));
    Date.now = () => base + 300000;
    await worker.scheduled({}, env, ctx);
    await Promise.all(promises.splice(0));
    assert.equal(networkCalls, 2, "next 5-minute slot must dispatch again");
    assert.equal(stored.size < 10, true, "single slot key must not grow forever");
  } finally {
    globalThis.fetch = originalFetch;
    Date.now = originalNow;
    console.error = originalError;
    console.log = originalLog;
  }
}
console.log("CRON_ATOMIC_SLOT_TEST PASS: same-slot duplicate suppressed, next slot allowed");
