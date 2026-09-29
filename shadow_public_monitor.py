import argparse
import copy
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
STATE_PATH = Path("state.json")
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


def load_json(path, default):
    if not path.exists():
        return copy.deepcopy(default)
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


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

    if not future:
        return []

    # 같은 가장 이른 날짜에 걸린 모든 영화를 확인한다.
    first_day = future[0][0]
    return [
        (target, play_ymd)
        for day, target, play_ymd in future
        if day == first_day
    ]


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
        "total_seats": row.get(
            "totalSeats",
            row.get("stcnt"),
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


def structural_valid(status, error, payload_shape_ok, inspection):
    return (
        status == 200
        and error is None
        and payload_shape_ok
        and not inspection["identity_collisions"]
        and all(v == 0 for v in inspection["missing_required"].values())
        and inspection["unique_identity_keys"] == inspection["rows"]
    )


def parse_clock_minutes(value):
    raw = str(value or "").strip().replace(":", "")
    if len(raw) != 4 or not raw.isdigit():
        return None
    hour = int(raw[:2])
    minute = int(raw[2:])
    if minute > 59 or hour > 47:
        return None
    return hour * 60 + minute


def latest_end_minutes(rows):
    values = []
    for row in rows:
        end = parse_clock_minutes(row.get("end_time"))
        start = parse_clock_minutes(row.get("start_time"))
        if end is not None:
            if start is not None and end < start:
                end += 24 * 60
            values.append(end)
        elif start is not None:
            # 종료 시각이 없을 때만 보수적으로 시작 + 4시간을 사용한다.
            values.append(start + 240)
    return max(values) if values else None


def natural_end_of_day(now, play_ymd, previous):
    if now.strftime("%Y%m%d") != str(play_ymd):
        return False
    latest_end = previous.get("last_nonempty_latest_end_minutes")
    if latest_end is None:
        return False
    current_minutes = now.hour * 60 + now.minute
    # 마지막 회차 종료 후 20분이 지났을 때만 당일 0건을 자연 감소로 인정한다.
    return current_minutes >= int(latest_end) + 20


def new_identity_keys(previous_keys, current_keys):
    return sorted(set(current_keys) - set(previous_keys))


def update_nonempty_snapshot(bucket, key, inspection, rows, now):
    bucket[key] = {
        "last_nonempty_at": now.isoformat(),
        "last_nonempty_count": inspection["rows"],
        "last_nonempty_hash": inspection["identity_hash"],
        "last_nonempty_keys": inspection["identity_keys"],
        "last_nonempty_latest_end_minutes": latest_end_minutes(rows),
    }


def run_self_test():
    base = [
        {
            "movie_code": "M1",
            "movie_name": "테스트 영화",
            "theater_code": "0128",
            "play_date": "20990101",
            "schedule_id": "S1",
            "start_time": "10:00",
            "end_time": "12:00",
            "screen": "",
            "remaining_seats": 100,
        },
        {
            "movie_code": "M1",
            "movie_name": "테스트 영화",
            "theater_code": "0128",
            "play_date": "20990101",
            "schedule_id": "S2",
            "start_time": "13:00",
            "end_time": "15:00",
            "screen": "",
            "remaining_seats": 80,
        },
    ]
    base_inspection = inspect_rows(base)
    baseline = base_inspection["identity_keys"]

    # 좌석 수 변화는 회차 identity에 포함되지 않아 신규로 잡히면 안 된다.
    seat_changed = copy.deepcopy(base)
    seat_changed[0]["remaining_seats"] = 1
    seat_changed_keys = inspect_rows(seat_changed)["identity_keys"]
    if new_identity_keys(baseline, seat_changed_keys):
        raise RuntimeError("Shadow 자체점검 실패: 좌석 변화가 신규 회차로 감지됨")

    # 신규 회차 하나를 넣으면 정확히 하나만 신규여야 한다.
    added = copy.deepcopy(base)
    added.append(
        {
            "movie_code": "M1",
            "movie_name": "테스트 영화",
            "theater_code": "0128",
            "play_date": "20990101",
            "schedule_id": "S3",
            "start_time": "16:00",
            "end_time": "18:00",
            "screen": "",
            "remaining_seats": 120,
        }
    )
    added_keys = inspect_rows(added)["identity_keys"]
    first_new = new_identity_keys(baseline, added_keys)
    if len(first_new) != 1 or "S3" not in first_new[0]:
        raise RuntimeError("Shadow 자체점검 실패: 신규 회차 1개 감지 규칙 오류")

    # 그 신규 회차를 baseline에 반영한 뒤 같은 자료를 다시 보면 0개여야 한다.
    repeated_new = new_identity_keys(added_keys, added_keys)
    if repeated_new:
        raise RuntimeError("Shadow 자체점검 실패: 동일 신규 회차 중복 감지")

    # 직전 정상 스냅샷이 있는데 갑자기 0건이면 정상으로 확정하면 안 된다.
    previous = {
        "last_nonempty_count": 3,
        "last_nonempty_keys": added_keys,
        "last_nonempty_latest_end_minutes": 18 * 60,
    }
    test_now = datetime(2099, 1, 1, 14, 0, tzinfo=KST)
    if natural_end_of_day(test_now, "20990101", previous):
        raise RuntimeError("Shadow 자체점검 실패: 상영 중 0건을 자연 종료로 오판")

    late_now = datetime(2099, 1, 1, 18, 30, tzinfo=KST)
    if not natural_end_of_day(late_now, "20990101", previous):
        raise RuntimeError("Shadow 자체점검 실패: 마지막 상영 종료 뒤 0건 처리 오류")

    print(
        "SHADOW_SELF_TEST PASS · 좌석변화=신규0 · "
        "가짜신규=정확히1 · 반복=신규0 · 갑작스런빈응답=신뢰안함"
    )


def main():
    config = load_json(CONFIG, {"targets": []})
    state = load_json(STATE_PATH, {"version": 2})
    before_shadow_state = copy.deepcopy(
        state.get("public_shadow_validation") or {}
    )
    shadow_state = state.setdefault(
        "public_shadow_validation",
        {"version": 1, "sources": {}, "targets": {}},
    )
    shadow_state.setdefault("version", 1)
    source_state = shadow_state.setdefault("sources", {})
    target_state = shadow_state.setdefault("targets", {})

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

    # 당일 극장 시간표는 데이터 소스 자체가 살아 있는지 확인하는 sentinel.
    for site_no, theater_name in sorted(theaters.items()):
        status, elapsed, rows, error, payload_shape_ok = fetch(site_no, today_ymd)
        inspection = inspect_rows(rows)
        source_key = f"{site_no}|{today_ymd}"
        previous = source_state.get(source_key) or {}
        structural = structural_valid(
            status, error, payload_shape_ok, inspection
        )

        if not structural:
            source_valid = False
            source_state_name = "invalid_structure"
        elif inspection["rows"] > 0:
            source_valid = True
            source_state_name = "nonempty_trusted"
            update_nonempty_snapshot(
                source_state, source_key, inspection, rows, now
            )
        elif natural_end_of_day(now, today_ymd, previous):
            source_valid = True
            source_state_name = "empty_after_last_show"
        else:
            # HTTP 200 + [] 자체는 '정상적인 빈 시간표'로 확정하지 않는다.
            source_valid = False
            source_state_name = (
                "sudden_empty_untrusted"
                if previous.get("last_nonempty_count")
                else "empty_unconfirmed"
            )

        item = {
            "site_no": site_no,
            "theater_name": theater_name,
            "play_ymd": today_ymd,
            "http_status": status,
            "elapsed_seconds": elapsed,
            "error": error,
            "payload_shape_ok": payload_shape_ok,
            **inspection,
            "source_valid": source_valid,
            "source_state": source_state_name,
            "previous_nonempty_count": previous.get("last_nonempty_count"),
        }
        result["source_health"].append(item)
        print(
            "SHADOW_SOURCE "
            f"site={site_no} date={today_ymd} http={status} "
            f"rows={inspection['rows']} unique={inspection['unique_identity_keys']} "
            f"collisions={len(inspection['identity_collisions'])} "
            f"missing_required={sum(inspection['missing_required'].values())} "
            f"valid={source_valid} state={source_state_name} "
            f"latency={elapsed}s hash={inspection['identity_hash']}"
        )

    source_by_site = {
        item["site_no"]: item for item in result["source_health"]
    }

    for target, play_ymd in choose_target_checks(config):
        site_no = str(target["theater_code"])
        status, elapsed, rows, error, payload_shape_ok = fetch(site_no, play_ymd)
        all_inspection = inspect_rows(rows)
        matched = [item for item in rows if target_match(item, target)]
        target_inspection = inspect_rows(matched)

        target_key = f"{target['id']}|{play_ymd}"
        previous = target_state.get(target_key) or {}
        structural = structural_valid(
            status, error, payload_shape_ok, all_inspection
        )
        source_sentinel_ok = bool(
            (source_by_site.get(site_no) or {}).get("source_valid")
        )

        if not structural:
            sample_state = "invalid_structure"
            absence_trusted = False
            valid = False
            new_keys = []
        elif all_inspection["rows"] == 0:
            # 빈 배열로 target 부재를 확정하거나 baseline을 지우지 않는다.
            sample_state = (
                "sudden_empty_untrusted"
                if previous.get("last_nonempty_count")
                else "empty_unconfirmed"
            )
            absence_trusted = False
            valid = False
            new_keys = []
        elif matched:
            identity_valid = (
                not target_inspection["identity_collisions"]
                and all(
                    value == 0
                    for value in target_inspection["missing_required"].values()
                )
                and target_inspection["unique_identity_keys"]
                == target_inspection["rows"]
            )
            previous_keys = previous.get("last_nonempty_keys") or []
            new_keys = (
                new_identity_keys(
                    previous_keys, target_inspection["identity_keys"]
                )
                if previous_keys
                else []
            )
            valid = bool(identity_valid and source_sentinel_ok)
            absence_trusted = False
            sample_state = (
                "target_present_trusted"
                if valid
                else "target_present_source_untrusted"
            )
            if valid:
                update_nonempty_snapshot(
                    target_state,
                    target_key,
                    target_inspection,
                    matched,
                    now,
                )
        else:
            # 같은 극장/날짜에 다른 영화 회차가 실제로 존재하므로
            # 단순 []보다 target 부재에 대한 근거가 강하다.
            valid = bool(source_sentinel_ok)
            absence_trusted = valid
            sample_state = (
                "target_absent_with_nonempty_timetable"
                if valid
                else "target_absent_source_untrusted"
            )
            new_keys = []

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
            "new_identity_keys": new_keys,
            "new_identity_count": len(new_keys),
            "absence_trusted": absence_trusted,
            "sample_state": sample_state,
            "valid_shadow_sample": valid,
            "previous_nonempty_count": previous.get("last_nonempty_count"),
        }
        result["target_checks"].append(item)

        print(
            "SHADOW_TARGET "
            f"target={item['target_id']} date={play_ymd} http={status} "
            f"all_rows={all_inspection['rows']} "
            f"target_sessions={target_inspection['rows']} "
            f"unique={target_inspection['unique_identity_keys']} "
            f"new={len(new_keys)} "
            f"collisions={len(target_inspection['identity_collisions'])} "
            f"missing_screen={target_inspection['missing_screen_count']} "
            f"absence_trusted={absence_trusted} "
            f"sample_valid={valid} state={sample_state} "
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
        "unconfirmed_empty_checks": sum(
            x["sample_state"] in {
                "empty_unconfirmed",
                "sudden_empty_untrusted",
            }
            for x in target_checks
        ),
        "new_identity_candidates": sum(
            int(x["new_identity_count"]) for x in target_checks
        ),
        "notifications_sent": 0,
        "production_detection_modified": False,
    }

    current_shadow_state = state.get("public_shadow_validation") or {}
    if current_shadow_state != before_shadow_state:
        save_json(STATE_PATH, state)

    OUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("SHADOW_SUMMARY " + json.dumps(result["summary"], ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    if args.self_test:
        run_self_test()
    else:
        main()
