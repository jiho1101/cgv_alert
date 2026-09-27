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


def choose_targets(config):
    today = datetime.now(KST).date()
    horizon = today + timedelta(days=3)
    selected = []

    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        dates = resolve_dates(target)
        near = [
            d for d in dates
            if today <= datetime.strptime(d, "%Y%m%d").date() <= horizon
        ]
        for play_ymd in near:
            selected.append((target, play_ymd))

    if selected:
        return selected

    future = []
    for target in config.get("targets", []):
        if not target.get("enabled", True):
            continue
        for play_ymd in resolve_dates(target):
            d = datetime.strptime(play_ymd, "%Y%m%d").date()
            if d >= today:
                future.append((d, target, play_ymd))
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
        payload = None
        try:
            payload = response.json()
        except ValueError:
            pass
        return response.status_code, elapsed, payload, None
    except requests.RequestException as exc:
        return None, round(time.monotonic() - started, 3), None, f"{type(exc).__name__}: {exc}"


def extract_rows(payload):
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("timetable", "items", "results"):
            value = data.get(key)
            if isinstance(value, list):
                return value
    for key in ("timetable", "items", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def canonical(row):
    if not isinstance(row, dict):
        return None
    return {
        "movie_code": str(row.get("movieCode") or row.get("movNo") or ""),
        "movie_name": str(row.get("movieName") or row.get("movNm") or row.get("prodNm") or ""),
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
        "remaining_seats": row.get("remainingSeats", row.get("frSeatCnt", row.get("frtmpSeatCnt"))),
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
    aliases = [norm(x) for x in (target.get("movie_aliases") or [target.get("label", "")]) if str(x).strip()]
    movie = norm(item.get("movie_name"))
    return bool(movie) and any(alias and alias in movie for alias in aliases)


def main():
    config = json.loads(CONFIG.read_text(encoding="utf-8"))
    selected = choose_targets(config)

    result = {
        "generated_at": datetime.now(KST).isoformat(),
        "source": BASE,
        "mode": "shadow_only_no_notifications",
        "checks": [],
    }

    cache = {}
    for target, play_ymd in selected:
        site_no = str(target["theater_code"])
        cache_key = (site_no, play_ymd)

        if cache_key not in cache:
            status, elapsed, payload, error = request_timetable(site_no, play_ymd)
            rows = [x for x in (canonical(r) for r in extract_rows(payload)) if x]
            cache[cache_key] = (status, elapsed, rows, error)
        else:
            status, elapsed, rows, error = cache[cache_key]

        matched = [x for x in rows if target_match(x, target)]
        keys = [identity_key(x) for x in matched]
        counts = Counter(keys)
        collisions = sorted(k for k, count in counts.items() if count > 1)

        required = ("movie_code", "movie_name", "theater_code", "play_date", "start_time", "schedule_id")
        missing_required = {
            field: sum(not bool(x.get(field)) for x in matched)
            for field in required
        }
        screen_missing = sum(not bool(x.get("screen")) for x in matched)

        sorted_keys = sorted(set(keys))
        key_hash = hashlib.sha256(
            "\n".join(sorted_keys).encode("utf-8")
        ).hexdigest()[:16]

        valid = (
            status == 200
            and error is None
            and bool(matched)
            and all(value == 0 for value in missing_required.values())
            and not collisions
            and len(sorted_keys) == len(matched)
        )

        check = {
            "target_id": str(target["id"]),
            "label": target.get("label"),
            "site_no": site_no,
            "play_ymd": play_ymd,
            "http_status": status,
            "elapsed_seconds": elapsed,
            "error": error,
            "timetable_rows": len(rows),
            "target_sessions": len(matched),
            "unique_identity_keys": len(sorted_keys),
            "identity_collisions": collisions,
            "missing_required": missing_required,
            "missing_screen_count": screen_missing,
            "identity_hash": key_hash,
            "identity_keys": sorted_keys,
            "valid_shadow_sample": valid,
        }
        result["checks"].append(check)

        print(
            "SHADOW_RESULT "
            f"target={check['target_id']} date={play_ymd} "
            f"http={status} rows={len(rows)} target_sessions={len(matched)} "
            f"unique={len(sorted_keys)} collisions={len(collisions)} "
            f"missing_screen={screen_missing} valid={valid} "
            f"latency={elapsed}s hash={key_hash}"
        )

    result["summary"] = {
        "checks": len(result["checks"]),
        "valid_checks": sum(bool(x["valid_shadow_sample"]) for x in result["checks"]),
        "all_valid": bool(result["checks"]) and all(x["valid_shadow_sample"] for x in result["checks"]),
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
