import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from source_transition_canary import (
    choose_canary_date,
    qualify_canary_response,
    run_canary,
    STATE_KEY,
)

TZ = ZoneInfo("Asia/Seoul")
START = datetime(2026, 11, 1, 22, 0, tzinfo=TZ)
DAY = (START + timedelta(days=8)).strftime("%Y%m%d")
ROW = {
    "movieCode": "30009999",
    "movieName": "가상 신작",
    "theaterCode": "0128",
    "theaterName": "CGV 울산삼산",
    "playDate": DAY,
    "startTime": "10:20",
    "scheduleId": DAY + "01281",
    "totalSeats": 200,
    "remainingSeats": 199,
}
SUCCESS = {"data": [ROW]}


class TransitionCanaryTests(unittest.TestCase):
    def test_unavailable_to_published_transition(self):
        state = {}
        calls = []

        def request(theater, day):
            calls.append((theater, day))
            if len(calls) == 1:
                return 502, .1, {"error": "upstream failed"}, None
            return 200, .2, copy.deepcopy(SUCCESS), None

        first = run_canary(state, START, request)
        self.assertEqual(first["http_status"], 502)
        self.assertEqual(first["date"], DAY)
        self.assertFalse(first["qualified"])
        self.assertEqual(
            run_canary(state, START + timedelta(minutes=5), request),
            {"skipped": "not_due"},
        )
        second = run_canary(state, START + timedelta(minutes=10), request)
        self.assertTrue(second["qualified"])
        self.assertTrue(second["transition_from_unqualified"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(state[STATE_KEY]["history"]), 2)
        self.assertEqual(
            run_canary(state, START + timedelta(minutes=20), request),
            {"skipped": "transition_already_observed"},
        )

    def test_empty_and_wrong_theater_not_qualified(self):
        self.assertEqual(
            qualify_canary_response(200, {"data": []}, None, "0128", DAY)[0],
            False,
        )
        wrong = copy.deepcopy(ROW)
        wrong["theaterCode"] = "0013"
        self.assertEqual(
            qualify_canary_response(200, {"data": [wrong]}, None, "0128", DAY)[2],
            "identity_mismatch",
        )

    def test_bad_seats_and_duplicate_identity_rejected(self):
        bad = copy.deepcopy(ROW)
        bad["remainingSeats"] = 201
        self.assertEqual(
            qualify_canary_response(200, {"data": [bad]}, None, "0128", DAY)[0],
            False,
        )
        self.assertEqual(
            qualify_canary_response(200, {"data": [ROW, ROW]}, None, "0128", DAY)[0],
            False,
        )

    def test_rate_limit_respected(self):
        state = {
            "public_api_guard": {
                "last_status": 429,
                "backoff_until": (START + timedelta(minutes=30)).isoformat(),
            },
        }
        self.assertEqual(
            run_canary(state, START, lambda *args: self.fail("429 retry attempted")),
            {"skipped": "supplier_429_backoff"},
        )
        self.assertNotIn(STATE_KEY, state)

    def test_canary_429_sets_own_backoff_without_raising_global_guard(self):
        state = {}
        first = run_canary(state, START, lambda *_: (429, .1, {}, None))
        self.assertEqual(first["http_status"], 429)
        self.assertEqual(
            run_canary(
                state, START + timedelta(minutes=15),
                lambda *_: self.fail("canary must not retry HTTP 429"),
            ),
            {"skipped": "canary_429_backoff"},
        )
        self.assertNotIn("public_api_guard", state)

    def test_date_rolls_only_when_near(self):
        state = {STATE_KEY: {"date": DAY}}
        self.assertEqual(choose_canary_date(state, START), DAY)
        later = START + timedelta(days=7)
        self.assertEqual(
            choose_canary_date(state, later),
            (later + timedelta(days=8)).strftime("%Y%m%d"),
        )


if __name__ == "__main__":
    unittest.main()
