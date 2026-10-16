"""Fail-closed decoder for CGV booking-page timetable envelopes.

Handles either the simple data-list format or an object wrapping a unique
session list. Never guess between multiple plausible session lists.
"""

import json

_REQUIRED_SESSION_FIELDS = ("movNo", "scnsNo", "scnSseq", "scnsrtTm")


def _looks_like_schedule(rows):
    return (
        isinstance(rows, list)
        and bool(rows)
        and all(
            isinstance(row, dict)
            and all(row.get(field) is not None and str(row[field]).strip()
                    for field in _REQUIRED_SESSION_FIELDS)
            for row in rows
        )
    )


def parse_cgv_schedule_payload(body):
    try:
        payload = json.loads(str(body or ""))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("statusCode") not in (None, 0, "0"):
        return None

    data = payload.get("data")
    if isinstance(data, list):
        # A direct empty list is allowed as an *unconfirmed* response only.
        # The feed validator still rejects an all-empty source as healthy.
        return data if not data or _looks_like_schedule(data) else None

    if not isinstance(data, dict):
        return None
    candidates = [value for value in data.values()
                  if _looks_like_schedule(value)]
    if len(candidates) != 1:
        # Do not promote unknown empty nested lists or ambiguous combinations.
        return None
    return candidates[0]
