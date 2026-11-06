from test_delivery_support import acknowledge_embeds
"""A failed date must not starve a different due date, or duplicate title reads."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import checker


class DueDateCollectionTests(unittest.TestCase):
    def run_case(self, status, retry_after=None, exhausted=False):
        dates = ['20991219', '20991220', '20991221']
        targets = [{'id': title, 'enabled': True, 'label': title,
                    'movie_aliases': [title], 'theater_code': '0128',
                    'theater_name': 'CGV 울산삼산', 'require_sale_open': True,
                    'min_remaining_seats': 1,
                    'date_range': {'start': dates[0], 'end': dates[-1]},
                    'scan_strategy': {'priority_range': {'start': '20991218', 'end': '20991218'}}}
                   for title in ['영화 A', '영화 B']]
        calls = []
        def request(site, day):
            calls.append((site, day))
            if day != dates[-1]:
                return status, .1, {}, 'supplier error', retry_after
            return 200, .1, {'data': [{
                'movieCode': 'M-REAL', 'movieName': '영화 A',
                'theaterCode': site, 'playDate': day, 'startTime': '10:00',
                'scheduleId': day + site + '1', 'totalSeats': 100,
                'remainingSeats': 90}]}, None, None
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, state, runtime = [root/n for n in ['config.json', 'state.json', 'runtime.json']]
            config.write_text(json.dumps({'targets': targets}))
            state.write_text(json.dumps({'version': 2, 'seen': {'preserved': ['existing']},
                                         'public_api_guard': {'last_status': 502, 'failure_count': 8,
                                          'backoff_until': '2099-12-31T00:00:00+09:00'}}))
            with (patch.object(checker.time, 'monotonic', side_effect=[0] + [151 if exhausted else 1] * 100),
                  patch.object(checker, 'CONFIG_PATH', config),
                  patch.object(checker, 'STATE_PATH', state),
                  patch.object(checker, 'RUNTIME_STATUS_PATH', runtime),
                  patch.object(checker, 'planned_dates', return_value=dates),
                  patch.object(checker, '_production_public_request', side_effect=request),
                  patch.object(checker, 'read_cinema_snapshot', return_value={'rows': []}),
                  patch.object(checker, 'merge_emergency_seen_from_worker', return_value=False),
                  patch.object(checker, 'record_shadow', return_value=False),
                  patch.object(checker, 'send_health_message', return_value=False),
                  patch.object(checker, 'send_discord_embeds', side_effect=acknowledge_embeds) as send,
                  patch.object(checker, 'CgvBrowser', side_effect=AssertionError('No official access'))):
                checker.run_checker()
                final = json.loads(state.read_text())
                self.assertEqual(final['seen']['preserved'], ['existing'])
                return calls, send.call_count, final

    def test_each_due_date_checked_once_for_two_movies_during_502(self):
        calls, sends, state = self.run_case(502)
        self.assertEqual(calls, [('0128', '20991219'), ('0128', '20991220'), ('0128', '20991221')])
        self.assertEqual(sends, 1)
        self.assertEqual(len(state['seen']['영화 A']), 1)

    def test_nonpriority_budget_bounds_timeout_amplification(self):
        calls, sends, _ = self.run_case(502, exhausted=True)
        self.assertEqual(calls, [])
        self.assertEqual(sends, 0)

    def test_429_stops_remaining_dates(self):
        calls, sends, _ = self.run_case(429, '3600')
        self.assertEqual(calls, [('0128', '20991219')])
        self.assertEqual(sends, 0)

    def test_explicit_retry_after_stops_remaining_dates_even_for_503(self):
        calls, sends, _ = self.run_case(503, '600')
        self.assertEqual(calls, [('0128', '20991219')])
        self.assertEqual(sends, 0)


if __name__ == '__main__':
    unittest.main()
