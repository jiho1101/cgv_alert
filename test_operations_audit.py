from datetime import datetime,timezone
import unittest
from operations_audit import summarize
class AuditTests(unittest.TestCase):
    def test_missing_duplicate_failed_and_incomplete_window_are_not_normal(self):
        runs=[{'name':'CGV Alert','event':'workflow_dispatch','created_at':at,'conclusion':outcome}
              for at,outcome in [('2026-11-06T02:30:33Z','success'),('2026-11-06T02:30:34Z','failure'),('2026-11-06T02:40:33Z','success')]]
        r=summarize(runs,datetime(2026,11,6,2,45,tzinfo=timezone.utc))
        self.assertEqual(r['missing_creation_slots'],['2026-11-06T02:35:00+00:00'])
        self.assertEqual(r['extra_runs_in_creation_slots'],1)
        self.assertEqual(r['outcomes']['failure'],1)
        self.assertFalse(r['window_has_24_hours'])
        self.assertEqual(r['verdict'],'review_pending')
    def test_24_hours_metadata_never_claims_delivery_success(self):
        r=summarize([],datetime(2026,11,7,3,tzinfo=timezone.utc))
        self.assertTrue(r['window_has_24_hours'])
        self.assertEqual(r['verdict'],'review_pending')
        self.assertFalse(r['five_minute_open_to_discord_proven'])
