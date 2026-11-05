"""Bounded multi-date publication observer; no Discord or notification state."""
import json
import os
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from shadow_public_monitor import canonical, extract_rows, inspect_rows, structural_valid
from checker import (_production_public_request as request_timetable,
                     _parse_retry_after_seconds, _public_guard_failure,
                     extract_public_sessions, resolve_target_range)
from collection_observer import pages, seed_legacy_probe, observe

KST = ZoneInfo('Asia/Seoul')
HORIZON_DAYS = 8
PROBE_INTERVAL_SECONDS = 300
STATE_KEY = 'source_transition_canary'


def _parse_dt(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        return parsed if parsed.tzinfo else None
    except (ValueError, TypeError):
        return None


def _guard_active(state, now):
    guard = state.get('public_api_guard') or {}
    until = _parse_dt(guard.get('backoff_until'))
    return (str(guard.get('last_status')) == '429' or bool(guard.get('retry_after_raw'))) and until is not None and until > now


def candidate_dates(now):
    return [(now + timedelta(days=n)).strftime('%Y%m%d')
            for n in range(1, HORIZON_DAYS + 1)]


def choose_canary_date(state, now, site='0128', watched_dates=()):
    """Oldest observed near-future date first; one query/slot, eight dates/cycle.

    Published dates stay in the rotation to detect additions. Primary watch
    dates already collected in this slot are reused, never queried again.
    """
    ledger = pages(state)
    slot = int(now.timestamp()) // PROBE_INTERVAL_SECONDS
    run_id = (os.environ['GITHUB_RUN_ID'] + ':' +
              os.environ.get('GITHUB_RUN_ATTEMPT', '1')
              if os.environ.get('GITHUB_RUN_ID') else None)
    eligible = []
    for date in candidate_dates(now):
        if date in watched_dates:
            continue
        page = ledger.get(f'{site}|{date}') or {}
        last = _parse_dt(page.get('last_attempt_at'))
        if run_id and page.get('last_run_id') == run_id:
            continue
        if last and int(last.timestamp()) // PROBE_INTERVAL_SECONDS >= slot:
            continue
        eligible.append(date)
    return min(eligible, key=lambda d:
               (ledger.get(f'{site}|{d}', {}).get('last_attempt_at') or '', d)) if eligible else None


def qualify_canary_response(status, payload, error, site, play_date):
    raw, shape = extract_rows(payload)
    rows = [r for r in (canonical(x) for x in raw) if r is not None]
    accepted = structural_valid(status, error, shape, inspect_rows(rows))
    # Use the same strict identity/seat validation as the collection ledger.
    result = observe({}, site, play_date, datetime.now(KST), status, rows,
                     accepted, extract_public_sessions)
    return result['observation_state'] == 'published', len(rows), result['reason'] or 'qualified'


def run_canary(state, now, request=request_timetable, site='0128', watched_dates=()):
    if _guard_active(state, now):
        return {'skipped': 'supplier_backoff'}
    seed_legacy_probe(state, site)
    previous = state.setdefault(STATE_KEY, {})
    until = _parse_dt(previous.get('backoff_until'))
    if until and until > now:
        return {'skipped': 'canary_backoff'}
    last = _parse_dt(previous.get('last_request_at'))
    slot = int(now.timestamp()) // PROBE_INTERVAL_SECONDS
    if last and int(last.timestamp()) // PROBE_INTERVAL_SECONDS >= slot:
        return {'skipped': 'not_due'}
    date = choose_canary_date(state, now, site, watched_dates)
    if date is None:
        return {'skipped': ('covered_by_watched_schedule'
                            if set(candidate_dates(now)).issubset(set(watched_dates))
                            else 'already_collected_this_slot')}
    previous['last_request_at'] = now.isoformat()
    previous['scheduler_version'] = 2
    previous['last_probe_date'] = date
    response = request(site, date)
    status, elapsed, payload, error = response[:4]
    retry_after = response[4] if len(response) > 4 else None
    raw, shape = extract_rows(payload)
    rows = [r for r in (canonical(x) for x in raw) if r is not None]
    accepted = structural_valid(status, error, shape, inspect_rows(rows))
    result = observe(state, site, date, now, status, rows, accepted,
                     extract_public_sessions, reason=error or 'invalid_or_empty_response')
    # Keep the legacy top-level summary current while its original full
    # observation remains preserved under the migrated date's legacy_probe.
    observed_page = pages(state)[f'{site}|{date}']
    previous.update(version=2, date=date, last_status=status,
                    last_valid_rows=result['validated_rows'],
                    qualified_at=observed_page.get('published_at'),
                    observed_keys=list(observed_page.get('observed_keys') or []),
                    history=list(observed_page.get('history') or []))
    seconds = _parse_retry_after_seconds(retry_after, now)
    if status == 429 or (seconds is not None and status != 200):
        delay = max(seconds or 0, 3600 if status == 429 else 0)
        previous['backoff_until'] = (now + timedelta(seconds=delay)).isoformat()
        _public_guard_failure(state, status, retry_after, now)
    return dict(result, elapsed_seconds=elapsed,
                source='public_third_party_relay', queried_pages=1,
                candidate_dates=candidate_dates(now), notifications_sent=False)


def main():
    now = datetime.now(KST)
    path = Path('state.json')
    if not path.exists():
        print('SOURCE_TRANSITION_CANARY skipped: state missing')
        return
    state = json.loads(path.read_text(encoding='utf-8'))
    config_path = Path('config.json')
    config = json.loads(config_path.read_text(encoding='utf-8')) if config_path.exists() else {}
    watched = {date for target in config.get('targets', [])
               if target.get('enabled') and str(target.get('theater_code')) == '0128'
               for date in resolve_target_range(target)}
    result = run_canary(state, now, watched_dates=watched)
    # Persist migration and quota protection too; never modify seen/config.
    path.write_text(json.dumps(state, ensure_ascii=False, sort_keys=True, indent=2)+'\n', encoding='utf-8')
    output = {'result': result, 'pages': pages(state),
              'scope': 'collection_only', 'production_discord_called': False}
    Path('source_transition_observation.json').write_text(
        json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print('SOURCE_TRANSITION_CANARY', json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
