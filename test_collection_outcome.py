"""Regression checks for the actual production failures observed on Oct 14."""
from datetime import datetime
import unittest
from zoneinfo import ZoneInfo
from collection_outcome import page_outcome, summarize, workflow_exit_code, known_supplier_outage, ci_exit_code
import checker


class CollectionOutcomeTests(unittest.TestCase):
    def snapshot(self, results):
        target = {"id": "watch", "theater_code": "0128", "label": "오디세이",
                  "theater_name": "CGV 울산삼산", "date": "20991218"}
        return checker.build_runtime_snapshot({}, {}, [target],
            datetime(2026, 10, 14, 12, 0, tzinfo=ZoneInfo("Asia/Seoul")), results)

    def test_all_failed_is_error_and_nonzero_after_state_save(self):
        status = self.snapshot({"0128|20991218": {"ok": False, "error": "HTTP 502 / 403"}})
        self.assertEqual(status["health_summary"], "error")
        self.assertEqual(status["targets"][0]["health_status"], "error")
        self.assertEqual(workflow_exit_code(status), 1)
        self.assertIsNone(status["last_monitoring_success_at"])

    def test_empty_future_response_is_not_actual_collection_success(self):
        status = self.snapshot({"0128|20991218": {"ok": True, "source_mode": "public_primary",
                                                   "empty_unconfirmed": True, "row_count": 0}})
        self.assertEqual(status["collection"]["outcome"], "unconfirmed")
        self.assertEqual(status["health_summary"], "warning")
        for field in ["last_monitoring_success_at", "last_primary_success_at", "last_cgv_success_at"]:
            self.assertIsNone(status[field])
        self.assertIsNotNone(status["last_response_success_at"])
        self.assertIsNone(status["targets"][0]["priority_session_snapshot_at"])

    def test_one_failed_date_cannot_be_hidden_by_another_success(self):
        result = summarize([{"ok": False}, {"ok": True, "source_mode": "public_primary", "row_count": 2}])
        self.assertEqual(result["outcome"], "partial")
        self.assertEqual(workflow_exit_code({"collection": result}), 1)

    def test_known_upstream_failure_silences_actions_email_but_keeps_error_state(self):
        status = self.snapshot({
            "0128|20991218": {"ok": False, "error": (
                "주 공급자 실패: 제3자 API HTTP 502 공급자 오류; "
                "독립 공급자: SnapshotError: manifest stale (19912s)"
            )}
        })
        self.assertEqual(status["health_summary"], "error")
        self.assertEqual(status["collection"]["outcome"], "failed")
        self.assertEqual(workflow_exit_code(status), 1)
        self.assertTrue(known_supplier_outage(status))
        self.assertEqual(ci_exit_code(status), 0)
        self.assertIsNone(status["last_monitoring_success_at"])

    def test_unrecognized_checker_failure_remains_action_failure(self):
        status = self.snapshot({
            "0128|20991218": {"ok": False, "error": "RuntimeError: broken parser"}
        })
        self.assertFalse(known_supplier_outage(status))
        self.assertEqual(ci_exit_code(status), 1)

    def test_missing_evidence_remains_action_failure(self):
        self.assertFalse(known_supplier_outage({
            "collection": {"outcome": "failed", "failed": 1},
        }))
        self.assertEqual(ci_exit_code({
            "collection": {"outcome": "failed", "failed": 1},
        }), 1)

    def test_not_due_does_not_manufacture_a_failure_or_success(self):
        status = self.snapshot({})
        self.assertEqual(status["collection"]["outcome"], "not_due")
        self.assertEqual(workflow_exit_code(status), 0)
        self.assertIsNone(status["last_monitoring_success_at"])

    def test_third_party_success_is_not_official_cgv_success(self):
        status = self.snapshot({"0128|20991218": {"ok": True, "source_mode": "public_primary", "row_count": 3}})
        self.assertIsNotNone(status["last_primary_success_at"])
        self.assertIsNone(status["last_cgv_success_at"])

    def test_text_candidates_are_unconfirmed(self):
        self.assertEqual(page_outcome({"ok": True, "source_mode": "text_fallback"}), "unconfirmed")

    def test_official_zero_rows_are_unconfirmed(self):
        self.assertEqual(page_outcome({"ok": True, "source_mode": "cgv_official", "row_count": 0}), "unconfirmed")

    def test_public_missing_theater_and_past_session_are_rejected(self):
        target = {"id": "watch", "theater_code": "0128", "movie_aliases": ["오디세이"],
                  "require_sale_open": True}
        row = {"theater_code": "0128", "play_date": "20991218", "movie_code": "30001323",
               "movie_name": "오디세이", "schedule_id": "2099121801282", "start_time": "13:30",
               "total_seats": 441, "remaining_seats": 433}
        self.assertEqual(len(checker.extract_public_sessions([row], target, "20991218")), 1)
        self.assertEqual(checker.extract_public_sessions([dict(row, theater_code="")], target, "20991218"), [])
        old = dict(row, play_date="20201014")
        self.assertEqual(checker.extract_public_sessions([old], target, "20201014"), [])

    def test_exact_identity_and_seat_evidence_required(self):
        target = {"id": "odyssey", "theater_code": "0128", "movie_aliases": ["오디세이"],
                  "require_sale_open": True, "min_remaining_seats": 1}
        # Field values corroborated by the real Oct 14 public moviebara record
        # and the official UI (13:30, IMAX 9, 441 total). Future date is used
        # only for deterministic parsing; this test never sends Discord.
        row = {"siteNo": "0128", "scnYmd": "20991218", "movNo": "30001323",
               "movNm": "오디세이", "scnsNo": "009", "scnSseq": "2", "scnsNm": "IMAX관[9관]",
               "scnsrtTm": "1330", "stcnt": 441, "frSeatCnt": 433}
        first = checker.extract_sessions([row], target, "20991218")
        self.assertEqual(len(first), 1)
        self.assertTrue(first[0]["_sale_open_evidence"])
        for patch in [{"siteNo": "0013"}, {"scnYmd": ""}, {"movNo": ""}, {"scnSseq": ""},
                      {"stcnt": 0}, {"frSeatCnt": 442}, {"frSeatCnt": 0}, {"cntlYn": "Y"},
                      {"salYn": "N"}, {"movNm": "오디세이2", "expoProdNm": "오디세이2(IMAX)"}]:
            with self.subTest(patch=patch):
                self.assertEqual(checker.extract_sessions([dict(row, **patch)], target, "20991218"), [])
        changed = checker.extract_sessions([dict(row, frSeatCnt=1)], target, "20991218")
        self.assertEqual(first[0]["_key"], changed[0]["_key"])


if __name__ == "__main__":
    unittest.main()
