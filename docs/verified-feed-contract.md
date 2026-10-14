# Permissioned collector inlet — 2026-10-14

**Implemented and tested; disabled. No new live data source acquired.**
This module is an input boundary, not an API access workaround or a CGV
scraper. It cannot create external permission, credentials or current seats.

## What is missing, specifically

An authorized publisher must provide all of the following:

1. CGV's permission for unattended, read-only timetable/seat access, or a
   licensed provider's permission to supply these CGV records to this system.
   The permission must cover theater 0128, requested dates and intended cadence.
   A publicly working browser, copied UA, CGV login account or a reference
   string alone does not establish this permission.
2. A stable HTTPS endpoint and its credential, with documented rate limits.
   No such endpoint/credential is currently connected. No request was sent to
   CGV's blocked APIs to try different clients or network paths in this work.
3. Exact movie/theater/date/screen/sequence/start and explicit sale-enabled
   state, total and remaining seats. A publisher observation time must describe
   an actual upstream read, not just repackaging an old moviebara snapshot.
4. Separate-time live shadow observations and measured update latency before
   activation. The implemented probe never promotes Production automatically.

The prior investigation established CGV's general crawler Disallow policy.
This follow-up found no public license or unattended timetable feed grant.
Official search results expose an advertising/partnership inquiry category in
CGV's privacy notice (`https://cgv.co.kr/tme/tmeStpl/27`), but that category is
not a seat-feed offer or authorization. No inquiry, contract, payment, account
creation, new credentials or permissions were submitted.

## Implemented boundary

- `verified_feed.py`: one authenticated HTTPS read per theater/run; no redirect
  following, retry loop, cookies, browser/TLS impersonation or proxy.
- Maximum upstream observation age: 300 seconds, with 30 seconds of clock
  skew. Published timestamp must be consistent with the actual observation.
- Full identity and integer seat fields required, positive total capacity,
  explicit Boolean bookable status, declared complete-date coverage.
- Duplicate identities and ambiguous mapping to existing provider keys reject
  the entire packet. An empty future response cannot qualify a feed: a real
  current/next-day bookable sentinel is required in every accepted packet.
- 403/429 stop the publisher and persist a backoff (at least 30m, or longer
  Retry-After) in the existing state file. Errors do not expose endpoint/token.
- `checker.py` prefers the verified feed only if explicitly enabled and its
  packet passes validation. Missing coverage/stale/error goes through existing
  fallbacks and retains failure evidence. No current source is enabled.
- Existing public dedup keys stay unchanged. Previous official screen/time
  keys are checked as aliases; records are preserved rather than migrated.
  Positive seats with bookable=false never generate an opening alert.
- `collection_outcome.py` reports the source ID/origin/actual observation age
  and distinguishes zero future rows from current collection success.

## Publisher JSON contract

Required packet fields:

| Field | Meaning |
|---|---|
| schema_version | Integer 1 |
| source_id | Must equal configured publisher ID |
| authorization_reference | Must equal operator's documented authorization reference |
| origin | cgv_first_party or licensed_cgv_feed |
| theater_code / theater_name | Requested theater identity |
| observed_at | Timezoned ISO timestamp of actual upstream read |
| published_at | Timezoned ISO publication timestamp, not before observed_at |
| complete_dates | Explicit YYYYMMDD dates whose query responses are complete |
| rows | Nonempty full timetable including a near-term bookable sentinel |

Every row requires theater_code, play_date (YYYYMMDD), movie_code, movie_name,
screen_no, screen_name, sequence, start_time (HH:MM, extended hours through 29),
total_seats, remaining_seats, bookable. end_time is optional. Identifiers are
strings; seat counts are JSON integers; bookable is a JSON Boolean.

Authorization reference matching authenticates operator selection of the
publisher. It is not independent legal or upstream authenticity verification.
Those are external admission gates and must be documented before activation.

## Activation after real qualification

The existing Production workflow is already wired for these optional values:

- Secrets: CGV_VERIFIED_FEED_URL, CGV_VERIFIED_FEED_TOKEN.
- Variables: CGV_VERIFIED_FEED_SOURCE_ID, CGV_VERIFIED_FEED_PERMISSION.
- Activation variable: CGV_VERIFIED_FEED_ENABLED=1, **only after qualification**.

The credentials are separate from existing STATUS_API_TOKEN and Discord
credentials. Do not reuse or send those existing secrets to a new publisher.
No new secret values or permissions were created during this task.

Use Verified CGV Feed Boundary → workflow_dispatch → live_probe=true to obtain
two observations a minute apart. A static observation timestamp fails, even
if both HTTP requests succeed. The probe writes verified_feed_live.json;
no config, state, D1 or Discord is changed. Continued shadow observation is
required to establish the 24h condition and real opening-to-alert latency.

Once qualified, activation uses the existing 5m priority and staged other-date
scheduler and notification engine. Rollback: disable the activation variable;
retain current state and all secrets. The old source behavior is the default.

## This task's evidence and verdict

- Baseline main inspected: f2bd59c280857935e00ec7835f145a535ea7bbe8.
- Official Work browser UI: Ulsan Samsan, Odyssey IMAX 9, 13:30–16:32,
  429/441 seats; 16:50–19:52 433/441; 20:20–23:22 430/441. These are direct UI
  observations, not packets from this inlet and not unattended collection.
- At 03:54:18 UTC the current-day UI still showed those records. Selecting a
  date or observing an unchanged UI is not proof of an independent upstream
  refresh. Full first-party sequence metadata was not extracted.
- `python verify_feed_live.py` with the actual unconfigured environment:
  live_collection_verified=false, samples=[], no authorized publisher.
- Existing 28 regressions plus 10 new boundary/pipeline tests pass (38 total).
  All new packets are explicitly synthetic. Mocked Discord calls verify new
  session/seat-only/duplicate/legacy-alias handling; no live test alert sent.
- Final status: **미복구** for automatic data collection. Interface readiness
  does not establish a live source, a measured success rate or opening delay.

Known code-test coverage: disabled=no requests; identity/seat/status/coverage
validation; stale/future clocks; duplicate/ambiguous keys; false bookable;
403/429/redirect/5xx without retries; normalized production pipeline and
legacy key preservation. Live source and 24h stability remain unverified.

## Post-merge Production verification

Implementation commit: 4ee46b49f997f7e8d567d8aa63864d34ce1b7fe0.
PR #4 merged as 93989d8ca1f5b19f62a1f6b750fc6fe6553681f0.
Main contract run 38509076577 passed all 38 tests and Worker checks;
live-shadow was skipped because no qualified publisher was configured.

Production run 38509188753 (2026-10-14 04:01 UTC) executed the new code.
All optional feed settings were empty, so no feed request was made.
Actual outcome: failed; 14 checked pages, 0 collected, 14 failed. Primary
HTTP 502 and stale backup manifest (22,284 seconds) remained recorded.
The existing known-outage email suppression explains the green Actions result;
it does not represent collection recovery. Runtime status upload completed.

Compared f2bd59c with post-run main a84e225: config.json and Worker code were
identical, and all previous seen/fallback_seen keys remained present. Secrets
were not changed and no D1 mutation was introduced by this change.

The official UI legal notice allows personal use/storage of site information,
but it does not specify an unattended feed, cadence or service guarantee.
The page's calendar heading also displayed an inconsistent year/month; visible
seat text alone cannot establish complete dated first-party session identity.
Neither that notice nor successful UI rendering was treated as a verified
automated integration. No 24-hour success rate or opening latency was measured.

Remaining external prerequisites: an authorized unattended read-only endpoint
or publisher, its access credentials and permission scope for theater 0128,
complete session/movie/screen identities and sale/seat status, and independently
verified upstream observations no more than five minutes old. These were not
obtained during this task. No verified replacement source is active.
