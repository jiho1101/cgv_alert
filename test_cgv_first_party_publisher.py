"""Offline contract tests for the optional first-party publisher.

No browser, network, Discord, or Production state changes are made.
"""
import unittest
from datetime import datetime, timedelta, timezone

from cgv_first_party_publisher import (
    CollectorError, build_packet, canonicalize, default_dates,
)
from verified_feed import validate_packet


def row(date, **changes):
    item = {
        "siteNo": "0128", "scnYmd": date,
        "movNo": "30000001", "movNm": "테스트 영화",
        "scnsNo": "009", "scnsNm": "IMAX관", "scnSseq": "2",
        "scnsrtTm": "1330", "scnendTm": "1600",
        "frSeatCnt": "430", "stcnt": "441",
        "salYn": "Y", "cntlYn": "N",
    }
    item.update(changes)
    return item


class FirstPartyPublisherTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 10, 16, 13, 25, tzinfo=timezone.utc)
        self.near = "20261017"

    def packet(self, collector=None, dates=None, clock=None):
        collector = collector or (
            lambda site, name, date: [row(date)] if date == self.near else []
        )
        return build_packet(
            collector, "0128", "CGV 울산삼산",
            dates if dates is not None else [self.near, "20261218"],
            "publisher-id", "permission-ref",
            clock=clock or (lambda: self.now),
        )

    def test_accepts_real_schema_sentinel_and_preserves_identity(self):
        packet = self.packet()
        validated = validate_packet(
            packet, "0128", self.now, "publisher-id", "permission-ref"
        )
        self.assertEqual(len(validated["rows"]), 1)
        self.assertEqual(packet["complete_dates"], [self.near])
        self.assertEqual(packet["rows"][0]["sequence"], "2")

    def test_403_stops_with_no_retries(self):
        attempted = []
        def forbidden(site, name, date):
            attempted.append(date)
            raise CollectorError("HTTP 403")
        with self.assertRaisesRegex(CollectorError, "403"):
            self.packet(collector=forbidden)
        self.assertEqual(attempted, [self.near])

    def test_empty_result_is_not_live_success(self):
        with self.assertRaisesRegex(CollectorError, "validation failed"):
            self.packet(collector=lambda *args: [])

    def test_wrong_site_or_date_is_rejected(self):
        for altered in ({"siteNo": "1111"}, {"scnYmd": "20261018"}):
            with self.subTest(altered=altered):
                with self.assertRaises(CollectorError):
                    self.packet(collector=lambda *args: [row(self.near, **altered)],
                                dates=[self.near])

    def test_ambiguous_identity_is_rejected(self):
        with self.assertRaisesRegex(CollectorError, "ambiguous"):
            self.packet(collector=lambda *args: [row(self.near), row(self.near)],
                        dates=[self.near])

    def test_sale_closed_cannot_qualify(self):
        candidate = canonicalize(row(self.near, salYn="N"), "0128", self.near)
        self.assertFalse(candidate["bookable"])
        with self.assertRaisesRegex(CollectorError, "validation failed"):
            self.packet(collector=lambda *args: [row(self.near, salYn="N")],
                        dates=[self.near])

    def test_seat_count_change_does_not_change_identity(self):
        before = canonicalize(row(self.near), "0128", self.near)
        after = canonicalize(row(self.near, frSeatCnt="400"), "0128", self.near)
        for key in ("movie_code", "play_date", "screen_no", "sequence", "start_time"):
            self.assertEqual(before[key], after[key])

    def test_long_sweep_is_rejected(self):
        ticks = iter([self.now, self.now, self.now + timedelta(seconds=241)])
        with self.assertRaisesRegex(CollectorError, "freshness window"):
            self.packet(clock=lambda: next(ticks))

    def test_priority_date_does_not_replace_near_term_sentinel(self):
        self.assertEqual(
            default_dates(self.now, "20261218"),
            ["20261016", "20261017", "20261218"],
        )


if __name__ == "__main__":
    unittest.main()
