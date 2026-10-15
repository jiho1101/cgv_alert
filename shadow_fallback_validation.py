"""Read-only audit of the actual production parser using published independent rows.

Never mutates CGV monitoring state or sends notifications. An absent future
date is not evidence that booking has not opened.
"""

from datetime import datetime


def audit_rows(rows, now, extract_public_sessions, theater_code="0128", priority_date="20261218"):
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    today = now.strftime("%Y%m%d")
    time_now = now.strftime("%H%M")
    bookable_future = []
    for row in rows:
        day = str(row.get("play_date") or "")
        start_time = str(row.get("start_time") or "").replace(":", "")
        if (day < today or (day == today and start_time <= time_now)
                or str(row.get("theater_code") or "") != theater_code):
            continue
        total, remaining = row.get("total_seats"), row.get("remaining_seats")
        if (not isinstance(total, int) or isinstance(total, bool)
                or not isinstance(remaining, int) or isinstance(remaining, bool)
                or not 0 < remaining <= total):
            continue
        bookable_future.append(row)

    bookable_future.sort(key=lambda row: (
        str(row.get("play_date")), str(row.get("start_time")),
        str(row.get("schedule_id")),
    ))
    priority_rows = sum(row["play_date"] == priority_date for row in bookable_future)
    rejected = 0
    passed = 0
    for row in bookable_future[:500]:
        target = {
            "id": "independent-live-shadow",
            "label": row["movie_name"],
            "theater_code": theater_code,
            "movie_aliases": [row["movie_name"]],
            "screen_keywords": [],
            "min_remaining_seats": 1,
            "require_sale_open": True,
        }
        try:
            sessions = extract_public_sessions([row], target, row["play_date"])
            ok = (len(sessions) == 1
                  and str(row["schedule_id"]) in str(sessions[0].get("_key", ""))
                  and sessions[0].get("_sale_open_evidence") is True
                  and sessions[0].get("RemainingSeats") == row["remaining_seats"]
                  and sessions[0].get("TotalSeats") == row["total_seats"])
        except (KeyError, TypeError, ValueError):
            ok = False
        if ok:
            passed += 1
        else:
            rejected += 1
    return {
        "status": ("rejected" if rejected else "passed" if passed else "no_bookable_future_sample"),
        "available_future_rows": len(bookable_future),
        "checked_rows": min(len(bookable_future), 500),
        "passed_rows": passed,
        "rejected_rows": rejected,
        "priority_rows": priority_rows,
        "truncated": len(bookable_future) > 500,
        "no_notifications_or_state_writes": True,
    }
