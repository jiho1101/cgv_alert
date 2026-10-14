"""New-source boundary/pipeline tests; every packet below is synthetic.

These tests establish readiness, NOT real CGV collection or permission.
"""
import copy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

import checker
from verified_feed import FeedError, read_verified_feed, validate_packet

NOW = datetime(2026, 10, 14, 3, 50, tzinfo=timezone.utc)


def packet():
    row = {"theater_code": "0128", "play_date": "20261014", "movie_code": "30001323",
           "movie_name": "오디세이", "screen_no": "009", "screen_name": "IMAX관[9관]",
           "sequence": "2", "start_time": "13:30", "total_seats": 441,
           "remaining_seats": 429, "bookable": True}
    watched = dict(row, play_date="20261218", movie_name="어벤져스: 둠스데이", movie_code="TEST-MOVIE")
    return {"schema_version": 1, "source_id": "synthetic-test", "origin": "cgv_first_party",
            "authorization_reference": "TEST-ONLY-NO-AUTHORIZATION",
            "theater_code": "0128", "theater_name": "CGV 울산삼산",
            "observed_at": (NOW - timedelta(seconds=15)).isoformat(),
            "published_at": NOW.isoformat(), "complete_dates": ["20261014", "20261218"],
            "rows": [row, watched]}


def validate(data):
    return validate_packet(data, "0128", NOW, "synthetic-test", "TEST-ONLY-NO-AUTHORIZATION")


class VerifiedFeedTests(unittest.TestCase):
    def test_disabled_makes_no_requests(self):
        with patch.dict("os.environ", {}, clear=True):
            session = Mock()
            self.assertIsNone(read_verified_feed("0128", NOW, session))
            session.get.assert_not_called()

    def test_full_identity_and_observation(self):
        result = validate(packet())
        row = result["rows"][1]
        self.assertEqual(row["screen_no"], "009")
        self.assertEqual(row["schedule_id"], "2026121801282")
        self.assertEqual(result["source_age_seconds"], 15)

    def test_stale_or_fabricated_future_observation_rejected(self):
        for at in [NOW - timedelta(seconds=301), NOW + timedelta(seconds=31)]:
            data = packet()
            data["observed_at"] = at.isoformat()
            with self.assertRaises(FeedError):
                validate(data)

    def test_identity_seats_status_and_coverage_fail_closed(self):
        for field, value in [("theater_code", "0013"), ("movie_code", ""), ("screen_no", ""),
                             ("sequence", ""), ("start_time", "13:99"), ("total_seats", 0),
                             ("remaining_seats", 442), ("remaining_seats", True),
                             ("bookable", "true"), ("play_date", "20260230")]:
            data = packet()
            data["rows"][0][field] = value
            with self.subTest(field=field), self.assertRaises(FeedError):
                validate(data)

    def test_duplicate_and_ambiguous_legacy_key_rejected(self):
        for duplicate in [packet()["rows"][0], dict(packet()["rows"][0], screen_no="008")]:
            data = packet()
            data["rows"].append(duplicate)
            with self.assertRaises(FeedError):
                validate(data)

    def test_future_only_empty_or_no_bookable_sentinel_rejected(self):
        for rows in [[], [packet()["rows"][1]], [dict(packet()["rows"][0], bookable=False)]]:
            data = packet()
            data["rows"] = rows
            with self.assertRaises(FeedError):
                validate(data)

    def test_permission_origin_mismatch_rejected(self):
        for field, value in [("source_id", "wrong"), ("authorization_reference", ""),
                             ("origin", "third_party_unverified")]:
            data = packet()
            data[field] = value
            with self.assertRaises(FeedError):
                validate(data)

    def test_access_limit_no_retries(self):
        env = {"CGV_VERIFIED_FEED_ENABLED": "1", "CGV_VERIFIED_FEED_URL": "https://publisher.example/feed",
               "CGV_VERIFIED_FEED_TOKEN": "test", "CGV_VERIFIED_FEED_SOURCE_ID": "synthetic-test",
               "CGV_VERIFIED_FEED_PERMISSION": "TEST-ONLY-NO-AUTHORIZATION"}
        for status in [403, 429, 302, 502]:
            session = Mock()
            response = Mock(status_code=status, headers={"Retry-After": "3600"})
            session.get.return_value = response
            with patch.dict("os.environ", env, clear=True), self.assertRaises(FeedError) as caught:
                read_verified_feed("0128", NOW, session)
            self.assertEqual(caught.exception.retry_after_seconds, 3600)
            self.assertEqual(session.get.call_count, 1)
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
            response.close.assert_called_once()

    def test_controlled_session_positive_seats_not_bookable(self):
        data = packet()
        data["rows"][1]["bookable"] = False
        rows = validate(data)["rows"]
        target = {"id": "watch", "theater_code": "0128", "movie_aliases": ["어벤져스: 둠스데이"],
                  "require_sale_open": True}
        self.assertEqual(checker.extract_public_sessions(rows, target, "20261218"), [])

    def test_pipeline_new_session_seat_change_duplicate_and_existing_alias(self):
        target = {"id": "watch", "label": "어벤져스: 둠스데이", "theater_code": "0128",
                  "theater_name": "CGV 울산삼산", "date": "20261218", "enabled": True,
                  "movie_aliases": ["어벤져스: 둠스데이"], "require_sale_open": True}
        feed = validate(packet())
        with tempfile.TemporaryDirectory() as td:
            config, state, runtime = [Path(td) / n for n in ["config.json", "state.json", "runtime.json"]]
            config.write_text(json.dumps({"targets": [target]}))
            state.write_text(json.dumps({"version": 2, "seen": {}, "fallback_seen": {}}))
            with patch.object(checker, "CONFIG_PATH", config), patch.object(checker, "STATE_PATH", state), \
                 patch.object(checker, "RUNTIME_STATUS_PATH", runtime), \
                 patch.object(checker, "verified_feed_enabled", return_value=True), \
                 patch.object(checker, "read_verified_feed", return_value=feed), \
                 patch.object(checker, "_fetch_priority_public_with_retry", side_effect=AssertionError("old source called")), \
                 patch.object(checker, "read_cinema_snapshot", side_effect=AssertionError("old feed called")), \
                 patch.object(checker, "merge_emergency_seen_from_worker", return_value=False), \
                 patch.object(checker, "send_health_message", return_value=True), \
                 patch.object(checker, "send_discord_embeds", return_value=True) as send:
                checker.run_checker()
                self.assertEqual(send.call_count, 1)
                actual = json.loads(runtime.read_text())
                self.assertEqual(actual["collection"]["outcome"], "collected")
                self.assertEqual(actual["collection_pages"][0]["source_id"], "synthetic-test")
                self.assertEqual(len(json.loads(state.read_text())["seen"]["watch"]), 1)
                feed["rows"][1]["remaining_seats"] = 1
                checker.run_checker()
                self.assertEqual(send.call_count, 1)
                # Prior official-format key is retained as an alias, not deleted.
                legacy = "0128|20261218|watch|IMAX관[9관]|13:30"
                state.write_text(json.dumps({"version": 2, "seen": {"watch": [legacy]}, "fallback_seen": {}}))
                checker.run_checker()
                self.assertEqual(send.call_count, 1)
                self.assertIn(legacy, json.loads(state.read_text())["seen"]["watch"])


if __name__ == "__main__":
    unittest.main()
