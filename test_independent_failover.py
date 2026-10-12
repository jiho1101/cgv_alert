"""Production pipeline recovery simulation. No network, secrets, or live Discord."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import checker


class IndependentFailoverTests(unittest.TestCase):
    def test_live_primary_502_independent_alert_once_even_after_seat_change(self):
        target_id = "avengers-doomsday-ulsan-samsan"
        target = {
            "id": target_id,
            "enabled": True,
            "label": "어벤져스: 둠스데이",
            "theater_code": "0128",
            "theater_name": "CGV 울산삼산",
            "date_range": {"start": "20991218", "end": "20991218"},
            "movie_aliases": ["어벤져스: 둠스데이"],
            "screen_keywords": [],
            "require_sale_open": True,
            "min_remaining_seats": 1,
            "scan_strategy": {
                "priority_range": {"start": "20991218", "end": "20991218"},
                "priority_interval_minutes": 5,
                "full_interval_minutes": 30,
            },
        }
        public_row = {
            "movie_code": "30009999",
            "movie_name": "어벤져스: 둠스데이",
            "theater_code": "0128",
            "theater_name": "CGV울산삼산",
            "play_date": "20991218",
            "start_time": "10:20",
            "end_time": "13:15",
            "schedule_id": "2099121801281",
            "screen": "IMAX관",
            "remaining_seats": 439,
            "total_seats": 440,
        }
        failed_primary = {
            "accepted": False,
            "status": 502,
            "state": "invalid_structure",
            "error": "simulated upstream HTTP 502",
            "state_changed": False,
            "guard_state_changed": False,
            "rows": [],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "config.json"
            state_path = Path(tmp) / "state.json"
            runtime_path = Path(tmp) / "runtime_status.json"
            config_path.write_text(
                json.dumps({"targets": [target], "request": {"timeout_seconds": 15}}),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"version": 2, "seen": {}, "fallback_seen": {}}),
                encoding="utf-8",
            )
            snapshot = {
                "rows": [public_row],
                "snapshot_age_seconds": 900,
                "source_age_seconds": 3600,
            }
            with (
                patch.object(checker, "CONFIG_PATH", config_path),
                patch.object(checker, "STATE_PATH", state_path),
                patch.object(checker, "RUNTIME_STATUS_PATH", runtime_path),
                patch.object(checker, "planned_dates", return_value=["20991218"]),
                patch.object(checker, "_fetch_priority_public_with_retry", return_value=failed_primary),
                patch.object(checker, "read_cinema_snapshot", return_value=snapshot),
                patch.object(checker, "merge_emergency_seen_from_worker", return_value=False),
                patch.object(checker, "send_discord_embeds", return_value=True) as send,
                patch.object(checker, "send_health_message", return_value=True),
                patch.object(checker, "CgvBrowser", side_effect=AssertionError("unexpected browser")),
            ):
                checker.run_checker()
                self.assertEqual(send.call_count, 1)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(len(state["seen"][target_id]), 1)
                self.assertIn("|public|30009999|2099121801281|10:20", state["seen"][target_id][0])
                status = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(status["targets"][0]["detection_mode"], "independent_static")
                self.assertIn(status["targets"][0]["health_status"], ("fallback", "warning"))

                public_row["remaining_seats"] = 1
                checker.run_checker()
                self.assertEqual(send.call_count, 1, "Seat change must not send duplicate alerts")
                after = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(len(after["seen"][target_id]), 1)


    def test_http_200_primary_missing_movie_crosschecks_independent_once(self):
        """Primary HTTP 200 is not proof its timetable includes every new session."""
        target_id = "avengers-doomsday-ulsan-samsan"
        target = {
            "id": target_id,
            "enabled": True,
            "label": "어벤져스: 둠스데이",
            "theater_code": "0128",
            "theater_name": "CGV 울산삼산",
            "date_range": {"start": "20991218", "end": "20991218"},
            "movie_aliases": ["어벤져스: 둠스데이"],
            "screen_keywords": [],
            "require_sale_open": True,
            "min_remaining_seats": 1,
            "scan_strategy": {
                "priority_range": {"start": "20991218", "end": "20991218"},
                "priority_interval_minutes": 5,
                "full_interval_minutes": 30,
            },
        }
        independent_movie = {
            "movie_code": "30009999", "movie_name": "어벤져스: 둠스데이",
            "theater_code": "0128", "theater_name": "CGV 울산삼산",
            "play_date": "20991218", "start_time": "10:20",
            "schedule_id": "2099121801281", "screen": "IMAX",
            "total_seats": 440, "remaining_seats": 439,
        }
        primary_other_movie = {
            **independent_movie, "movie_code": "30008888",
            "movie_name": "다른 영화", "schedule_id": "2099121801282",
        }
        accepted_primary = {
            "accepted": True, "status": 200, "state": "nonempty_trusted",
            "rows": [primary_other_movie], "empty_unconfirmed": False,
            "state_changed": False, "guard_state_changed": False,
            "inspection": {"rows": 1, "identity_collisions": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, state_path = root / "config.json", root / "state.json"
            runtime_path = root / "runtime_status.json"
            config_path.write_text(
                json.dumps({"targets": [target], "request": {"timeout_seconds": 15}}),
                encoding="utf-8",
            )
            state_path.write_text(
                json.dumps({"version": 2, "seen": {}, "fallback_seen": {}}),
                encoding="utf-8",
            )
            snapshot = {
                "rows": [independent_movie],
                "snapshot_age_seconds": 700,
                "source_age_seconds": 900,
            }
            with (
                patch.object(checker, "CONFIG_PATH", config_path),
                patch.object(checker, "STATE_PATH", state_path),
                patch.object(checker, "RUNTIME_STATUS_PATH", runtime_path),
                patch.object(checker, "planned_dates", return_value=["20991218"]),
                patch.object(checker, "_fetch_priority_public_with_retry",
                             return_value=accepted_primary),
                patch.object(checker, "read_cinema_snapshot",
                             return_value=snapshot) as fetch_independent,
                patch.object(checker, "merge_emergency_seen_from_worker",
                             return_value=False),
                patch.object(checker, "send_discord_embeds",
                             return_value=True) as send,
                patch.object(checker, "send_health_message", return_value=True),
                patch.object(checker, "CgvBrowser",
                             side_effect=AssertionError("unexpected browser")),
            ):
                checker.run_checker()
                status = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    status["targets"][0]["detection_mode"],
                    "primary_independent_combined",
                )
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(len(state["seen"][target_id]), 1)
                self.assertEqual(send.call_count, 1)
                self.assertEqual(fetch_independent.call_count, 1)
                independent_movie["remaining_seats"] = 1
                checker.run_checker()
                self.assertEqual(send.call_count, 1, "Seat changes must not re-alert")

                # Even HTTP 200 with [] is not proof that the priority movie
                # is absent: a different new session may appear on the
                # independent feed before the primary publishes it.
                accepted_primary.update({
                    "rows": [], "state": "empty_unconfirmed",
                    "empty_unconfirmed": True,
                })
                independent_movie["schedule_id"] = "2099121801283"
                with patch.object(
                    checker, "_public_sentinel_should_check", return_value=False
                ):
                    checker.run_checker()
                self.assertEqual(send.call_count, 2)
                latest = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(len(latest["seen"][target_id]), 2)


    def test_nonpriority_dates_continue_during_primary_backoff_without_primary_requests(self):
        """5xx backoff must never suppress independent monitoring on due dates."""
        play_date = "20991219"
        target_id = "avengers-doomsday-ulsan-samsan"
        target = {
            "id": target_id, "enabled": True,
            "label": "어벤져스: 둠스데이",
            "theater_code": "0128", "theater_name": "CGV 울산삼산",
            "date_range": {"start": play_date, "end": play_date},
            "movie_aliases": ["어벤져스: 둠스데이"],
            "screen_keywords": [], "require_sale_open": True,
            "min_remaining_seats": 1,
            "scan_strategy": {
                "priority_range": {"start": "20991218", "end": "20991218"},
                "priority_interval_minutes": 5, "full_interval_minutes": 30,
            },
        }
        show = {
            "movie_code": "30009999", "movie_name": "어벤져스: 둠스데이",
            "theater_code": "0128", "theater_name": "CGV 울산삼산",
            "play_date": play_date, "start_time": "13:20",
            "schedule_id": "2099121901281", "screen": "IMAX",
            "total_seats": 300, "remaining_seats": 299,
        }
        future = (checker.datetime.now(checker.KST) +
                  checker.timedelta(hours=2)).isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, state_path = root / "config.json", root / "state.json"
            runtime_path = root / "runtime_status.json"
            config_path.write_text(
                json.dumps({"targets": [target], "request": {"timeout_seconds": 15}}),
                encoding="utf-8",
            )
            state_path.write_text(json.dumps({
                "version": 2, "seen": {}, "fallback_seen": {},
                "public_api_guard": {
                    "version": 1, "failure_count": 1, "last_status": 502,
                    "backoff_until": future,
                },
            }), encoding="utf-8")
            snapshot = {"rows": [show], "snapshot_age_seconds": 600,
                        "source_age_seconds": 900}
            with (
                patch.object(checker, "CONFIG_PATH", config_path),
                patch.object(checker, "STATE_PATH", state_path),
                patch.object(checker, "RUNTIME_STATUS_PATH", runtime_path),
                patch.object(checker, "planned_dates", return_value=[play_date]),
                patch.object(checker, "_production_public_request",
                             side_effect=AssertionError("Primary API must stay idle")),
                patch.object(checker, "read_cinema_snapshot",
                             return_value=snapshot) as independent,
                patch.object(checker, "merge_emergency_seen_from_worker",
                             return_value=False),
                patch.object(checker, "send_discord_embeds",
                             return_value=True) as send,
                patch.object(checker, "send_health_message", return_value=True),
                patch.object(checker, "CgvBrowser",
                             side_effect=AssertionError("No blocked CGV browser")),
            ):
                checker.run_checker()
                self.assertEqual(send.call_count, 1)
                self.assertEqual(independent.call_count, 1)
                runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
                self.assertEqual(runtime["targets"][0]["detection_mode"],
                                 "independent_static")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(len(state["seen"][target_id]), 1)
                show["remaining_seats"] = 1
                checker.run_checker()
                self.assertEqual(send.call_count, 1, "No duplicate on seat change")


if __name__ == "__main__":
    unittest.main()
