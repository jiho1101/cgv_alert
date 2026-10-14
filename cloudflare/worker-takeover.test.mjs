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
