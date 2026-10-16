"""Offline checks for CGV page response envelopes and the publisher bridge."""

import json
import unittest

from cgv_payload_contract import parse_cgv_schedule_payload
from cgv_first_party_publisher import CollectorError, collect_official_rows
from checker import CgvBrowser


def session(**changes):
    row = {
        "siteNo": "0128", "scnYmd": "20261017",
        "movNo": "1234567", "scnsNo": "009", "scnSseq": "2",
        "scnsrtTm": "1330", "movNm": "테스트 영화", "scnsNm": "IMAX관",
        "stcnt": 441, "frSeatCnt": 430,
    }
    row.update(changes)
    return row


class FakeBrowser:
    def __init__(self, outcome):
        self.current_site = "previous"
        self.outcome = outcome
        self.calls = []

    def _bootstrap(self, site, name, date):
        self.calls.append(("bootstrap", site, name, date))

    def _consume_browser_schedule(self, site, date, wait_seconds):
        self.calls.append(("consume", site, date, wait_seconds))
        return self.outcome


class CgvEnvelopeCompatibilityTests(unittest.TestCase):
    def test_simple_list_matches_existing_capture(self):
        body = json.dumps({"statusCode": 0, "data": [session()]})
        self.assertEqual(parse_cgv_schedule_payload(body), [session()])

    def test_wrapped_unique_schedule_list(self):
        body = json.dumps({"statusCode": 0, "data": {
            "siteInfo": [{"siteNm": "울산삼산"}],
            "movieScnInfoList": [session()],
        }})
        self.assertEqual(parse_cgv_schedule_payload(body), [session()])
        self.assertEqual(CgvBrowser._parse_schedule_payload(None, body), [session()])

    def test_unknown_nested_empty_list_is_not_success(self):
        self.assertIsNone(parse_cgv_schedule_payload(
            json.dumps({"data": {"siteInfo": []}})
        ))

    def test_ambiguous_nested_lists_fail_closed(self):
        self.assertIsNone(parse_cgv_schedule_payload(
            json.dumps({"data": {"first": [session()], "second": [session(scnSseq="3")]}})
        ))

    def test_non_schedule_list_not_accepted(self):
        self.assertIsNone(parse_cgv_schedule_payload(
            json.dumps({"data": [{"siteNm": "울산삼산"}]})
        ))

    def test_failed_status_and_invalid_json(self):
        self.assertIsNone(parse_cgv_schedule_payload(
            json.dumps({"statusCode": 403, "data": [session()]})
        ))
        self.assertIsNone(parse_cgv_schedule_payload("<html>403 Forbidden</html>"))

    def test_direct_empty_list_is_only_unconfirmed(self):
        self.assertEqual(parse_cgv_schedule_payload('{"data": []}'), [])

    def test_publisher_passes_site_display_name_without_cgv_prefix(self):
        browser = FakeBrowser({"ok": True, "rows": [session()]})
        rows = collect_official_rows(browser, "0128", "CGV 울산삼산", "20261017")
        self.assertEqual(rows, [session()])
        self.assertIsNone(browser.current_site)
        self.assertEqual(browser.calls, [
            ("bootstrap", "0128", "울산삼산", "20261017"),
            ("consume", "0128", "20261017", 8),
        ])

    def test_restricted_browser_stops_without_second_attempt(self):
        browser = FakeBrowser({"ok": False, "status": 403})
        with self.assertRaisesRegex(CollectorError, "restricted"):
            collect_official_rows(browser, "0128", "CGV 울산삼산", "20261017")
        self.assertEqual(len(browser.calls), 2)

    def test_missing_browser_rows_fail_closed(self):
        browser = FakeBrowser({"ok": True, "rows": {"foo": "bar"}})
        with self.assertRaisesRegex(CollectorError, "shape"):
            collect_official_rows(browser, "0128", "CGV 울산삼산", "20261017")


if __name__ == "__main__":
    unittest.main()
