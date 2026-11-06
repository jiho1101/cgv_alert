"""Actual CGV capture -> production checker/Worker -> loopback HTTP -> D1 SQL receipts.
No real Discord or external provider requests are made by these tests.
"""
import copy
from datetime import datetime
from contextlib import ExitStack, contextmanager
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import requests
import checker
from delivery_journal import DeliveryJournal, DeliveryError

FIXTURE=Path(__file__).parent/'tests/fixtures/stage2-live-2026-11-05.json'

class Stage2DeliveryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture=json.loads(FIXTURE.read_text())
        cls.proc=subprocess.Popen(['node','cloudflare/delivery-test-server.mjs'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        cls.url=json.loads(cls.proc.stdout.readline())['url']
    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate();cls.proc.wait(timeout=5);cls.proc.stdout.close()
    def control(self,**body):
        r=requests.post(self.url+'/control',json=body,timeout=5);r.raise_for_status();return r.json()
    def setUp(self):
        self.control(reset=True)
        observed=datetime.fromisoformat(self.capture['observed_at'])
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return observed.astimezone(tz) if tz else observed.replace(tzinfo=None)
        clock=patch.object(checker,'datetime',Clock)
        clock.start()
        self.addCleanup(clock.stop)
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.day='20261106';self.days=[self.day,'20261107']
        sample=self.capture['dates'][self.day]['rows'][0]
        self.target={'id':'isolated-stage2','enabled':True,'label':sample['movie_name'],
          'movie_aliases':[sample['movie_name']],'theater_code':'0128','theater_name':'CGV 울산삼산',
          'require_sale_open':True,'min_remaining_seats':1,'date_range':{'start':self.day,'end':self.days[-1]},
          'scan_strategy':{'priority_range':{'start':self.day,'end':self.day}}}
        self.code=sample['movie_code'];self.config=self.root/'config.json';self.state=self.root/'state.json';self.runtime=self.root/'runtime.json'
        self.config.write_text(json.dumps({'targets':[self.target]}))
        self.state.write_text(json.dumps({'version':2,'seen':{'preserved':['old']},'fallback_seen':{'preserved':['candidate']}}))
        self.responses={d:copy.deepcopy(self.capture['dates'][d]['response']) for d in self.days}
        self.snapshot={'rows':[]}
    @contextmanager
    def engine(self):
        observed=datetime.fromisoformat(self.capture['observed_at'])
        class Clock(datetime):
            @classmethod
            def now(cls,tz=None):return observed.astimezone(tz) if tz else observed.replace(tzinfo=None)
        with ExitStack() as st:
            st.enter_context(patch.object(checker,'datetime',Clock))
            st.enter_context(patch.dict(os.environ,{'DISCORD_WEBHOOK_URL':self.url+'/webhook',
                'BROWSER_CHECK_URL':self.url+'/api/browser-check','STATUS_API_TOKEN':'isolated-local-token'}))
            for name,value in [('CONFIG_PATH',self.config),('STATE_PATH',self.state),('RUNTIME_STATUS_PATH',self.runtime)]:
                st.enter_context(patch.object(checker,name,value))
            st.enter_context(patch.object(checker,'planned_dates',return_value=self.days))
            st.enter_context(patch.object(checker,'_production_public_request',side_effect=lambda site,day:self.responses[day]))
            st.enter_context(patch.object(checker,'read_cinema_snapshot',side_effect=lambda *args:self.snapshot))
            st.enter_context(patch.object(checker,'merge_emergency_seen_from_worker',return_value=False))
            st.enter_context(patch.object(checker,'record_shadow',return_value=False))
            st.enter_context(patch.object(checker,'send_health_message',return_value=False))
            st.enter_context(patch.object(checker,'CgvBrowser',side_effect=AssertionError('Forbidden external access')))
            yield
    def run_engine(self):
        with self.engine():checker.run_checker()
    def seen(self):return json.loads(self.state.read_text()).get('seen',{}).get(self.target['id'],[])
    def messages(self):return self.control()['messages']
    def rows(self,day):return checker.public_extract_rows(self.responses[day][2])[0]
    def test_real_full_pipeline_duplicate_seats_recovery_alias_source_and_content(self):
        self.run_engine();first=self.seen();messages=self.messages()
        self.assertGreater(len(first),1);self.assertEqual(len(messages),2)
        expected=[]
        for day in self.days:
            expected+=checker.extract_public_sessions(self.capture['dates'][day]['rows'],self.target,day,self.code)
        self.assertEqual(set(first),{r['_key'] for r in expected})
        for message in messages:
            embed=message['embeds'][0]
            self.assertIn(self.target['label'],embed['title']);self.assertIn('CGV 울산삼산',embed['description'])
            self.assertIn('siteNo=0128',embed['url']);self.assertIn('scnYmd=',embed['url'])
            self.assertIn('상영관 정보 미제공',embed['fields'][0]['name'])
            self.assertTrue(all('잔여 ' in field['value'] and '석)' in field['value'] for field in embed['fields']))
            self.assertTrue(all(not key.startswith('_') for key in embed))
        self.run_engine()
        for day in self.days:
            for row in self.rows(day):row['remainingSeats']=1
        self.run_engine();self.assertEqual(len(self.messages()),2)
        # Partial response, empty response, failure and recovery do not erase receipts.
        original=copy.deepcopy(self.responses)
        for day in self.days:self.responses[day][2]['data']['timetable']=self.rows(day)[:1]
        self.run_engine()
        for status in [502,403,429,None,200]:
            self.responses={d:[status,0,{'data':[]},'isolated failure' if status!=200 else None,None] for d in self.days}
            self.run_engine();self.assertEqual(self.seen(),first)
        self.responses=original;self.run_engine();self.assertEqual(len(self.messages()),2)
        # A title alias change preserves the identity; a code mismatch remains excluded.
        alias=self.target['label']+' (재개봉)';self.target['movie_aliases'].append(alias)
        self.config.write_text(json.dumps({'targets':[self.target]}))
        for day in self.days:
            for row in self.rows(day):
                if row['movieCode']==self.code:row['movieName']=alias
        self.run_engine();self.assertEqual(len(self.messages()),2)
        self.snapshot={'rows':[r for d in self.days for r in self.capture['dates'][d]['rows']]}
        self.responses={d:[502,0,{},'isolated failure',None] for d in self.days}
        self.run_engine();self.assertEqual(len(self.messages()),2);self.assertEqual(self.seen(),first)
        state=json.loads(self.state.read_text());self.assertEqual(state['seen']['preserved'],['old'])
        self.assertEqual(state['fallback_seen']['preserved'],['candidate'])
    def test_initial_soldout_resale_and_invalid_identity_excluded(self):
        for day in self.days:
            for row in self.rows(day):
                if row['movieCode']==self.code:row['remainingSeats']=0
        self.run_engine();self.assertFalse(self.seen());self.assertFalse(self.messages())
        self.responses={d:copy.deepcopy(self.capture['dates'][d]['response']) for d in self.days}
        self.run_engine();self.assertFalse(self.messages());self.assertFalse(self.seen())
        sample=self.capture['dates'][self.day]['rows'][0]
        for field,value in [('movie_name','wrong'),('movie_code','wrong'),('theater_code','9999'),
                            ('play_date','20261107'),('schedule_id',''),('schedule_id','garbage'),('remaining_seats',True),('remaining_seats',1.5),
                            ('schedule_id',self.day+'0128'),('start_time','30:00'),
                            ('total_seats',0),('remaining_seats',sample['total_seats']+1)]:
            self.assertFalse(checker.extract_public_sessions([dict(sample,**{field:value})],self.target,self.day,self.code),(field,value))
    def test_partial_failure_retry_only_unsent_and_shared_429(self):
        self.control(responses=[{'status':204},{'status':500}])
        with self.assertRaisesRegex(RuntimeError,'HTTP 500'):self.run_engine()
        first=self.seen();self.assertTrue(first);self.assertTrue(all(self.day in k for k in first))
        self.run_engine();self.assertEqual(len(self.messages()),2)
        self.control(advance=301000,responses=[{'status':429,'retry_after':900}])
        with self.assertRaisesRegex(RuntimeError,'HTTP 429'):self.run_engine()
        self.assertEqual(self.seen(),first)
        self.control(advance=301000);self.run_engine();self.assertEqual(len(self.messages()),3)
        self.control(advance=600000);self.run_engine();self.assertGreater(len(self.seen()),len(first))
        self.assertEqual(len(self.messages()),4)
        self.run_engine();self.assertEqual(len(self.messages()),4)
    def test_receipt_survives_local_save_failure_and_ack_loss_window(self):
        with self.engine():
            real_save=checker.save_state
            def fail_receipt(state):
                if state.get('seen',{}).get(self.target['id']):raise OSError('isolated disk failure')
                return real_save(state)
            with patch.object(checker,'save_state',side_effect=fail_receipt):
                with self.assertRaises(OSError):checker.run_checker()
        self.assertFalse(self.seen());self.assertEqual(len(self.messages()),1)
        self.run_engine();self.assertEqual(len(self.messages()),2)
        self.assertTrue(self.seen());self.run_engine();self.assertEqual(len(self.messages()),2)
        self.control(reset=True,failAck=True)
        self.state.write_text(json.dumps({'seen':{},'fallback_seen':{}}))
        with self.assertRaises(DeliveryError):self.run_engine()
        self.assertEqual(len(self.messages()),2);self.assertTrue(self.seen())
        self.control(failAck=False);self.run_engine();self.assertEqual(len(self.messages()),2)
    def test_timeout_never_seen_then_delayed_retry(self):
        self.control(responses=[{'status':204,'delay':100}])
        real_post=requests.post
        def shortened_timeout(url,**kwargs):
            if url.endswith('/webhook'):kwargs['timeout']=.02
            return real_post(url,**kwargs)
        with patch.object(checker.requests,'post',side_effect=shortened_timeout):
            with self.assertRaisesRegex(RuntimeError,'ReadTimeout'):self.run_engine()
        self.assertTrue(all(self.days[1] in key for key in self.seen()))
        self.run_engine();self.assertEqual(len(self.messages()),2) # Other date remains independent.
        self.assertTrue(all(self.days[1] in key for key in self.seen()))
        self.control(advance=601000);self.run_engine();self.assertEqual(len(self.messages()),3)
    def test_atomic_owners_crash_reservation_and_worker_github_overlap(self):
        sample=self.capture['dates'][self.day]['rows'][0]
        session=checker.extract_public_sessions([sample],self.target,self.day,self.code)[0]
        item={'key':session['_key'],'target_id':self.target['id'],'bookable':True}
        with self.engine():
            a=DeliveryJournal({},lambda state:None);b=DeliveryJournal({},lambda state:None)
            a.call('observe',[item])
            with ThreadPoolExecutor(2) as pool:
                results=list(pool.map(lambda journal:journal.call('claim',[item]),[a,b]))
            self.assertEqual(sum(row['status']=='granted' for r in results for row in r['items']),1)
            # Process loss after reservation: no other owner may send until the uncertainty window.
            self.assertNotEqual(b.call('claim',[item])['items'][0]['status'],'granted')
            self.control(advance=601000)
            self.assertEqual(b.call('claim',[item])['items'][0]['status'],'granted')
            b.call('complete',[item]);self.assertEqual(a.call('claim',[item])['items'][0]['status'],'delivered')
        self.control(reset=True)
        # Production Worker sends captured real sessions, then Python finds the same D1 receipts.
        worker_target=dict(self.target,priority_dates=[self.day],pinned_movie_code=self.code)
        self.control(target=worker_target,fixture=self.rows(self.day))
        r=requests.post(self.url+'/emergency',json={},timeout=10);r.raise_for_status()
        self.assertTrue(r.json()['notified_count'],r.json())
        self.assertEqual(len(self.messages()),1)
        self.run_engine();self.assertEqual(len(self.messages()),2) # Only the other date is sent by Python.
        self.assertTrue(self.seen());self.run_engine();self.assertEqual(len(self.messages()),2)
        # Reverse order uses exactly the same centralized delivered state.
        r=requests.post(self.url+'/emergency',json={},timeout=10);r.raise_for_status()
        self.assertFalse(r.json()['notified_count']);self.assertEqual(len(self.messages()),2)
    def test_actual_additional_session_and_pinned_alias_ambiguity(self):
        original=copy.deepcopy(self.responses)
        removed=None
        for row in self.rows(self.day):
            if row['movieCode']==self.code:
                removed=row
                break
        self.rows(self.day).remove(removed)
        self.run_engine();first=self.seen();count=len(self.messages())
        self.responses=original
        self.run_engine();self.assertEqual(len(self.seen()),len(first)+1)
        self.assertEqual(len(self.messages()),count+1)
        embed=self.messages()[-1]['embeds'][0]
        self.assertIn('**1개**',embed['description'])
        self.assertIn(removed['startTime'],embed['fields'][0]['value'])
        # Same exact alias with a different code cannot pass a pinned target.
        self.control(reset=True)
        self.target['movie_code']=self.code
        self.config.write_text(json.dumps({'targets':[self.target]}))
        self.state.write_text(json.dumps({'seen':{},'fallback_seen':{}}))
        for day in self.days:
            for row in self.rows(day):
                if row['movieCode']==self.code:row['movieCode']='wrong-code'
        self.run_engine();self.assertFalse(self.messages());self.assertFalse(self.seen())
    def test_process_exit_after_http_before_receipt_has_uncertainty_reservation(self):
        # Kill a real checker process after the loopback server accepts HTTP but before
        # D1 acknowledgement/local seen. Its claim must outlive the process.
        script='''
import os, requests
from unittest.mock import patch
from test_stage2_delivery import Stage2DeliveryTests
Stage2DeliveryTests.url=os.environ['ISOLATED_TEST_URL']
import json
Stage2DeliveryTests.capture=json.load(open('tests/fixtures/stage2-live-2026-11-05.json'))
t=Stage2DeliveryTests();t.setUp()
real=requests.post
def post(url,**kwargs):
    response=real(url,**kwargs)
    if url.endswith('/webhook'):os._exit(17)
    return response
with patch('checker.requests.post',side_effect=post):t.run_engine()
'''
        completed=subprocess.run(['python','-c',script],env=dict(os.environ,ISOLATED_TEST_URL=self.url),
                                 stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=15)
        self.assertEqual(completed.returncode,17,completed.stderr.decode())
        self.assertEqual(len(self.messages()),1)
        self.run_engine()
        self.assertEqual(len(self.messages()),2) # Only the unclaimed second date.
        self.assertTrue(all(self.days[1] in key for key in self.seen()))
        self.control(advance=601000)
        self.run_engine()
        self.assertEqual(len(self.messages()),3)
        # This third POST is the documented, irreducible acknowledgement-loss ambiguity.
        self.assertEqual(len(self.seen()),7)
    def test_priority_http_delivery_precedes_slow_nonpriority_failure(self):
        import time
        def source(site,day):
            if day==self.days[1]:
                self.assertEqual(len(self.messages()),1, 'Priority must deliver before this request starts')
                time.sleep(.05)
                return (502,.05,{},'isolated 502',None)
            return self.responses[day]
        with self.engine(), patch.object(checker,'_production_public_request',side_effect=source):
            checker.run_checker()
        trace=json.loads((self.root/'latency_trace.json').read_text())
        stages=[e['stage'] for e in trace['events']]
        self.assertLess(stages.index('discord_response'),max(i for i,s in enumerate(stages) if s=='page_finished'))
        self.assertIsNone(trace['official_first_seen_utc'])
        self.assertFalse(trace['real_discord_latency_proven'])
        self.assertTrue(all(e['at_utc'].endswith('+00:00') for e in trace['events']))
        response=next(e for e in trace['events'] if e['stage']=='discord_response')
        self.assertEqual(response['http_status'],204)
    def test_atomic_state_write_preserves_old_file(self):
        old=self.state.read_bytes()
        with patch.object(checker,'STATE_PATH',self.state),patch.object(checker.os,'replace',side_effect=OSError('isolated replace failure')):
            with self.assertRaises(OSError):checker.save_state({'seen':{'wrong':['new']}})
        self.assertEqual(self.state.read_bytes(),old)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()),['config.json','state.json'])

if __name__=='__main__':unittest.main()
