import assert from "node:assert/strict";
import { priorityPrimaryFailed } from "./worker.js";

const today = "20261012";
const base = {
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
  targets: [{ ...base.targets[0], priority_session_snapshot_at: "2026-10-12T09:55:00+09:00" }],
}, today), false, "Healthy primary must not double-poll");
assert.equal(priorityPrimaryFailed({
  targets: [{ ...base.targets[0], checked_page_count: 0 }],
}, today), false, "Not-due runs must not be labeled source failures");
assert.equal(priorityPrimaryFailed({
  targets: [{ ...base.targets[0], priority_dates: ["20261011"] }],
}, today), false, "Expired watch dates must not trigger takeover");
assert.equal(priorityPrimaryFailed({
  targets: [{ ...base.targets[0], priority_dates: ["20261218"], checked_page_count: 1,
    priority_session_snapshot_at: null }, { ...base.targets[0],
    priority_session_snapshot_at: "2026-10-12T09:55:00+09:00" }],
}, today), true, "One failed target is enough to activate backup");
console.log("SOURCE_TAKEOVER_TEST PASS: source fail, healthy, not due, expired, mixed");
