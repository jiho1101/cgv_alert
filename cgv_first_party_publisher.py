"""Optional first-party CGV feed publisher; does not modify Production.

Reads only responses initiated by the normal official booking page. Does not
invoke CGV's rejected API independently, impersonate a browser, or retry on
access restrictions. Requires separately verified permission for unattended
collection. No Discord messages or updates to config/state/secrets.
"""
import argparse
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from verified_feed import FeedError, validate_packet

KST = ZoneInfo("Asia/Seoul")
MAX_OBSERVATION_SPAN_SECONDS = 240


class CollectorError(RuntimeError):
    pass


def _nonnegative_int(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = int(value)
        return parsed if parsed >= 0 and str(parsed) == str(value).strip() else None
    except (TypeError, ValueError, OverflowError):
        return None


def _time(value):
    raw = str(value or "").strip().replace(":", "")
    if not (raw.isascii() and raw.isdigit() and len(raw) in (3, 4)):
        return None
    raw = raw.zfill(4)
    hour, minute = int(raw[:2]), int(raw[2:])
    return f"{hour:02d}:{minute:02d}" if hour <= 29 and minute <= 59 else None


def canonicalize(row, theater_code, date):
    """Preserve exact first-party session identity; refuse incomplete rows."""
    if not isinstance(row, dict) or str(row.get("siteNo")) != theater_code or str(row.get("scnYmd")) != date:
        raise CollectorError("theater/date mismatch in official response")
    mov = str(row.get("movNo") or "").strip()
    title = str(row.get("movNm") or row.get("expoProdNm") or "").strip()
    screen = str(row.get("scnsNo") or "").strip()
    screen_name = str(row.get("scnsNm") or row.get("scnsEnm") or "").strip()
    sequence = str(row.get("scnSseq") or "").strip()
    start = _time(row.get("scnsrtTm"))
    end = _time(row.get("scnendTm"))
    total, remaining = _nonnegative_int(row.get("stcnt")), _nonnegative_int(row.get("frSeatCnt"))
    if not (mov and title and screen.isdigit() and sequence.isdigit() and screen_name and start):
        raise CollectorError("official session identity incomplete")
    if total is None or total <= 0 or remaining is None or remaining > total:
        raise CollectorError("invalid official seat evidence")
    blocked = (
        row.get("salYn") == "N" or row.get("cntlYn") == "Y"
        or any(x in str(row.get("salsStusNm") or "")
               for x in ("예매종료", "판매종료", "판매중지"))
    )
    return {
        "theater_code": theater_code, "movie_code": mov, "movie_name": title,
        "play_date": date, "screen_no": screen, "screen_name": screen_name,
        "sequence": sequence, "start_time": start, "end_time": end or "",
        "total_seats": total, "remaining_seats": remaining,
        "bookable": remaining > 0 and not blocked,
    }


def build_packet(collector, theater_code, theater_name, dates, source_id,
                 permission_reference, clock=None):
    """Collect each date at most once; halt on restriction or bad identity."""
    clock = clock or (lambda: datetime.now(timezone.utc))
    began = clock().astimezone(timezone.utc)
    if not dates or not source_id or not permission_reference:
        raise CollectorError("dates/source/permission reference missing")
    if len(dates) != len(set(dates)) or len(dates) > 20:
        raise CollectorError("duplicate or excessive dates")
    rows, complete_dates, identities = [], [], set()
    for date in dates:
        try:
            datetime.strptime(date, "%Y%m%d")
        except (TypeError, ValueError):
            raise CollectorError("invalid date") from None
        if (clock().astimezone(timezone.utc) - began).total_seconds() > MAX_OBSERVATION_SPAN_SECONDS:
            raise CollectorError("observation sweep exceeded freshness window")
        received = collector(theater_code, theater_name, date)
        if not isinstance(received, list):
            raise CollectorError("first-party browser response unavailable or restricted")
        if not received:
            # Unpublished future dates are unknown, not complete zero-session evidence.
            continue
        mapped = [canonicalize(r, theater_code, date) for r in received]
        for row in mapped:
            ident = (date, row["screen_no"], row["sequence"])
            if ident in identities:
                raise CollectorError("ambiguous session identity")
            identities.add(ident)
        rows.extend(mapped)
        complete_dates.append(date)
    observed = clock().astimezone(timezone.utc)
    if (observed - began).total_seconds() > MAX_OBSERVATION_SPAN_SECONDS:
        raise CollectorError("observation sweep exceeded freshness window")
    packet = {
        "schema_version": 1, "source_id": source_id,
        "authorization_reference": permission_reference,
        "origin": "cgv_first_party", "theater_code": theater_code,
        "theater_name": theater_name,
        "observed_at": began.isoformat(), "published_at": observed.isoformat(),
        "complete_dates": complete_dates, "rows": rows,
    }
    try:
        validate_packet(packet, theater_code, observed, source_id, permission_reference)
    except FeedError as exc:
        raise CollectorError(f"feed validation failed: {exc}") from None
    return packet


def default_dates(now, priority_date="20261218"):
    local_today = now.astimezone(KST).date()
    dates = [(local_today + timedelta(days=i)).strftime("%Y%m%d") for i in (0, 1)]
    if priority_date and priority_date not in dates:
        dates.append(priority_date)
    return dates

 
def collect_official_rows(browser, site_no, theater_name, date):
    """Only inspect what the normal booking page itself has requested."""
    # siteNm is the name without the CGV brand prefix; this matches
    # checker.page_site_name() for the same cinema.
    page_name = str(theater_name).removeprefix("CGV ").strip()
    if not page_name:
        raise CollectorError("official cinema display name missing")
    browser.current_site = None
    browser._bootstrap(site_no, page_name, date)
    result = browser._consume_browser_schedule(site_no, date, wait_seconds=8)
    if not result or not result.get("ok"):
        raise CollectorError("official page schedule missing/restricted; stop")
    rows = result.get("rows")
    if not isinstance(rows, list):
        raise CollectorError("official page schedule shape invalid")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Read-only first-party browser feed prototype")
    parser.add_argument("--date", action="append", help="YYYYMMDD; repeat for more dates")
    parser.add_argument("--priority-date", default="20261218")
    parser.add_argument("--output", default="cgv_first_party_candidate.json")
    args = parser.parse_args()
    if os.environ.get("CGV_OFFICIAL_AUTOMATION_AUTHORIZED") != "1":
        raise SystemExit("NOT RUN: CGV unattended-collection permission not confirmed")
    source = os.environ.get("CGV_VERIFIED_FEED_SOURCE_ID", "")
    permission = os.environ.get("CGV_VERIFIED_FEED_PERMISSION", "")
    if not source or not permission:
        raise SystemExit("NOT RUN: verified publisher identifiers not configured")
    dates = args.date or default_dates(datetime.now(timezone.utc), args.priority_date)

    # Existing CgvBrowser captures the booking PAGE'S OWN network response.
    # Deliberately DO NOT call fetch_schedule(), whose fallback calls the API
    # separately and reloads. Explicit restrictions end the run immediately.
    from checker import CgvBrowser
    browser = CgvBrowser(timeout=12)
    def collect(site_no, site_name, date):
        return collect_official_rows(browser, site_no, site_name, date)

    try:
        packet = build_packet(collect, "0128", "CGV 울산삼산", dates, source, permission)
    except Exception as exc:
        raise SystemExit(f"UNQUALIFIED: {type(exc).__name__}: {exc}") from None
    finally:
        browser.close()
    Path(args.output).write_text(
        json.dumps(packet, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"LOCAL_ONLY: {len(packet['rows'])} first-party session rows; no Production change")


if __name__ == "__main__":
    main()
