"""Transport receipts persist only the sessions in confirmed Discord messages."""
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import checker


class DeliveryReceiptTests(unittest.TestCase):
    def test_partial_delivery_retries_only_unsent_date(self):
        days = ['20991218', '20991219']
        target = {'id': 'watch', 'enabled': True, 'label': '영화 A',
                  'movie_aliases': ['영화 A'], 'theater_code': '0128',
                  'theater_name': 'CGV 울산삼산', 'require_sale_open': True,
                  'min_remaining_seats': 1,
                  'date_range': {'start': days[0], 'end': days[1]},
                  'scan_strategy': {'priority_range': {'start': days[0], 'end': days[0]}}}
        def request(site, day):
            return 200, .1, {'data': [{'movieCode': 'M1', 'movieName': '영화 A',
                'theaterCode': site, 'playDate': day, 'startTime': '10:00',
                'scheduleId': day + site + '1', 'totalSeats': 100,
                'remainingSeats': 90}]}, None, None
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config, state, runtime = [root/n for n in ['config.json','state.json','runtime.json']]
            config.write_text(json.dumps({'targets':[target]}))
            state.write_text(json.dumps({'version':2,'seen':{},'fallback_seen':{}}))
            with (patch.dict(os.environ, {'DISCORD_WEBHOOK_URL':'https://discord.com/api/webhooks/isolated/not-real'}),
                  patch.object(checker,'CONFIG_PATH',config),
                  patch.object(checker,'STATE_PATH',state),
                  patch.object(checker,'RUNTIME_STATUS_PATH',runtime),
                  patch.object(checker,'planned_dates',return_value=days),
                  patch.object(checker,'_production_public_request',side_effect=request),
                  patch.object(checker,'read_cinema_snapshot',return_value={'rows':[]}),
                  patch.object(checker,'merge_emergency_seen_from_worker',return_value=False),
                  patch.object(checker,'record_shadow',return_value=False),
                  patch.object(checker,'send_health_message',return_value=False),
                  patch.object(checker.requests,'post',side_effect=[SimpleNamespace(status_code=204),SimpleNamespace(status_code=500)]) as post):
                with self.assertRaisesRegex(RuntimeError,'HTTP 500'):
                    checker.run_checker()
                first = json.loads(state.read_text())['seen']['watch']
                self.assertEqual(len(first),1)
                self.assertIn(days[0],first[0])
                for call in post.call_args_list:
                    embed=call.kwargs['json']['embeds'][0]
                    self.assertNotIn('_delivery',embed)
                    self.assertNotIn('_fallback',embed)
                    self.assertTrue(all('_rows' not in f for f in embed['fields']))
                post.reset_mock()
                post.side_effect=None
                post.return_value=SimpleNamespace(status_code=204)
                checker.run_checker()
                self.assertEqual(post.call_count,1)
                self.assertIn('2099-12-19',post.call_args.kwargs['json']['embeds'][0]['description'])
                self.assertEqual(len(json.loads(state.read_text())['seen']['watch']),2)
                checker.run_checker()
                self.assertEqual(post.call_count,1)

    def test_large_message_receipts_cover_only_displayed_rows(self):
        rows=[{'_key':str(i),'PlayStartTm':'1000','PlayYmd':'20991218',
               'ScreenNm':f'관{i}', '_sale_open_evidence':True} for i in range(45)]
        item={'target_id':'watch','target':{'id':'watch','label':'영화',
              'theater_code':'0128'},'by_date':{'20991218':rows},
              'new_keys':{r['_key'] for r in rows}}
        embeds=checker.build_alert_embeds([item])
        self.assertGreater(len(embeds),1)
        seen=[]
        for embed in embeds:
            keys=embed['_delivery']['seen']
            self.assertEqual(len(keys),len(embed['fields']))
            seen.extend(keys)
        self.assertEqual(set(seen),item['new_keys'])
        self.assertEqual(len(seen),len(set(seen)))

        item['new_keys']={rows[-1]['_key']}
        only_new=checker.build_alert_embeds([item])
        self.assertEqual(len(only_new),1)
        self.assertIn('**1개**',only_new[0]['description'])
