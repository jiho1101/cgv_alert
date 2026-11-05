"""Live public relay -> isolated production detection/delivery regression.

No production config/state or Discord is used. Network is limited to one
public supplier timetable read; subsequent engine runs replay that response.
"""
import argparse
import copy
import json
from datetime import datetime
from pathlib import Path
import tempfile
from unittest.mock import patch
from zoneinfo import ZoneInfo
import checker


def verify(day):
    now = datetime.now(ZoneInfo('Asia/Seoul'))
    response = checker._production_public_request('0128', day)
    with patch.object(checker, '_production_public_request', return_value=response):
        live = checker._fetch_public_page({}, '0128', day, now)
    if not live.get('accepted') or not live.get('rows'):
        raise RuntimeError('Actual live timetable not qualified: ' + str(live.get('status')))
    rows = live['rows']
    sample = next((r for r in rows if int(r.get('remaining_seats') or 0) > 0), None)
    if not sample:
        raise RuntimeError('No live bookable seat evidence')
    target = {'id': 'isolated-live-primary', 'enabled': True,
              'label': sample['movie_name'], 'movie_aliases': [sample['movie_name']],
              'theater_code': '0128', 'theater_name': 'CGV 울산삼산',
              'date_range': {'start': day, 'end': day}, 'screen_keywords': [],
              'require_sale_open': True, 'min_remaining_seats': 1,
              'scan_strategy': {'priority_range': {'start': day, 'end': day},
                                'priority_interval_minutes': 5, 'full_interval_minutes': 30}}
    parsed = checker.extract_public_sessions(rows, target, day,
                                             pinned_movie_code=sample['movie_code'])
    if not parsed:
        raise AssertionError('Live data rejected by production parser')
    for field, value in [('movie_code', 'WRONG'), ('movie_name', '다른 영화'),
                         ('theater_code', '9999'), ('play_date', '20990101'),
                         ('schedule_id', ''), ('schedule_id', day + '0128'),
                         ('total_seats', 0), ('remaining_seats', 0),
                         ('remaining_seats', sample['total_seats'] + 1)]:
        if checker.extract_public_sessions([dict(sample, **{field: value})], target,
                                            day, pinned_movie_code=sample['movie_code']):
            raise AssertionError('Invalid row accepted: ' + field)
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        config, state, runtime = [root / n for n in ['config.json', 'state.json', 'runtime.json']]
        config.write_text(json.dumps({'targets': [target]}))
        state.write_text(json.dumps({'version': 2, 'seen': {}, 'fallback_seen': {}}))
        with (patch.object(checker, 'CONFIG_PATH', config),
              patch.object(checker, 'STATE_PATH', state),
              patch.object(checker, 'RUNTIME_STATUS_PATH', runtime),
              patch.object(checker, 'planned_dates', return_value=[day]),
              patch.object(checker, '_production_public_request', return_value=response) as source,
              patch.object(checker, 'read_cinema_snapshot', return_value={'rows': []}),
              patch.object(checker, 'merge_emergency_seen_from_worker', return_value=False),
              patch.object(checker, 'record_shadow', return_value=False),
              patch.object(checker, 'send_health_message', return_value=False),
              patch.object(checker, 'send_discord_embeds', return_value=False) as send,
              patch.object(checker, 'CgvBrowser', side_effect=AssertionError('Unexpected official access'))):
            checker.run_checker()
            assert not (json.loads(state.read_text()).get('seen') or {}).get(target['id'])
            send.return_value = True
            checker.run_checker()
            first_seen = json.loads(state.read_text())['seen'][target['id']]
            assert len(first_seen) == len(parsed)
            sends_after_success = send.call_count
            checker.run_checker()
            assert send.call_count == sends_after_success
            changed = copy.deepcopy(response)
            for row in checker.public_extract_rows(changed[2])[0]:
                row['remainingSeats'] = 1
            source.return_value = changed
            checker.run_checker()
            assert send.call_count == sends_after_success
            assert json.loads(runtime.read_text())['collection']['outcome'] == 'collected'
            for status in [403, 429, 502, None]:
                source.return_value = (status, 0, {}, 'isolated failure', None)
                checker.run_checker()
                assert json.loads(state.read_text())['seen'][target['id']] == first_seen
            source.return_value = (200, 0, {'data': []}, None, None)
            checker.run_checker()
            assert json.loads(state.read_text())['seen'][target['id']] == first_seen
            assert send.call_count == sends_after_success
    result = {'observed_at': now.isoformat(), 'source': 'https://mcp.aka.page/api/cgv/timetable',
              'theater': '0128', 'date': day, 'http_status': response[0],
              'live_rows': len(rows), 'parsed_movie_sessions': len(parsed), 'sample': sample,
              'new_duplicate_seat_change_rejections_failed_delivery_state_preservation': 'passed',
              'production_seen_changed': False, 'real_discord_called': False,
              'natural_publication_latency_measured': False,
              'engine_validation': 'isolated replay of actual captured timetable'}
    Path('live_primary_validation.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
    print('LIVE_PRIMARY_VALIDATION', json.dumps(result, ensure_ascii=False))
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--date', required=True)
    verify(parser.parse_args().date)
