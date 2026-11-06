"""Delivery ownership and receipts, shared through the existing authenticated D1 API."""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import os
import math
import time
import uuid
from urllib.parse import urlparse
import requests


class DeliveryError(RuntimeError):
    pass


def retry_seconds(response):
    values = []
    try:
        values.append(float(response.json().get('retry_after', 0)))
    except (ValueError, TypeError, AttributeError):
        pass
    raw = getattr(response, 'headers', {}).get('Retry-After', '')
    try:
        values.append(float(raw))
    except (ValueError, TypeError):
        try:
            values.append((parsedate_to_datetime(raw) - datetime.now(timezone.utc)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            pass
    return max([0] + [v for v in values if math.isfinite(v)])


class DeliveryJournal:
    def __init__(self, state, persist):
        self.state, self.persist = state, persist
        self.token = os.environ.get('STATUS_API_TOKEN', '').strip()
        base = urlparse(os.environ.get('BROWSER_CHECK_URL', ''))
        self.url = f'{base.scheme}://{base.netloc}/api/delivery' if base.netloc else None
        self.owner = uuid.uuid4().hex

    def call(self, op, items, **extra):
        if len(items) > 20:
            batches=[self.call(op,items[start:start+20],**extra) for start in range(0,len(items),20)]
            return {'ok':True,'items':[row for batch in batches for row in batch['items']],
                    'retry_until':max(batch.get('retry_until',0) for batch in batches)}
        if self.url:
            if not self.token:
                raise DeliveryError('shared delivery credentials unavailable')
            try:
                response = requests.post(self.url, json={'op':op,'owner':self.owner,'items':items,**extra},
                    headers={'Authorization':'Bearer '+self.token}, timeout=10)
                response.raise_for_status()
                result = response.json()
                if result.get('ok') is not True: raise ValueError('invalid receipt')
                if op == 'complete' and any(r.get('status') != 'delivered' for r in result.get('items', [])):
                    raise ValueError('receipt not committed')
                return result
            except (requests.RequestException, ValueError) as exc:
                raise DeliveryError('shared delivery unavailable: '+type(exc).__name__) from None
        # Production without a coordinator URL, also used by isolated tests.
        # Persist the intent before HTTP. Shared deployments use the D1 path.
        root = self.state.setdefault('delivery_journal', {})
        now = time.time() * 1000
        results = []
        cooldown = self.state.get('discord_retry_until', 0)
        for item in items:
            row = root.setdefault(item['key'], {'status':'delivered' if item.get('delivered') else
                                  'new' if item.get('bookable') else 'suppressed','next_attempt':0})
            if item.get('delivered'): row['status']='delivered'
            if op=='claim' and cooldown<=now and row['status'] in ('new','retry','pending') and row['next_attempt']<=now:
                row.update(status='pending',owner=self.owner,next_attempt=now+600000)
                results.append({'key':item['key'],'status':'granted'})
                continue
            if op=='complete' and row.get('owner')==self.owner and row['status']=='pending':
                row['status']='delivered'
            if op=='fail' and row.get('owner')==self.owner and row['status']=='pending':
                row.update(status='retry',next_attempt=now+max(600000 if extra.get('uncertain') else 300000,
                                                            extra.get('retry_seconds',0)*1000))
            results.append({'key':item['key'],**row})
        if op=='fail' and extra.get('http_status')==429:
            self.state['discord_retry_until'] = now + max(300000,extra.get('retry_seconds',0)*1000)
        self.persist(self.state)
        return {'ok':True,'items':results}
