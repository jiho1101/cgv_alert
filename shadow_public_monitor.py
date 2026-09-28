import hashlib
import json
import time
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

BASE = "https://mcp.aka.page"
CONFIG = Path("config.json")
OUT = Path("public_shadow_result.json")
KST = ZoneInfo("Asia/Seoul")

REQUIRED_ID_FIELDS = (
    "movie_code",
    "movie_name",
    "theater_code",
    "play_date",
    "start_time",
    "schedule_id",
)


def norm(text):
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def resolve_dates(target):
    dr = target.get("date_range") or {}
    start = str(dr.get("start") or target.get("date") or "")
    end = str(dr.get("end") or start)
    if not start:
        return []
    cur = datetime.strptime(start, "%Y%m%d").date()
    last = datetime.strptime(end, "%Y%m%d").date()
    out = []
    while cur <= last:
        out.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)
    return out


def choose_target_checks(config):
    today = datetime.now(KST).date()
    horizon = today + timedelta(days=3)
    selected = []

    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        for play_ymd in resolve_dates(target):
            day = datetime.strptime(play_ymd, "%Y%m%d").date()
            if today <= day <= horizon:
                selected.append((target, play_ymd))

    if selected:
        return selected

    future = []
    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        for play_ymd in resolve_dates(target):
            day = datetime.strptime(play_ymd, "%Y%m%d").date()
            if day >= today:
                future.append((day, target, play_ymd))
    future.sort(key=lambda x: x[0])
    return [(future[0][1], future[0][2])] if future else []


def request_timetable(site_no, play_ymd):
    started = time.monotonic()
    try:
        response = requests.get(
            BASE + "/api/cgv/timetable",
            params={
                "playDate": play_ymd,
                "theaterCode": site_no,
                "limit": 200,
            },
            headers={"Accept": "application/json"},
            timeout=20,
        )
        elapsed = round(time.monotonic() - started, 3)
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return response.status_code, elapsed, payload, None
    except requests.RequestException as exc:
        return (
            None,
            round(time.monotonic() - started, 3),
            None,
            f"{type(exc).__name__}: {exc}",
        )


def extract_rows(payload):
    if not isinstance(payload, dict):
        return [], False

    data = payload.get("data")
    if isinstance(data, list):
        return data, True
    if isinstance(data, dict):
        for key in ("timetable", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value, True

    for key in ("timetable", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value, True

    return [], False


def canonical(row):
    if not isinstance(row, dict):
        return None
    return {
        "movie_code": str(row.get("movieCode") or row.get("movNo") or ""),
        "movie_name": str(
            row.get("movieName") or row.get("movNm") or row.get("prodNm") or ""
        ),
        "theater_code": str(row.get("theaterCode") or row.get("siteNo") or ""),
        "theater_name": str(row.get("theaterName") or row.get("siteNm") or ""),
        "play_date": str(row.get("playDate") or row.get("scnYmd") or ""),
        "start_time": str(row.get("startTime") or row.get("scnsrtTm") or ""),
        "end_time": str(row.get("endTime") or row.get("scnendTm") or ""),
        "schedule_id": str(row.get("scheduleId") or row.get("scnSseq") or ""),
        "screen": str(
            row.get("screenName")
            or row.get("screenNm")
            or row.get("scnsNm")
            or row.get("screenNo")
            or row.get("scnsNo")
            or ""
        ),
        "remaining_seats": row.get(
            "remainingSeats",
            row.get("frSeatCnt", row.get("frtmpSeatCnt")),
        ),
    }


def identity_key(item):
    return "|".join(
        (
            item["movie_code"],
            item["theater_code"],
            item["play_date"],
            item["schedule_id"],
            item["start_time"],
        )
    )


def target_match(item, target):
    aliases = [
        norm(x)
        for x in (
            target.get("movie_aliases")
            or [target.get("label", "")]
        )
        if str(x).strip()
    ]
    movie = norm(item.get("movie_name"))
    return bool(movie) and any(alias and alias in movie for alias in aliases)


def inspect_rows(rows):
    keys = [identity_key(item) for item in rows]
    counts = Counter(keys)
    collisions = sorted(key for key, count in counts.items() if count > 1)
    missing_required = {
        field: sum(not bool(item.get(field)) for item in rows)
        for field in REQUIRED_ID_FIELDS
    }
    unique_keys = sorted(set(keys))
    key_hash = hashlib.sha256(
        "\n".join(unique_keys).encode("utf-8")
    ).hexdigest()[:16]

    return {
        "rows": len(rows),
        "unique_identity_keys": len(unique_keys),
        "identity_collisions": collisions,
        "missing_required": missing_required,
        "missing_screen_count": sum(not bool(x.get("screen")) for x in rows),
        "identity_hash": key_hash,
        "identity_keys": unique_keys,
    }


def source_valid(status, error, payload_shape_ok, inspection):
    return (
        status == 200
        and error is None
        and payload_shape_ok
        and not inspection["identity_collisions"]
        and all(v == 0 for v in inspection["missing_required"].values())
        and inspection["unique_identity_keys"] == inspection["rows"]
    )


def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    now = datetime.now(KST)
    today_ymd = now.strftime("%Y%m%d")

    result = {
        "generated_at": now.isoformat(),
        "source": BASE,
        "mode": "shadow_only_no_notifications",
        "source_health": [],
        "target_checks": [],
    }

    cache = {}

    def fetch(site_no, play_ymd):
        cache_key = (site_no, play_ymd)
        if cache_key not in cache:
            status, elapsed, payload, error = request_timetable(site_no, play_ymd)
            raw_rows, payload_shape_ok = extract_rows(payload)
            rows = [
                item
                for item in (canonical(row) for row in raw_rows)
                if item is not None
            ]
            cache[cache_key] = (
                status,
                elapsed,
                rows,
                error,
                payload_shape_ok,
            )
        return cache[cache_key]

    theaters = {}
    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        site_no = str(target.get("theater_code") or "")
        if site_no:
            theaters[site_no] = target.get("theater_name") or site_no

    # Source-health sentinel: always query today's timetable for each configured theater.
    # This keeps the 5-minute acquisition test meaningful even after a monitored movie date expires.
    for site_no, theater_name in sorted(theaters.items()):
        status, elapsed, rows, error, payload_shape_ok = fetch(site_no, today_ymd)
        inspection = inspect_rows(rows)
        valid = source_valid(status, error, payload_shape_ok, inspection)
        item = {
            "site_no": site_no,
            "theater_name": theater_name,
            "play_ymd": today_ymd,
            "http_status": status,
            "elapsed_seconds": elapsed,
            "error": error,
            "payload_shape_ok": payload_shape_ok,
            **inspection,
            "source_valid": valid,
        }
        result["source_health"].append(item)
        print(
            "SHADOW_SOURCE "
            f"site={site_no} date={today_ymd} http={status} "
            f"rows={inspection['rows']} unique={inspection['unique_identity_keys']} "
            f"collisions={len(inspection['identity_collisions'])} "
            f"missing_required={sum(inspection['missing_required'].values())} "
            f"valid={valid} latency={elapsed}s "
            f"hash={inspection['identity_hash']}"
        )

    for target, play_ymd in choose_target_checks(config):
        site_no = str(target["theater_code"])
        status, elapsed, rows, error, payload_shape_ok = fetch(site_no, play_ymd)
        all_inspection = inspect_rows(rows)
        matched = [item for item in rows if target_match(item, target)]
        target_inspection = inspect_rows(matched)

        target_identity_valid = None
        if matched:
            target_identity_valid = (
                not target_inspection["identity_collisions"]
                and all(
                    value == 0
                    for value in target_inspection["missing_required"].values()
                )
                and target_inspection["unique_identity_keys"]
                == target_inspection["rows"]
            )

        valid = (
            source_valid(status, error, payload_shape_ok, all_inspection)
            and target_identity_valid is not False
        )

        item = {
            "target_id": str(target["id"]),
            "label": target.get("label"),
            "site_no": site_no,
            "play_ymd": play_ymd,
            "http_status": status,
            "elapsed_seconds": elapsed,
            "error": error,
            "payload_shape_ok": payload_shape_ok,
            "timetable_rows": all_inspection["rows"],
            "target_sessions": target_inspection["rows"],
            "target_present": bool(matched),
            "unique_identity_keys": target_inspection["unique_identity_keys"],
            "identity_collisions": target_inspection["identity_collisions"],
            "missing_required": target_inspection["missing_required"],
            "missing_screen_count": target_inspection["missing_screen_count"],
            "identity_hash": target_inspection["identity_hash"],
            "identity_keys": target_inspection["identity_keys"],
            "target_identity_valid": target_identity_valid,
            "valid_shadow_sample": valid,
        }
        result["target_checks"].append(item)

        print(
            "SHADOW_TARGET "
            f"target={item['target_id']} date={play_ymd} http={status} "
            f"all_rows={all_inspection['rows']} "
            f"target_sessions={target_inspection['rows']} "
            f"unique={target_inspection['unique_identity_keys']} "
            f"collisions={len(target_inspection['identity_collisions'])} "
            f"missing_screen={target_inspection['missing_screen_count']} "
            f"identity_valid={target_identity_valid} sample_valid={valid} "
            f"latency={elapsed}s hash={target_inspection['identity_hash']}"
        )

    source_checks = result["source_health"]
    target_checks = result["target_checks"]
    result["summary"] = {
        "source_checks": len(source_checks),
        "source_valid_checks": sum(bool(x["source_valid"]) for x in source_checks),
        "all_sources_valid": bool(source_checks)
        and all(x["source_valid"] for x in source_checks),
        "target_checks": len(target_checks),
        "valid_target_checks": sum(
            bool(x["valid_shadow_sample"]) for x in target_checks
        ),
        "notifications_sent": 0,
        "production_state_modified": False,
    }

    OUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("SHADOW_SUMMARY " + json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
