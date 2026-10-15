"""Isolated independent shadow audit tests: no network or Discord."""

from datetime import datetime
from zoneinfo import ZoneInfo
import unittest

from shadow_fallback_validation import audit_rows


class ShadowAuditTests(unittest.TestCase):
    now = datetime(2026, 10, 15, 20, 0, tzinfo=ZoneInfo("Asia/Seoul"))

    def row(self, day="20261016", remaining=42):
        return {
            "movie_name": "샘플 영화", "theater_code": "0128",
            "movie_code": "30000001", "play_date": day,
            "start_time": "10:20", "schedule_id": f"{day}012801",
            "remaining_seats": remaining, "total_seats": 100,
        }

    def good_extractor(self, rows, target, day):
        row = rows[0]
        return [{
            "_key": f"0128|{day}|{target['id']}|public|30000001|{row['schedule_id']}|10:20",
            "_sale_open_evidence": True,
            "RemainingSeats": row["remaining_seats"],
            "TotalSeats": row["total_seats"],
        }]

    def test_passes_future_contract(self):
        result = audit_rows([self.row()], self.now, self.good_extractor)
        self.assertEqual(result["status"], "passed")
        self.assertEqual((result["checked_rows"], result["passed_rows"]), (1, 1))
        self.assertTrue(result["no_notifications_or_state_writes"])

    def test_empty_or_unbookable_is_not_confirmed_absence(self):
        data = [self.row("20261014"), self.row(remaining=0)]
        result = audit_rows(data, self.now, self.good_extractor)
        self.assertEqual(result["status"], "no_bookable_future_sample")
        self.assertEqual(result["available_future_rows"], 0)

    def test_rejects_session_parser_mismatch(self):
        result = audit_rows([self.row()], self.now, lambda *args: [])
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["rejected_rows"], 1)

    def test_priority_date_sample_count(self):
        result = audit_rows([self.row("20261218")], self.now, self.good_extractor)
        self.assertEqual(result["priority_rows"], 1)
        self.assertEqual(result["status"], "passed")


if __name__ == "__main__":
    unittest.main()
