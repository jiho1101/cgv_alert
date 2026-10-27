import copy
import json
from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock
from zoneinfo import ZoneInfo

import checker
from production_shadow import record_shadow


class ProductionShadowTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2099, 10, 28, 7, 1, tzinfo=ZoneInfo("Asia/Seoul"))
        self.row = {
            "movie_code": "30001440", "movie_name": "실제 영화",
            "theater_code": "0128", "play_date": "20991028",
            "start_time": "10:00", "end_time": "11:49",
            "schedule_id": "2099102801281", "total_seats": 441,
            "remaining_seats": 440,
        }
        self.feed = {"0128": {"rows": [self.row],
                     "generated_at": self.now.isoformat(),
                     "source_updated_at": self.now.isoformat()}}

    def test_cached_audit_no_network_no_seen_change_and_30_minute_clock(self):
        state = {"seen": {"watch": ["preserved-key"]}}
        cache = {("0128", "20991028"): {"accepted": True, "status": 200,
                                        "rows": [self.row]}}
        fetch = Mock(side_effect=AssertionError("duplicate supplier request"))
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "shadow.json"
            self.assertTrue(record_shadow(state, self.now, self.feed, cache,
                checker.extract_public_sessions, fetch, path))
            sample = json.loads(path.read_text())
            self.assertEqual(sample["sites"]["0128"]["comparison"]["shared"], 1)
            self.assertFalse(sample["watch_coverage_proven"])
            self.assertEqual(sample["sites"]["0128"]["secondary"]["parser_audit"]["passed_rows"], 1)
            self.assertFalse(record_shadow(state, self.now + timedelta(minutes=5),
                self.feed, cache, checker.extract_public_sessions, fetch, path))
            self.assertTrue(record_shadow(state, self.now + timedelta(minutes=30),
                self.feed, cache, checker.extract_public_sessions, fetch, path))
        self.assertEqual(state["seen"], {"watch": ["preserved-key"]})
        fetch.assert_not_called()

    def test_missing_page_is_fetched_once_and_reused(self):
        fetch = Mock(return_value=(502, .1, {"error": {"code": "CGV_RELAY_FAILED"}}, None, None))
        cache = {}
        with tempfile.TemporaryDirectory() as root:
            record_shadow({}, self.now, self.feed, cache,
                checker.extract_public_sessions, fetch, Path(root) / "shadow.json")
            self.assertIn(("0128", "20991028"), cache)
            sample = json.loads((Path(root) / "shadow.json").read_text())
            self.assertFalse(sample["sites"]["0128"]["primary"]["ok"])
            self.assertIsNone(sample["sites"]["0128"]["comparison"])
        fetch.assert_called_once_with("0128", "20991028")

    def test_composite_id_rejects_missing_sequence_and_wrong_date_or_site(self):
        target = {"id": "watch", "theater_code": "0128", "movie_aliases": ["실제 영화"],
                  "require_sale_open": True, "min_remaining_seats": 1}
        for invalid in ["", "1", "209910280128", "209910280128 ", "2099102901281", "2099102899991"]:
            with self.subTest(schedule_id=invalid):
                row = dict(self.row, schedule_id=invalid)
                self.assertEqual(checker.extract_public_sessions([row], target, "20991028"), [])
        self.assertEqual(len(checker.extract_public_sessions([self.row], target, "20991028")), 1)
        original = checker.extract_public_sessions([self.row], target, "20991028")[0]["_key"]
        changed = dict(self.row, remaining_seats=1)
        self.assertEqual(checker.extract_public_sessions([changed], target, "20991028")[0]["_key"], original)
        for field, value in [("movie_name", "다른 영화"), ("theater_code", "9999")]:
            self.assertEqual(checker.extract_public_sessions([dict(self.row, **{field: value})], target, "20991028"), [])


if __name__ == "__main__":
    unittest.main()
