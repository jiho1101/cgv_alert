"""Unprivileged, deterministic tests; no network and no Discord."""
import unittest
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from unittest.mock import Mock

from moviebara_fallback import SnapshotError, read_cinema_snapshot

NOW = datetime(2026, 10, 12, 1, 40, tzinfo=timezone.utc)
ROW = {
    "provider": "CGV", "cinema_code": "0128", "cinema_name": "CGV울산삼산",
    "movie_title": "어벤져스: 둠스데이", "source_movie_code": "30009999",
    "play_date": "2026-12-18", "start_dt": "10:20", "end_dt": "13:15",
    "screen_name": "IMAX관", "total_seat_cnt": 440, "remain_seat_cnt": 439,
    "url": "https://cgv.co.kr/cnm/movieBook/movie?movNo=30009999&siteNo=0128&scnYmd=20261218&scnsNo=009&scnSseq=1",
}


def mock_feed(rows, generated=None, source_updated=None, malformed_digest=False):
    snap = {"cinema": {"cinema_code": "0128", "provider": "CGV"}, "screenings": rows}
    raw = json.dumps(snap).encode()
    digest = sha256(raw).hexdigest()
    manifest = {
        "schema_version": 1,
        "generated_at": (generated or NOW - timedelta(minutes=20)).isoformat(),
        "source_updated_at": {"screenings": (source_updated or NOW - timedelta(hours=2)).isoformat()},
        "artifacts": {"cinemas/0128.json": {"sha256": digest, "key": f"objects/{digest}.json"}},
    }
    resp1 = Mock(status_code=200, content=json.dumps(manifest).encode())
    resp1.json.return_value = manifest
    resp2 = Mock(status_code=200, content=(b"tampered" if malformed_digest else raw))
    resp2.json.return_value = snap
    session = Mock()
    session.get.side_effect = [resp1, resp2]
    return session


class SnapshotTests(unittest.TestCase):
    def test_exact_primary_identity_and_seats(self):
        result = read_cinema_snapshot("0128", NOW, mock_feed([ROW]))
        self.assertEqual(result["rows"][0]["schedule_id"], "2026121801281")
        self.assertEqual(result["rows"][0]["start_time"], "10:20")
        self.assertEqual(result["rows"][0]["remaining_seats"], 439)
        self.assertEqual(result["rows"][0]["total_seats"], 440)

    def test_seat_change_not_new_identity(self):
        a = read_cinema_snapshot("0128", NOW, mock_feed([ROW]))["rows"][0]
        b = read_cinema_snapshot("0128", NOW, mock_feed([{**ROW, "remain_seat_cnt": 1}]))["rows"][0]
        from shadow_public_monitor import identity_key
        self.assertEqual(identity_key(a), identity_key(b))

    def test_incorrect_cinema_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([{**ROW, "cinema_code": "0056"}]))

    def test_booking_url_mismatch_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([{**ROW, "url": ROW["url"].replace("siteNo=0128", "siteNo=0056")}]))

    def test_untrusted_seats_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([{**ROW, "remain_seat_cnt": 900}]))

    def test_same_screening_duplicate_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([ROW, ROW]))

    def test_stale_manifest_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([ROW], generated=NOW-timedelta(hours=3)))

    def test_stale_source_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([ROW], source_updated=NOW-timedelta(hours=13)))

    def test_digest_mismatch_rejected(self):
        with self.assertRaises(SnapshotError):
            read_cinema_snapshot("0128", NOW, mock_feed([ROW], malformed_digest=True))

    def test_empty_future_not_claimed_bookable(self):
        result = read_cinema_snapshot("0128", NOW, mock_feed([]))
        self.assertEqual(result["rows"], [])


if __name__ == "__main__":
    unittest.main()
