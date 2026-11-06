"""Read-only hourly evidence. Workflow success is not collection success."""
import json, os
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE='https://api.github.com/repos/jiho1101/cgv_alert'
ANCHOR=datetime(2026,11,6,2,30,tzinfo=timezone.utc)

def summarize(runs, now):
    end=int(now.timestamp()//300)-1  # Exclude the open slot and its queued run.
    start=int(ANCHOR.timestamp()//300)
    slots={}; outcomes={}
    selected=[]
    for r in runs:
        if r.get('name')!='CGV Alert' or r.get('event')!='workflow_dispatch':continue
        at=datetime.fromisoformat(r['created_at'].replace('Z','+00:00'))
        slot=int(at.timestamp()//300)
        if not start<=slot<=end:continue
        slots[slot]=slots.get(slot,0)+1;selected.append(at.timestamp())
        result=r.get('conclusion') or r.get('status');outcomes[result]=outcomes.get(result,0)+1
    selected.sort()
    return {'baseline_utc':ANCHOR.isoformat(),'observed_until_utc':datetime.fromtimestamp((end+1)*300,timezone.utc).isoformat(),
        'window_has_24_hours':(end-start+1)>=288,'verdict':'review_pending',
        'expected_creation_slots':max(0,end-start+1),'actual_dispatch_runs':len(selected),
        'missing_creation_slots':[datetime.fromtimestamp(s*300,timezone.utc).isoformat() for s in range(start,end+1) if s not in slots],
        'extra_runs_in_creation_slots':sum(max(0,n-1) for n in slots.values()),
        'max_creation_gap_seconds':max([b-a for a,b in zip(selected,selected[1:])],default=None),
        'outcomes':outcomes,'five_minute_open_to_discord_proven':False,
        'limitations':['Creation slots are not exact Cron slots; delayed/manual runs need trace review.',
                      '24-hour window alone does not prove collection, delivery or post-fix stability.']}

def main():
    now=datetime.now(timezone.utc)
    session=requests.Session();session.headers['Authorization']='Bearer '+os.environ['GITHUB_TOKEN']
    runs=[]
    for page in range(1,31):
        response=session.get(BASE+'/actions/runs',params={'per_page':100,'page':page},timeout=20)
        response.raise_for_status();batch=response.json()['workflow_runs'];runs.extend(batch)
        if not batch or min(r['created_at'] for r in batch)<ANCHOR.isoformat().replace('+00:00','Z'):break
    else:raise RuntimeError('Audit pagination incomplete; no completeness claim')
    report=summarize(runs,now)
    latest=next((r for r in runs if r.get('name')=='CGV Alert' and r.get('event')=='workflow_dispatch' and r.get('status')=='completed'),None)
    if latest:
        jobs=session.get(latest['jobs_url'],timeout=20);jobs.raise_for_status()
        report['latest_completed_run']={'id':latest['id'],'created_utc':latest['created_at'],'jobs':[
            {'started_utc':j['started_at'],'finished_utc':j['completed_at'],'conclusion':j['conclusion'],
             'collection_steps':[s for s in j['steps'] if s['name']=='Check configured alerts']}
            for j in jobs.json()['jobs']]}

    state=json.loads(Path('state.json').read_text())
    report['state_counts']={k:sum(len(v) for v in state.get(k,{}).values()) for k in ['seen','fallback_seen']}
    runtime=json.loads(Path('worker_runtime_latest.json').read_text())
    report['worker_version']=runtime.get('version');report['emergency_config']=runtime.get('emergency_config')
    report['emergency_fallback']=runtime.get('emergency_fallback')
    Path('operations_audit.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    print('OPERATIONS_AUDIT '+json.dumps(report,ensure_ascii=False))

if __name__=='__main__':main()
