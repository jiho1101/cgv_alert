"""Collection-only publication ledger. Never reads or writes notification seen."""
from copy import deepcopy
import os
from shadow_public_monitor import identity_key

STATE_KEY = 'collection_observations'


def pages(state):
    root = state.setdefault(STATE_KEY, {'version': 1, 'pages': {}})
    return root.setdefault('pages', {})


def seed_legacy_probe(state, site):
    legacy = state.get('source_transition_canary') or {}
    date = str(legacy.get('date') or '')
    if len(date) != 8 or not date.isdigit():
        return
    key = f'{site}|{date}'
    ledger = pages(state)
    if key in ledger:
        return
    ledger[key] = {
        'date': date, 'theater_code': site,
        'last_attempt_at': legacy.get('last_request_at'),
        'published_at': legacy.get('qualified_at'),
        'observed_keys': list(legacy.get('observed_keys') or []),
        'identity_baseline_established': bool(legacy.get('observed_keys')),
        'last_valid_rows': legacy.get('last_valid_rows', 0),
        'legacy_probe': deepcopy(legacy),
    }


def observe(state, site, date, now, status, rows, accepted, parse, reason=None):
    """Track identities of published rows, including sold-out rows.

    Returning seats to an already published sold-out session is not a new
    session. Failed/empty/partial responses never remove any identity.
    """
    key = f'{site}|{date}'
    page = pages(state).setdefault(key, {'date': date, 'theater_code': site})
    identities, bookable, rejected = set(), 0, 0
    valid = bool(accepted and status == 200 and rows)
    if valid:
        for row in rows:
            try:
                total, remaining = row['total_seats'], row['remaining_seats']
                seat_ok = (not isinstance(total, bool) and not isinstance(remaining, bool)
                           and int(total) == float(total) and int(remaining) == float(remaining)
                           and int(total) > 0 and 0 <= int(remaining) <= int(total))
            except (KeyError, TypeError, ValueError, OverflowError):
                seat_ok = False
            target = {'id': 'collection-only', 'label': row.get('movie_name'),
                      'movie_aliases': [row.get('movie_name')],
                      'theater_code': site, 'require_sale_open': False}
            if not seat_ok or not parse([row], target, date,
                                        pinned_movie_code=row.get('movie_code')):
                rejected += 1
                reason = 'invalid_identity_or_seats'
                continue
            identities.add(identity_key(row))
            bookable += int(int(remaining) > 0)
    valid = valid and bool(identities)
    previous_keys = set(page.get('observed_keys') or [])
    previously_published = bool(page.get('published_at') and
                                page.get('identity_baseline_established', bool(previous_keys)))
    added = identities - previous_keys if valid and previously_published else set()
    recovered = valid and page.get('last_observation_state') == 'unknown'
    first = valid and not previously_published
    if valid:
        page['observed_keys'] = sorted(previous_keys | identities)
        page['identity_baseline_established'] = True
        page['published_at'] = page.get('published_at') or now.isoformat()
        page['last_success_at'] = now.isoformat()
        page['last_valid_rows'] = len(identities)
        page['successful_observations'] = int(page.get('successful_observations') or 0) + 1
        event = ('first_observed_publication' if first else
                 'additional_sessions' if added else 'recovered' if recovered else 'unchanged')
    else:
        page['last_unconfirmed_at'] = now.isoformat()
        page['failed_or_unconfirmed_observations'] = int(page.get('failed_or_unconfirmed_observations') or 0) + 1
        event = 'unconfirmed'
    sample = {'at': now.isoformat(), 'date': date, 'http_status': status,
              'observation_state': 'published' if valid else 'unknown',
              'event': event, 'validated_rows': len(identities) if valid else 0,
              'rejected_rows': rejected,
              'bookable_seat_rows': bookable if valid else 0,
              'first_publication_rows': len(identities) if first else 0,
              'added_session_count': len(added), 'added_keys': sorted(added),
              'recovered_after_unconfirmed': recovered,
              'reason': reason if not valid else None,
              'official_publication_time_known': False}
    page['last_run_id'] = os.environ.get('GITHUB_RUN_ID')
    page.update(last_attempt_at=now.isoformat(), last_status=status,
                last_observation_state=sample['observation_state'], latest=sample)
    page['history'] = (page.get('history') or [])[-19:] + [sample]
    return sample


def recent_observations(state, now):
    return [p['latest'] for p in pages(state).values()
            if p.get('last_attempt_at') == now.isoformat() and p.get('latest')]
