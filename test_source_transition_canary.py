import copy
import unittest
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from source_transition_canary import run_canary, choose_canary_date, candidate_dates, STATE_KEY
from collection_observer import observe, pages
from checker import extract_public_sessions, _fetch_public_page
from unittest.mock import patch

START = datetime(2026, 11, 5, 19, 0, tzinfo=ZoneInfo('Asia/Seoul'))
DAY = '20261106'


def payload(day, remaining=99, extra=False):
    row = {'movieCode':'30009999','movieName':'가상 신작','theaterCode':'0128',
           'playDate':day,'startTime':'10:00','scheduleId':day+'01281',
           'totalSeats':100,'remainingSeats':remaining}
    return {'data':[row, dict(row,scheduleId=day+'01282',startTime='13:00')] if extra else [row]}


class TransitionCanaryTests(unittest.TestCase):
    def test_all_dates_rotate_and_recover_then_observe_additions(self):
        state={'seen':{'watch':['preserved']}}
        calls=[]
        def request(site, day):
            calls.append(day)
            if len(calls)==1: return 502,.1,{},None
            return 200,.1,payload(day,extra=len(calls)>16),None
        first=run_canary(state,START,request)
        self.assertEqual(first['observation_state'],'unknown')
        for i in range(1,8): run_canary(state,START+timedelta(minutes=5*i),request)
        self.assertEqual(calls,candidate_dates(START))
        recovered=run_canary(state,START+timedelta(minutes=40),request)
        self.assertEqual(recovered['date'],DAY)
        self.assertEqual(recovered['first_publication_rows'],1)
        self.assertTrue(recovered['recovered_after_unconfirmed'])
        for i in range(9,16): run_canary(state,START+timedelta(minutes=5*i),request)
        added=run_canary(state,START+timedelta(minutes=80),request)
        self.assertEqual(added['added_session_count'],1)
        self.assertEqual(state['seen'],{'watch':['preserved']})

    def test_legacy_keys_and_history_are_preserved(self):
        legacy={'date':'20261109','qualified_at':START.isoformat(),
                'last_request_at':(START-timedelta(minutes=5)).isoformat(),
                'observed_keys':['legacy-key'],'history':[{'rows':37}]}
        state={STATE_KEY:copy.deepcopy(legacy)}
        run_canary(state,START,lambda site,day:(200,.1,payload(day),None))
        old=pages(state)['0128|20261109']
        self.assertEqual(old['legacy_probe'],legacy)
        self.assertIn('legacy-key',old['observed_keys'])
        self.assertEqual(old['legacy_probe']['history'],legacy['history'])
        self.assertEqual(state[STATE_KEY]['date'],DAY)
        self.assertEqual(state[STATE_KEY]['version'],2)

    def test_jitter_same_slot_and_next_slot(self):
        state={};calls=[]
        def request(site,day): calls.append(day);return 200,.1,payload(day),None
        run_canary(state,START+timedelta(seconds=49),request)
        self.assertEqual(run_canary(state,START+timedelta(minutes=4),request),{'skipped':'not_due'})
        run_canary(state,START+timedelta(minutes=5,seconds=35),request)
        self.assertEqual(len(calls),2)

    def test_429_and_retry_after_apply_across_dates(self):
        state={}
        run_canary(state,START,lambda *_:(429,.1,{},None,'7200'))
        self.assertEqual(run_canary(state,START+timedelta(minutes=90),lambda *_:self.fail()),
                         {'skipped':'supplier_backoff'})
        self.assertEqual(state['public_api_guard']['last_status'],429)

    def test_503_retry_after_is_global(self):
        state={}
        run_canary(state,START,lambda *_:(503,.1,{},None,'600'))
        self.assertEqual(run_canary(state,START+timedelta(minutes=5),lambda *_:self.fail()),
                         {'skipped':'supplier_backoff'})

    def test_failures_and_empty_never_erase_publication(self):
        state={}
        from shadow_public_monitor import canonical
        rows=[canonical(payload(DAY)['data'][0])]
        a=observe(state,'0128',DAY,START,200,rows,True,extract_public_sessions)
        self.assertEqual(a['event'],'first_observed_publication')
        keys=list(pages(state)['0128|'+DAY]['observed_keys'])
        for status in [403,429,502,None,200]:
            b=observe(state,'0128',DAY,START+timedelta(minutes=5),status,[],status==200,extract_public_sessions)
            self.assertEqual(b['observation_state'],'unknown')
            self.assertEqual(keys,pages(state)['0128|'+DAY]['observed_keys'])

    def test_seat_changes_soldout_reopening_and_partial_do_not_add(self):
        from shadow_public_monitor import canonical
        state={}
        for n,remaining in enumerate([0,99,1]):
            rows=[canonical(payload(DAY,remaining)['data'][0])]
            r=observe(state,'0128',DAY,START+timedelta(minutes=5*n),200,rows,True,extract_public_sessions)
            self.assertEqual(r['added_session_count'],0)
            self.assertEqual(r['observation_state'],'published')
        rows=[canonical(x) for x in payload(DAY,extra=True)['data']]
        r=observe(state,'0128',DAY,START+timedelta(minutes=15),200,rows,True,extract_public_sessions)
        self.assertEqual(r['added_session_count'],1)
        observe(state,'0128',DAY,START+timedelta(minutes=20),200,rows[:1],True,extract_public_sessions)
        r=observe(state,'0128',DAY,START+timedelta(minutes=25),200,rows,True,extract_public_sessions)
        self.assertEqual(r['added_session_count'],0)

    def test_malformed_identity_and_wrong_page_rejected_by_primary(self):
        wrong=payload(DAY);wrong['data'][0]['theaterCode']='9999'
        with patch('checker._production_public_request',return_value=(200,.1,wrong,None,None)):
            result=_fetch_public_page({},'0128',DAY,START)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['collection_observation']['observation_state'],'unknown')
        malformed=payload(DAY);malformed['data'][0]['scheduleId']=DAY+'0128'
        state={}
        r=run_canary(state,START,lambda *_:(200,.1,malformed,None))
        self.assertEqual(r['observation_state'],'unknown')

    def test_current_run_page_does_not_starve_another_date(self):
        state={}
        with patch.dict('os.environ',{'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'2'}):
            pages(state)['0128|'+DAY]={'last_attempt_at':(START-timedelta(minutes=10)).isoformat(),
                                     'last_run_id':'123:2'}
            self.assertEqual(choose_canary_date(state,START),'20261107')

    def test_watched_dates_use_their_existing_schedule_without_extra_requests(self):
        state={}
        self.assertEqual(run_canary(state,START,lambda *_:self.fail(),
                                    watched_dates=candidate_dates(START)),
                         {'skipped':'covered_by_watched_schedule'})

    def test_reads_already_collected_in_this_slot_are_not_repeated(self):
        state={}
        for day in candidate_dates(START):
            pages(state)['0128|'+day]={'last_attempt_at':START.isoformat()}
        self.assertEqual(run_canary(state,START,lambda *_:self.fail()),
                         {'skipped':'already_collected_this_slot'})


if __name__=='__main__': unittest.main()
