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
    request_timetable,
    structural_valid,
)

KST = ZoneInfo("Asia/Seoul")
HORIZON_DAYS = 8
PROBE_INTERVAL_SECONDS = 10 * 60
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
    return last_status == 429 and until is not None and until > now


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
    """At most one public-relay probe per 10m; never affects primary collection.

    Caller persists the returned state via its existing semantics-preserving
    GitHub Actions save step. No branch writes, push or Discord are done here.
    """
    if _guard_active(state, now):
        return {"skipped": "supplier_429_backoff"}
    previous = state.get(STATE_KEY) or {}
    last = _parse_dt(previous.get("last_request_at"))
    if last and (now - last).total_seconds() < PROBE_INTERVAL_SECONDS:
        return {"skipped": "not_due"}

    date = choose_canary_date(state, now)
    if date == previous.get("date") and previous.get("qualified_at"):
        # Completed canary; wait for the date to age out before rotating.
        return {"skipped": "transition_already_observed"}

    probe = (dict(previous) if date == previous.get("date")
             else {"date": date, "history": [], "started_at": now.isoformat()})
    probe["last_request_at"] = now.isoformat()
    probe["attempts"] = min(int(probe.get("attempts") or 0) + 1, 10000)

    status, elapsed, payload, error = request(site, date)
    valid, row_count, reason = qualify_canary_response(
        status, payload, error, site, date
    )
    sample = {
        "at": now.isoformat(),
        "http_status": status,
        "rows": row_count,
        "qualified": valid,
        "reason": reason,
        "elapsed_seconds": elapsed,
    }
    probe["history"] = (probe.get("history") or [])[-(MAX_HISTORY-1):] + [sample]
    probe["last_status"] = status
    probe["last_valid_rows"] = row_count if valid else 0
    if not valid:
        probe["last_unqualified_at"] = now.isoformat()
    elif not probe.get("qualified_at"):
        probe["qualified_at"] = now.isoformat()
        probe["transition_from_unqualified"] = bool(probe.get("last_unqualified_at"))
        # Publication observed within 10m sampling resolution, not exact
        # CGV sale-open time; do not describe it as a 5m SLA proof.
    state[STATE_KEY] = probe
    return {
        "date": date,
        "http_status": status,
        "rows": row_count,
        "qualified": valid,
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
    print("SOURCE_TRANSITION_CANARY", json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
