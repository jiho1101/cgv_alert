import json
import time
from pathlib import Path

import requests

BASE = "https://mcp.aka.page"
SITE_NO = "0128"
PLAY_YMD = "20260928"
TARGET_ALIASES = ("암살자(들)", "암살자들", "암살자")
OUT = Path("public_structured_poc_result.json")


def norm(text):
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def request_json(path, params):
    started = time.monotonic()
    response = requests.get(
        BASE + path,
        params=params,
        headers={"Accept": "application/json"},
        timeout=25,
    )
    elapsed = round(time.monotonic() - started, 3)
    content_type = response.headers.get("content-type", "")
    item = {
        "url_path": path,
        "status": response.status_code,
        "elapsed_seconds": elapsed,
        "content_type": content_type,
    }
    try:
        payload = response.json()
    except ValueError:
        payload = None
    item["payload"] = payload
    return item


def extract_array(payload, preferred):
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in preferred:
            value = data.get(key)
            if isinstance(value, list):
                return value
    for key in preferred:
        value = payload.get(key)
        if isinstance(value, list):
            return value
    return []


def canonical_session(row):
    if not isinstance(row, dict):
        return None
    movie = row.get("movieName") or row.get("movNm") or row.get("prodNm") or ""
    theater = row.get("theaterCode") or row.get("siteNo") or SITE_NO
    date = row.get("playDate") or row.get("scnYmd") or PLAY_YMD
    start = row.get("startTime") or row.get("scnsrtTm") or ""
    screen = (
        row.get("screenName")
        or row.get("screenNm")
        or row.get("scnsNm")
        or row.get("screenNo")
        or row.get("scnsNo")
        or ""
    )
    schedule_id = row.get("scheduleId") or row.get("scnSseq") or ""
    remaining = row.get("remainingSeats")
    if remaining is None:
        remaining = row.get("frSeatCnt")
    if remaining is None:
        remaining = row.get("frtmpSeatCnt")
    return {
        "movie": str(movie),
        "theater": str(theater),
        "date": str(date),
        "screen": str(screen),
        "start": str(start),
        "schedule_id": str(schedule_id),
        "remaining_seats": remaining,
    }


def session_key(session):
    # 상영관명이 비어도 schedule_id가 있으면 안정적으로 구분할 수 있게 포함한다.
    return "|".join(
        (
            norm(session["movie"]),
            session["theater"],
            session["date"],
            norm(session["screen"]),
            session["start"],
            session["schedule_id"],
        )
    )


def main():
    result = {
        "source": BASE,
        "site_no": SITE_NO,
        "play_ymd": PLAY_YMD,
        "checks": [],
        "official_public_probes": [],
    }

    # Independent official paths, ordinary server GET only. An access denial is
    # recorded as a health failure; no alternate network identity is attempted.
    official = [
        ("movie_dates", "https://cgv.co.kr/api/v1/booking/searchSiteScnscYmdListByMov", {"coCd": "A420", "siteNo": SITE_NO, "movNo": "30001323"}),
        ("movie_schedule", "https://cgv.co.kr/api/v1/booking/searchSchByMov", {"coCd": "A420", "siteNo": SITE_NO, "movNo": "30001323", "scnYmd": PLAY_YMD, "rtctlScopCd": "08"}),
        ("booking_page", "https://cgv.co.kr/cnm/movieBook/cinema", {"siteNo": SITE_NO}),
    ]
    for label, url, params in official:
        started = time.monotonic()
        try:
            response = requests.get(url, params=params, timeout=15)
            body = response.text
            item = {
                "name": label,
                "status": response.status_code,
                "elapsed_seconds": round(time.monotonic() - started, 3),
                "content_type": response.headers.get("content-type", ""),
                "has_next_data": "__NEXT_DATA__" in body,
                "has_nuxt_data": "__NUXT__" in body or "__NUXT_DATA__" in body,
                "has_application_json": 'type="application/json"' in body,
            }
            if "json" in item["content_type"]:
                try:
                    payload = response.json()
                    item["valid_json"] = isinstance(payload, dict)
                    item["status_code_field"] = payload.get("statusCode") if isinstance(payload, dict) else None
                    item["data_rows"] = len(payload.get("data")) if isinstance(payload, dict) and isinstance(payload.get("data"), list) else None
                except ValueError:
                    item["valid_json"] = False
        except requests.RequestException as exc:
            item = {"name": label, "error_type": type(exc).__name__, "elapsed_seconds": round(time.monotonic() - started, 3)}
        result["official_public_probes"].append(item)
        print("[official-probe]", json.dumps(item, ensure_ascii=False))

    # 공개 API 자체의 가용성과 결과 일관성을 확인한다.
    previous_keys = None
    for cycle in range(1, 4):
        movies_res = request_json(
            "/api/cgv/movies",
            {"playDate": PLAY_YMD, "theaterCode": SITE_NO},
        )
        tt_res = request_json(
            "/api/cgv/timetable",
            {"playDate": PLAY_YMD, "theaterCode": SITE_NO, "limit": 100},
        )

        movies = extract_array(movies_res.get("payload"), ("movies", "items", "results"))
        raw_timetable = extract_array(
            tt_res.get("payload"),
            ("timetable", "items", "results"),
        )
        sessions = [
            item for item in (canonical_session(row) for row in raw_timetable)
            if item and item["movie"] and item["date"] and item["start"]
        ]
        keys = sorted({session_key(item) for item in sessions})

        alias_norms = [norm(alias) for alias in TARGET_ALIASES]
        target_sessions = [
            item
            for item in sessions
            if any(alias in norm(item["movie"]) for alias in alias_norms)
        ]
        movie_names = sorted({
            str(row.get("movieName") or row.get("movNm") or row.get("name") or "")
            for row in movies
            if isinstance(row, dict)
        })

        check = {
            "cycle": cycle,
            "movies_status": movies_res["status"],
            "movies_elapsed_seconds": movies_res["elapsed_seconds"],
            "timetable_status": tt_res["status"],
            "timetable_elapsed_seconds": tt_res["elapsed_seconds"],
            "movie_count": len(movies),
            "timetable_count": len(sessions),
            "target_movie_listed": any(
                any(alias in norm(name) for alias in alias_norms)
                for name in movie_names
            ),
            "target_session_count": len(target_sessions),
            "same_session_keys_as_previous": (
                None if previous_keys is None else keys == previous_keys
            ),
            "target_sessions": target_sessions,
            "sample_movie_names": movie_names[:30],
            "session_keys": keys,
            "sample_raw_timetable_field_names": sorted(raw_timetable[0]) if raw_timetable and isinstance(raw_timetable[0], dict) else [],
            "sample_raw_target_row": next((row for row in raw_timetable if isinstance(row, dict) and any(alias in norm(row.get('movieName') or row.get('movNm') or '') for alias in alias_norms)), None),
        }
        previous_keys = keys
        if cycle == 1:
            print("[row-schema]", json.dumps({"fields": check["sample_raw_timetable_field_names"], "target": check["sample_raw_target_row"]}, ensure_ascii=False))
        result["checks"].append(check)

        print(
            f"[public-structured] cycle={cycle} "
            f"movies={movies_res['status']}/{len(movies)} "
            f"timetable={tt_res['status']}/{len(sessions)} "
            f"target_listed={check['target_movie_listed']} "
            f"target_sessions={len(target_sessions)} "
            f"same={check['same_session_keys_as_previous']}"
        )
        for row in target_sessions[:20]:
            print(
                "[target-session] "
                f"{row['movie']} {row['date']} {row['start']} "
                f"screen={row['screen'] or '-'} remaining={row['remaining_seats']}"
            )

        if cycle < 3:
            time.sleep(10)

    checks = result["checks"]
    successful = [
        c for c in checks
        if c["movies_status"] == 200 and c["timetable_status"] == 200
    ]
    stable = (
        len(successful) == len(checks)
        and all(c["same_session_keys_as_previous"] is not False for c in checks)
    )
    result["summary"] = {
        "cycles": len(checks),
        "http_success_cycles": len(successful),
        "all_cycles_http_success": len(successful) == len(checks),
        "stable_session_keys": stable,
        "target_listed_cycles": sum(c["target_movie_listed"] for c in checks),
        "target_session_cycles": sum(c["target_session_count"] > 0 for c in checks),
        "max_timetable_count": max((c["timetable_count"] for c in checks), default=0),
    }

    OUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("SUMMARY", json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
