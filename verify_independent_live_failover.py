"""End-to-end LIVE independent feed with forced primary 502.

Only a temporary state, synthetic target derived from TODAY'S actual movie and
mocked Discord are used. Never sends Discord, mutates production state, or
accesses CGV official restricted pages.
"""
from collections import Counter
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo
import json
import tempfile

import checker
from moviebara_fallback import read_cinema_snapshot

now = datetime.now(ZoneInfo("Asia/Seoul"))
play_date = now.strftime("%Y%m%d")
snapshot = read_cinema_snapshot("0128", now)
today_rows = [
    row for row in snapshot["rows"]
    if row["play_date"] == play_date and 0 < row["remaining_seats"] <= row["total_seats"]
]
if not today_rows:
    raise SystemExit("LIVE_502_SMOKE_NOT_QUALIFIED: no real bookable today rows")
first = today_rows[0]
movie_code = first["movie_code"]
movie_title = first["movie_name"]
target_id = "offline-test-" + movie_code
movie_rows = [r for r in today_rows if r["movie_code"] == movie_code]
target = {
    "id": target_id, "enabled": True, "label": movie_title,
    "theater_code": "0128", "theater_name": "CGV 울산삼산",
    "date_range": {"start": play_date, "end": play_date},
    "movie_aliases": [movie_title], "screen_keywords": [],
    "require_sale_open": True, "min_remaining_seats": 1,
    "scan_strategy": {
        "priority_range": {"start": play_date, "end": play_date},
        "priority_interval_minutes": 5, "full_interval_minutes": 30,
    },
}
# Apply rejection checks to an ACTUAL published row, not a fictional session.
from collection_outcome import page_outcome
valid = checker.extract_public_sessions([first], target, play_date, pinned_movie_code=movie_code)
if len(valid) != 1:
    raise AssertionError("live row was rejected by production extractor")
for field, value in [
    ("movie_code", "WRONG"), ("theater_code", "9999"),
    ("play_date", "20991218"), ("schedule_id", ""),
    ("schedule_id", play_date + "0128"),
    ("total_seats", 0), ("remaining_seats", first["total_seats"] + 1),
]:
    if checker.extract_public_sessions([dict(first, **{field: value})], target,
                                      play_date, pinned_movie_code=movie_code):
        raise AssertionError(f"invalid live-row variant accepted: {field}")
with patch.object(checker, "_production_public_request",
                  return_value=(200, .1, {"data": {"timetable": []}}, None, None)):
    empty = checker._fetch_public_page({}, "0128", "20991218", now)
    if not empty["empty_unconfirmed"] or page_outcome({
        "ok": empty["accepted"], "row_count": 0, "source_mode": "public_primary",
        "empty_unconfirmed": empty["empty_unconfirmed"],
    }) == "collected":
        raise AssertionError("empty future response promoted to collection success")

failed_primary = {
    "accepted": False, "status": 502, "state": "invalid_structure",
    "error": "simulated upstream HTTP 502", "state_changed": False,
    "guard_state_changed": False, "rows": [],
}
with tempfile.TemporaryDirectory() as tempdir:
    root = Path(tempdir)
    config_path = root / "config.json"
    state_path = root / "state.json"
    runtime_path = root / "runtime_status.json"
    config_path.write_text(
        json.dumps({"targets": [target], "request": {"timeout_seconds": 15}},ensure_ascii=False),
        encoding="utf-8",
    )
    state_path.write_text(
        json.dumps({"version":2,"seen":{},"fallback_seen":{}}),
        encoding="utf-8",
    )
    with (
        patch.object(checker, "CONFIG_PATH", config_path),
        patch.object(checker, "STATE_PATH", state_path),
        patch.object(checker, "RUNTIME_STATUS_PATH", runtime_path),
        patch.object(checker, "planned_dates", return_value=[play_date]),
        patch.object(checker, "_fetch_priority_public_with_retry", return_value=failed_primary),
        patch.object(checker, "read_cinema_snapshot", return_value=snapshot),
        patch.object(checker, "merge_emergency_seen_from_worker", return_value=False),
        patch.object(checker, "send_discord_embeds", return_value=True) as sends,
        patch.object(checker, "send_health_message", return_value=True),
        patch.object(checker, "CgvBrowser", side_effect=AssertionError("fallback should not launch browser")),
        patch.object(checker, "record_shadow", return_value=False),
    ):
        sends.side_effect = RuntimeError("simulated Discord transport failure")
        try:
            checker.run_checker()
        except RuntimeError as exc:
            if "simulated Discord" not in str(exc):
                raise
        else:
            raise AssertionError("expected transport failure")
        after_failed_send = json.loads(state_path.read_text(encoding="utf-8"))
        if (after_failed_send.get("seen") or {}).get(target_id):
            raise AssertionError("seen saved before successful Discord delivery")
        sends.side_effect = None
        sends.reset_mock()
        checker.run_checker()
        after_first = json.loads(state_path.read_text(encoding="utf-8"))
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        keys = (after_first.get("seen") or {}).get(target_id, [])
        if sends.call_count != 1 or not keys:
            raise AssertionError("first independent live fallback failed to send mock alert")
        if runtime["targets"][0]["detection_mode"] != "independent_static":
            raise AssertionError("independent path was not selected")
        if any("|public|" not in key for key in keys):
            raise AssertionError("cross-source dedup key incorrect")

        # Changing ONLY seat quantity must not produce another alert.
        import copy
        changed_snapshot = copy.deepcopy(snapshot)
        for row in changed_snapshot["rows"]:
            if row["movie_code"] == movie_code and row["play_date"] == play_date:
                row["remaining_seats"] = 1
        with patch.object(checker,"read_cinema_snapshot",return_value=changed_snapshot):
            checker.run_checker()
        after_second = json.loads(state_path.read_text(encoding="utf-8"))
        if sends.call_count != 1 or after_second["seen"][target_id] != keys:
            raise AssertionError("seat change triggered duplicate notification")

print("INDEPENDENT_LIVE_502_FAILOVER_PASS",json.dumps({
    "date":play_date,"movie_code":movie_code,"live_bookable_shows":len(movie_rows),
    "new_seen_keys":len(keys),"mock_discord_calls":sends.call_count,
    "snapshot_age_seconds":snapshot["snapshot_age_seconds"],
    "source_age_seconds":snapshot["source_age_seconds"],
    "production_discord_called":False, "production_state_modified":False,
    "live_identity_rejection_checks":7, "empty_future_unconfirmed":True,
    "failed_discord_did_not_save_seen":True,
},ensure_ascii=False))
