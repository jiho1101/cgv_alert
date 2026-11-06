"""Bounded UTC timing evidence; missing observations are never inferred."""
from datetime import datetime, timezone
from pathlib import Path
import json
import time
import functools
import os
_active = None

def event(stage, **values):
    if _active is not None and len(_active['events']) < 300:
        _active['events'].append({'stage':stage,'at_utc':datetime.now(timezone.utc).isoformat(),
                                 'elapsed_ms':round((time.perf_counter()-_active['clock'])*1000,3),**values})

def traced(function):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        global _active
        previous=_active
        _active={'clock':time.perf_counter(),'events':[], 'official_first_seen_utc':None,
                 'real_discord_latency_proven':False,
                 'cron_scheduled_utc':os.getenv('CGV_CRON_SCHEDULED_UTC') or None,
                 'dispatch_utc':os.getenv('CGV_DISPATCH_UTC') or None}
        event('collector_started')
        try:return function(*args,**kwargs)
        finally:
            event('collector_finished')
            data={k:v for k,v in _active.items() if k!='clock'}
            try:
                import checker
                path=checker.RUNTIME_STATUS_PATH.with_name('latency_trace.json')
                path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
                print('LATENCY_TRACE '+json.dumps(data,ensure_ascii=False))
            except OSError as exc:
                print("::warning::Latency evidence save failed: "+type(exc).__name__)
            finally:_active=previous
    return wrapper

def source_timed(function, source):
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        started=time.perf_counter()
        try:
            result=function(*args,**kwargs)
            event('source_response',source=source,duration_ms=round((time.perf_counter()-started)*1000,3),
                  http_status=result[0] if isinstance(result,tuple) else None,
                  source_age_seconds=result.get('source_age_seconds') if isinstance(result,dict) else None)
            return result
        except Exception as exc:
            event('source_error',source=source,duration_ms=round((time.perf_counter()-started)*1000,3),error_type=type(exc).__name__)
            raise
    return wrapper
