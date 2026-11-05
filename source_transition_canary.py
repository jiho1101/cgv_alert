"""Read-only CGV source-publication transition canary.

Tests whether the EXISTING public third-party relay can change from a 502 /
unpublished future date to valid, bookable showtimes before an actual target
opens. No CGV requests, no Discord, no alert/seen changes, and no interpretation
of an absent date as proof of no bookings.
"""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from shadow_public_monitor import (
    canonical,
    extract_rows,
    inspect_rows,
    structural_valid,
)

from checker import (_production_public_request as request_timetable,
                     _parse_retry_after_seconds, _public_guard_failure)

KST = ZoneInfo("Asia/Seoul")
HORIZON_DAYS = 8
PROBE_INTERVAL_SECONDS = 5 * 60
MAX_HISTORY = 20
STATE_KEY = "source_transition_canary"


def _parse_dt(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else None
    except (TypeError, ValueError):
        return None


def _guard_active(state, now):
    guard = state.get("public_api_guard") or {}
    try:
        last_status = int(guard.get("last_status") or 0)
    except (TypeError, ValueError):
        last_status = 0
    until = _parse_dt(guard.get("backoff_until"))
    return (last_status == 429 or bool(guard.get("retry_after_raw"))) and until is not None and until > now


def choose_canary_date(state, now):
    """Keep one date until publication or expiry, then probe a fresh future day."""
    current = (state.get(STATE_KEY) or {}).get("date")
    if isinstance(current, str) and len(current) == 8 and current.isdigit():
        try:
            date = datetime.strptime(current, "%Y%m%d").date()
            if date >= (now + timedelta(days=2)).date():
                return current
        except ValueError:
            pass
    return (now + timedelta(days=HORIZON_DAYS)).strftime("%Y%m%d")


def qualify_canary_response(status, payload, error, site, play_date):
    raw, shape_ok = extract_rows(payload)
    rows = [canonical(row) for row in raw]
    rows = [row for row in rows if row is not None]
    inspection = inspect_rows(rows)
    qualifying = structural_valid(status, error, shape_ok, inspection)
    if not qualifying or not rows:
        return False, len(rows), "unqualified"
    if any(row["theater_code"] != site or row["play_date"] != play_date
           for row in rows):
        return False, len(rows), "identity_mismatch"
    def positive_seats(row):
        try:
            total = int(row["total_seats"])
            remaining = int(row["remaining_seats"])
            return total > 0 and 0 < remaining <= total
        except (TypeError, ValueError, OverflowError):
            return False
    if not any(positive_seats(row) for row in rows):
        return False, len(rows), "no_bookable_seat_evidence"
    return True, len(rows), "qualified_near_future"


def run_canary(state, now, request=request_timetable, site="0128"):
    """At most one public-relay probe per 5m; shares supplier rejection protection.

    Caller persists the returned state via its existing semantics-preserving
    GitHub Actions save step. No branch writes, push or Discord are done here.
    """
    if _guard_active(state, now):
        return {"skipped": "supplier_429_backoff"}
    previous = state.get(STATE_KEY) or {}
    canary_backoff = _parse_dt(previous.get("backoff_until"))
    if canary_backoff and canary_backoff > now:
        return {"skipped": "canary_429_backoff"}
    last = _parse_dt(previous.get("last_request_at"))
    if last and int(last.timestamp()) // PROBE_INTERVAL_SECONDS >= int(now.timestamp()) // PROBE_INTERVAL_SECONDS:
        return {"skipped": "not_due"}

    date = choose_canary_date(state, now)

    probe = (dict(previous) if date == previous.get("date")
             else {"date": date, "history": [], "started_at": now.isoformat()})
    probe["last_request_at"] = now.isoformat()
    probe["attempts"] = min(int(probe.get("attempts") or 0) + 1, 10000)

    response = request(site, date)
    status, elapsed, payload, error = response[:4]
    retry_after = response[4] if len(response) > 4 else None
    valid, row_count, reason = qualify_canary_response(
        status, payload, error, site, date
    )
    # Continue after publication: the same production parser and stable keys
    # distinguish real additions from repeated responses and seat changes.
    # This observer has no webhook, target writes or production seen access.
    from checker import extract_public_sessions
    from shadow_public_monitor import identity_key
    qualified_rows = []
    if valid:
        for raw in extract_rows(payload)[0]:
            row = canonical(raw)
            if not row:
                continue
            target = {"id": "source-canary", "label": row["movie_name"],
                      "movie_aliases": [row["movie_name"]],
                      "theater_code": site, "require_sale_open": True,
                      "min_remaining_seats": 1}
            if extract_public_sessions([row], target, date,
                                       pinned_movie_code=row["movie_code"]):
                qualified_rows.append(row)
        if not qualified_rows:
            valid, reason = False, "production_parser_rejected"
    current_keys = {identity_key(row) for row in qualified_rows}
    prior_keys = set(probe.get("observed_keys") or [])
    baseline_exists = "observed_keys" in probe
    new_keys = current_keys - prior_keys if baseline_exists else set()
    if valid:
        # Preserve missing identities, just as production seen is preserved.
        probe["observed_keys"] = sorted(prior_keys | current_keys)[-1000:]
    probe["successful_observations"] = int(probe.get("successful_observations") or 0) + int(valid)
    probe["failed_observations"] = int(probe.get("failed_observations") or 0) + int(not valid)
    sample = {
        "at": now.isoformat(),
        "http_status": status,
        "rows": row_count,
        "qualified": valid,
        "reason": reason,
        "elapsed_seconds": elapsed,
        "production_parser_rows": len(qualified_rows),
        "new_session_count": len(new_keys),
        "baseline_created": valid and not baseline_exists,
    }
    probe["history"] = (probe.get("history") or [])[-(MAX_HISTORY-1):] + [sample]
    probe["last_status"] = status
    probe["last_valid_rows"] = row_count if valid else 0
    retry_seconds = _parse_retry_after_seconds(retry_after, now)
    if status == 429 or (retry_seconds is not None and status != 200):
        # Honor explicit supplier deadlines. Missing 429 deadline gets >=1h.
        delay = max(retry_seconds or 0, 3600 if status == 429 else 0)
        probe["backoff_until"] = (now + timedelta(seconds=delay)).isoformat()
        # The observer shares the same supplier/client quota as production.
        # Its explicit rejection must protect the next core run as well.
        _public_guard_failure(state, status, retry_after, now)
    if not valid:
        probe["last_unqualified_at"] = now.isoformat()
    elif not probe.get("qualified_at"):
        probe["qualified_at"] = now.isoformat()
        probe["transition_from_unqualified"] = bool(probe.get("last_unqualified_at"))
        # This bounds source observation, not official CGV sale-open time.
        # Actual publication-to-notification latency remains unmeasured.
    state[STATE_KEY] = probe
    return {
        "date": date,
        "http_status": status,
        "rows": row_count,
        "qualified": valid,
        "production_parser_rows": len(qualified_rows),
        "new_session_count": len(new_keys),
        "successful_observations": probe["successful_observations"],
        "failed_observations": probe["failed_observations"],
        "transition_from_unqualified": bool(probe.get("transition_from_unqualified")),
        "last_unqualified_at": probe.get("last_unqualified_at"),
        "observed_at": now.isoformat(),
        "source": "public_third_party_relay",
        "note": "canary only: not the December watched dates",
    }


def main():
    now = datetime.now(KST)
    path = Path("state.json")
    if not path.exists():
        print("SOURCE_TRANSITION_CANARY skipped: state.json missing")
        return
    state = json.loads(path.read_text(encoding="utf-8"))
    result = run_canary(state, now)
    if result.get("skipped"):
        print("SOURCE_TRANSITION_CANARY", json.dumps(result))
        return
    path.write_text(
        json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )
    Path("source_transition_observation.json").write_text(
        json.dumps({"result": result, "observation": state.get(STATE_KEY)},
                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("SOURCE_TRANSITION_CANARY", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
