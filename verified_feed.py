"""Input boundary for a permissioned CGV collector, disabled by default.

This is not a collector and does not grant permission to scrape CGV. Configure
only a separately authorized and shadow-qualified HTTPS publisher. No proxy,
browser impersonation, redirects, retry loop, or third-party stale promotion.
"""
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import ipaddress
import os
import json
from zoneinfo import ZoneInfo
import re
from urllib.parse import urlparse

import requests

MAX_AGE_SECONDS = 300
MAX_BYTES = 2_000_000


class FeedError(RuntimeError):
    def __init__(self, message, retry_after_seconds=0):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


def enabled():
    return os.getenv("CGV_VERIFIED_FEED_ENABLED", "") == "1"


def _instant(raw):
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, TypeError):
        raise FeedError("feed timestamp missing timezone or invalid") from None


def _date(raw):
    raw = str(raw)
    if not re.fullmatch(r"\d{8}", raw):
        raise FeedError("feed date invalid")
    try:
        datetime.strptime(raw, "%Y%m%d")
    except ValueError:
        raise FeedError("feed date invalid") from None
    return raw


def validate_packet(packet, theater_code, now, source_id, permission_reference):
    if not isinstance(packet, dict) or packet.get("schema_version") != 1:
        raise FeedError("feed schema invalid")
    if not source_id or not permission_reference or packet.get("source_id") != source_id or packet.get("authorization_reference") != permission_reference:
        raise FeedError("feed publisher/permission reference mismatch")
    if packet.get("origin") not in {"cgv_first_party", "licensed_cgv_feed"}:
        raise FeedError("feed origin unqualified")
    observed, published = _instant(packet.get("observed_at")), _instant(packet.get("published_at"))
    now = now.astimezone(timezone.utc)
    age = (now - observed).total_seconds()
    if not -30 <= age <= MAX_AGE_SECONDS or not observed <= published <= now + timedelta(seconds=30):
        raise FeedError("feed stale or timestamps inconsistent")
    if str(packet.get("theater_code")) != str(theater_code) or not packet.get("theater_name"):
        raise FeedError("feed theater identity mismatch")
    coverage = packet.get("complete_dates")
    if not isinstance(coverage, list) or not coverage or len(coverage) > 90:
        raise FeedError("feed date coverage missing")
    dates = {_date(d) for d in coverage}
    raw = packet.get("rows")
    if not isinstance(raw, list) or not raw or len(raw) > 5000:
        raise FeedError("feed nonempty actual timetable evidence required")
    rows, identities, legacy_keys = [], set(), set()
    for row in raw:
        if not isinstance(row, dict):
            raise FeedError("feed row invalid")
        date = _date(row.get("play_date"))
        if str(row.get("theater_code")) != str(theater_code) or date not in dates:
            raise FeedError("feed row outside declared coverage")
        for field in ("movie_code", "movie_name", "screen_no", "screen_name", "sequence"):
            if not isinstance(row.get(field), str) or not row[field].strip():
                raise FeedError("feed full session identity missing")
        start = row.get("start_time")
        if not isinstance(start, str) or not re.fullmatch(r"(?:[01]\d|2[0-9]):[0-5]\d", start):
            raise FeedError("feed start time invalid")
        total, remaining = row.get("total_seats"), row.get("remaining_seats")
        if type(total) is not int or type(remaining) is not int or not (total > 0 and 0 <= remaining <= total):
            raise FeedError("feed seat evidence invalid")
        if type(row.get("bookable")) is not bool:
            raise FeedError("feed explicit bookable status required")
        identity = (str(theater_code), date, row["screen_no"], row["sequence"])
        # Keep the proven existing provider key as an alias. Reject an
        # ambiguous mapping rather than changing old dedup records or guessing.
        schedule_id = date + str(theater_code) + row["sequence"]
        legacy = (date, row["movie_code"], schedule_id, start)
        if identity in identities or legacy in legacy_keys:
            raise FeedError("feed duplicate/ambiguous session identity")
        identities.add(identity)
        legacy_keys.add(legacy)
        rows.append({
            "theater_code": str(theater_code), "theater_name": packet["theater_name"],
            "movie_code": row["movie_code"], "movie_name": row["movie_name"],
            "play_date": date, "start_time": start, "end_time": row.get("end_time", ""),
            "screen": row["screen_name"], "screen_no": row["screen_no"],
            "sequence": row["sequence"], "schedule_id": schedule_id,
            "total_seats": total, "remaining_seats": remaining,
            "_bookable": row["bookable"], "_verified_feed": True,
        })
    # A future-date empty result alone cannot qualify an upstream source.
    local_now = now.astimezone(ZoneInfo("Asia/Seoul"))
    near_days = {(local_now + timedelta(days=n)).strftime("%Y%m%d") for n in (0, 1)}
    if not any(r["play_date"] in near_days and r["_bookable"] and r["remaining_seats"] > 0
               and datetime.strptime(r["play_date"], "%Y%m%d").replace(tzinfo=local_now.tzinfo)
                   + timedelta(hours=int(r["start_time"][:2]), minutes=int(r["start_time"][3:])) > local_now
               for r in rows):
        raise FeedError("feed current/near-term bookable sentinel missing")
    return {"rows": rows, "complete_dates": dates, "source_id": source_id,
            "origin": packet["origin"], "observed_at": observed.isoformat(),
            "published_at": published.isoformat(), "source_age_seconds": round(age)}


def read_verified_feed(theater_code, now, session=None):
    if not enabled():
        return None
    endpoint = os.getenv("CGV_VERIFIED_FEED_URL", "")
    token = os.getenv("CGV_VERIFIED_FEED_TOKEN", "")
    source = os.getenv("CGV_VERIFIED_FEED_SOURCE_ID", "")
    permission = os.getenv("CGV_VERIFIED_FEED_PERMISSION", "")
    url = urlparse(endpoint)
    if url.scheme != "https" or not url.hostname or url.username or url.password or url.fragment:
        raise FeedError("feed HTTPS endpoint configuration invalid")
    try:
        literal = ipaddress.ip_address(url.hostname)
    except ValueError:
        literal = None
    if url.hostname in {"localhost", "metadata.google.internal"} or (literal is not None and not literal.is_global):
        raise FeedError("feed public endpoint required")
    if not token or not source or not permission:
        raise FeedError("feed credentials and authorization reference required")
    session = session or requests.Session()
    try:
        response = session.get(endpoint, params={"theater_code": str(theater_code)},
                               headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                               allow_redirects=False, timeout=12, stream=True)
    except requests.RequestException:
        # Never expose token, endpoint query credentials, or exception URL.
        raise FeedError("feed transport failed") from None
    try:
        if response.status_code != 200:
            delay = 1800 if response.status_code in {403, 429} else 0
            retry_after = response.headers.get("Retry-After", "")
            try:
                delay = max(delay, int(retry_after))
            except ValueError:
                try:
                    delay = max(delay, round((parsedate_to_datetime(retry_after) - now).total_seconds()))
                except (TypeError, ValueError):
                    pass
            raise FeedError(f"feed HTTP {response.status_code}; no retry", max(0, delay))
        chunks, size = [], 0
        try:
            for chunk in response.iter_content(65536):
                size += len(chunk)
                if size > MAX_BYTES:
                    raise FeedError("feed response too large")
                chunks.append(chunk)
        except requests.RequestException:
            raise FeedError("feed response stream failed") from None
        try:
            packet = json.loads(b"".join(chunks))
        except (ValueError, UnicodeError):
            raise FeedError("feed JSON invalid") from None
        return validate_packet(packet, theater_code, now, source, permission)
    finally:
        response.close()
