import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

API_URL = "https://m.cgv.co.kr/WebAPP/Reservation/Common/ajaxTheaterScheduleList.aspx/GetTheaterScheduleList"
CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
KST = ZoneInfo("Asia/Seoul")


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def parse_ymd(value: str):
    return datetime.strptime(str(value), "%Y%m%d").date()


def daterange(start_ymd: str, end_ymd: str):
    start = parse_ymd(start_ymd)
    end = parse_ymd(end_ymd)
    if end < start:
        raise ValueError("date_range.end는 start보다 빠를 수 없습니다")
    result = []
    current = start
    while current <= end:
        result.append(current.strftime("%Y%m%d"))
        current += timedelta(days=1)
    return result


def resolve_target_range(target):
    date_range = target.get("date_range") or {}
    if date_range:
        return daterange(date_range["start"], date_range["end"])

    value = str(target.get("date", "today")).strip().lower()
    today = datetime.now(KST).date()
    if value == "today":
        return [today.strftime("%Y%m%d")]
    if value == "tomorrow":
        return [(today + timedelta(days=1)).strftime("%Y%m%d")]
    if value.startswith("today+"):
        days = int(value.split("+", 1)[1])
        return [(today + timedelta(days=days)).strftime("%Y%m%d")]
    if len(value) == 8 and value.isdigit():
        parse_ymd(value)
        return [value]
    raise ValueError(f"지원하지 않는 날짜 형식: {value}")


def is_due(interval_minutes: int, now: datetime) -> bool:
    interval_minutes = max(int(interval_minutes), 5)
    minute_of_day = now.hour * 60 + now.minute
    return (minute_of_day % interval_minutes) < 5


def planned_dates(target, now: datetime):
    all_dates = resolve_target_range(target)
    if len(all_dates) <= 1:
        return all_dates

    strategy = target.get("scan_strategy", {})
    priority_range = strategy.get("priority_range") or {}
    priority_dates = all_dates
    if priority_range:
        p_start = str(priority_range.get("start", all_dates[0]))
        p_end = str(priority_range.get("end", all_dates[-1]))
        priority_dates = [d for d in all_dates if p_start <= d <= p_end]

    fast_mode_from = str(strategy.get("fast_mode_from", all_dates[0]))
    today_ymd = now.strftime("%Y%m%d")

    if today_ymd < fast_mode_from:
        preopen_interval = int(strategy.get("preopen_interval_minutes", 360))
        return priority_dates if is_due(preopen_interval, now) else []

    selected = set()
    priority_interval = int(strategy.get("priority_interval_minutes", 15))
    full_interval = int(strategy.get("full_interval_minutes", 120))

    if is_due(priority_interval, now):
        selected.update(priority_dates)
    if is_due(full_interval, now):
        selected.update(all_dates)

    return sorted(selected)


def fetch_schedule(theater_code: str, play_ymd: str, timeout: int):
    payload = {
        "strRequestType": "THEATER",
        "strUserID": "",
        "strMovieGroupCd": "",
        "strMovieTypeCd": "",
        "strPlayYMD": play_ymd,
        "strTheaterCd": theater_code,
        "strScreenTypeCd": "",
        "strRankType": "MOVIE",
    }
    headers = {
        "Cache-Control": "no-cache",
        "Accept": "application/json",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Content-Type": "application/json; charset=UTF-8",
        "Host": "m.cgv.co.kr",
        "Origin": "https://m.cgv.co.kr",
        "Referer": "https://m.cgv.co.kr/WebApp/Reservation/QuickResult.aspx",
        "X-Requested-With": "XMLHttpRequest",
        "Cookie": "URL_PREV_COMMON=https%253a%252f%252fm.cgv.co.kr%252fWebApp%252fReservation%252fQuickResult.aspx",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36",
    }

    try:
        response = requests.post(API_URL, json=payload, headers=headers, timeout=timeout)
    except requests.RequestException as exc:
        raise RuntimeError(f"CGV 시간표 요청 실패 ({type(exc).__name__})") from None

    if response.status_code != 200:
        raise RuntimeError(f"CGV 시간표 요청 실패 (HTTP {response.status_code})")

    try:
        outer = response.json()
        data = outer.get("d", outer) if isinstance(outer, dict) else outer
        if isinstance(data, str):
            data = json.loads(data)
    except (ValueError, TypeError, json.JSONDecodeError):
        content_type = response.headers.get("Content-Type", "unknown").split(";", 1)[0]
        raise RuntimeError(f"CGV 응답을 JSON으로 해석하지 못했습니다 (Content-Type: {content_type})") from None

    if isinstance(data, dict):
        result_code = str(data.get("ResultCode", ""))
        if result_code and result_code != "00000":
            raise RuntimeError(f"CGV API 오류 코드: {result_code}")

    sessions = []

    def walk(node):
        if isinstance(node, dict):
            if "MovieNmKor" in node and "PlayStartTm" in node:
                sessions.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)

    unique = {}
    for row in sessions:
        key = (
            str(row.get("MovieGroupCd", "")),
            str(row.get("ScreenCd", "")),
            str(row.get("PlayYmd", play_ymd)),
            str(row.get("PlayStartTm", "")),
            str(row.get("MovieNmKor", "")),
        )
        unique[key] = row

    return list(unique.values())


def as_int(value, default=0):
    try:
        return int(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def normalize_title(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(text)).casefold()


def movie_matches(movie_name: str, target) -> bool:
    aliases = target.get("movie_aliases", [])
    if aliases:
        normalized_name = normalize_title(movie_name)
        return any(normalize_title(alias) in normalized_name for alias in aliases)

    keywords = target.get("movie_keywords", [])
    if not keywords:
        return True
    lowered = movie_name.casefold()
    return any(str(keyword).casefold() in lowered for keyword in keywords)


def contains_any(text: str, keywords) -> bool:
    if not keywords:
        return True
    lowered = text.casefold()
    return any(str(keyword).casefold() in lowered for keyword in keywords)


def session_key(theater_code: str, play_ymd: str, row: dict) -> str:
    parts = [
        theater_code,
        play_ymd,
        str(row.get("MovieGroupCd", row.get("MovieNmKor", ""))),
        str(row.get("ScreenCd", row.get("ScreenNm", ""))),
        str(row.get("PlayStartTm", "")),
    ]
    return "|".join(parts)


def filter_sessions(rows, target, play_ymd):
    screen_keywords = target.get("screen_keywords", [])
    require_sale_open = bool(target.get("require_sale_open", True))
    min_remaining = as_int(target.get("min_remaining_seats", 0), 0)

    matched = []
    for row in rows:
        movie_name = str(row.get("MovieNmKor", "")).strip()
        screen_text = " ".join(
            str(row.get(field, "")).strip()
            for field in ("ScreenNm", "MovieAttrNm", "ScreenRatingCd")
        )

        if not movie_matches(movie_name, target):
            continue
        if not contains_any(screen_text, screen_keywords):
            continue

        if require_sale_open:
            allow_sale = str(row.get("AllowSaleYn", "Y")).strip().upper()
            if allow_sale not in {"Y", "1", "TRUE"}:
                continue

        remaining = as_int(row.get("SeatRemainCnt", 0), 0)
        if min_remaining > 0 and remaining < min_remaining:
            continue

        normalized = dict(row)
        normalized["_key"] = session_key(str(target["theater_code"]), play_ymd, row)
        normalized["_remaining"] = remaining
        normalized["_capacity"] = as_int(row.get("SeatCapacity", 0), 0)
        matched.append(normalized)

    matched.sort(key=lambda row: str(row.get("PlayStartTm", "")))
    return matched


def pretty_time(value) -> str:
    raw = str(value or "").zfill(4)
    if len(raw) == 4 and raw.isdigit():
        return f"{raw[:2]}:{raw[2:]}"
    return str(value or "-")


def build_message(target, play_ymd, sessions):
    date_text = datetime.strptime(play_ymd, "%Y%m%d").strftime("%Y-%m-%d")
    label = target.get("label") or target.get("id") or "CGV 알림"
    theater = target.get("theater_name") or f"CGV {target['theater_code']}"

    lines = [
        "🎬 CGV 예매 오픈 감지",
        f"영화: {label}",
        f"극장: {theater}",
        f"가장 빠른 확인 날짜: {date_text}",
        "",
    ]

    for row in sessions[:15]:
        movie = str(row.get("MovieNmKor", "영화명 미확인"))
        screen = str(row.get("ScreenNm", "상영관 미확인"))
        attr = str(row.get("MovieAttrNm", "")).strip()
        start = pretty_time(row.get("PlayStartTm"))
        remain = row.get("_remaining", 0)
        capacity = row.get("_capacity", 0)
        seat_text = f"잔여 {remain}/{capacity}" if capacity else f"잔여 {remain}"
        extra = f" · {attr}" if attr else ""
        lines.append(f"• {start} · {movie} · {screen}{extra} · {seat_text}")

    if len(sessions) > 15:
        lines.append(f"• 외 {len(sessions) - 15}개 회차")

    lines.extend(["", "예매는 CGV 공식 앱/웹사이트에서 직접 진행하세요."])
    return "\n".join(lines)


def post_telegram(token: str, chat_id: str, message: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
            timeout=15,
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"Telegram 전송 실패 ({type(exc).__name__})") from None
    if response.status_code != 200:
        raise RuntimeError(f"Telegram 전송 실패 (HTTP {response.status_code})")


def post_discord(webhook_url: str, message: str):
    try:
        response = requests.post(webhook_url, json={"content": message[:1900]}, timeout=15)
    except requests.RequestException as exc:
        raise RuntimeError(f"Discord 전송 실패 ({type(exc).__name__})") from None
    if response.status_code not in {200, 204}:
        raise RuntimeError(f"Discord 전송 실패 (HTTP {response.status_code})")


def send_notifications(message: str) -> bool:
    telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
    discord_webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()

    configured = 0
    succeeded = 0
    errors = []

    if telegram_token and telegram_chat_id:
        configured += 1
        try:
            post_telegram(telegram_token, telegram_chat_id, message)
            succeeded += 1
            print("Telegram 알림 전송 완료")
        except RuntimeError as exc:
            errors.append(str(exc))

    if discord_webhook:
        configured += 1
        try:
            post_discord(discord_webhook, message)
            succeeded += 1
            print("Discord 알림 전송 완료")
        except RuntimeError as exc:
            errors.append(str(exc))

    if configured == 0:
        print("알림 Secret이 아직 설정되지 않아 실제 알림은 보내지 않았습니다.")
        return False

    if succeeded == 0:
        raise RuntimeError(" / ".join(errors) if errors else "모든 알림 전송이 실패했습니다")

    for error in errors:
        print(f"경고: {error}", file=sys.stderr)
    return True


def run_self_test(timeout: int):
    today = datetime.now(KST).strftime("%Y%m%d")
    rows = fetch_schedule("0128", today, timeout)
    print(f"CGV 울산삼산 공개 시간표 API 자체점검 완료: {len(rows)}개 회차 응답")


def run_checker():
    config = load_json(CONFIG_PATH, {"targets": []})
    state = load_json(STATE_PATH, {"version": 1, "seen": {}})
    state.setdefault("version", 1)
    state.setdefault("seen", {})

    timeout = as_int(config.get("request", {}).get("timeout_seconds", 15), 15)
    targets = [target for target in config.get("targets", []) if target.get("enabled", False)]
    if not targets:
        print("활성화된 감시 조건이 없습니다.")
        return

    now = datetime.now(KST)
    target_by_id = {}
    scheduled = defaultdict(lambda: defaultdict(list))

    for target in targets:
        target_id = str(target.get("id") or target.get("label") or target.get("theater_code"))
        target_by_id[target_id] = target
        theater_code = str(target["theater_code"]).strip()
        dates = planned_dates(target, now)
        for play_ymd in dates:
            scheduled[theater_code][play_ymd].append(target_id)

    if not scheduled:
        print("이번 5분 실행은 CGV 조회 차례가 아닙니다. 불필요한 반복 요청을 건너뜁니다.")
        return

    cache = {}

    def get_rows(theater_code, play_ymd):
        key = (theater_code, play_ymd)
        if key not in cache:
            print(f"극장 {theater_code}, 날짜 {play_ymd} 확인")
            cache[key] = fetch_schedule(theater_code, play_ymd, timeout)
        return cache[key]

    found = defaultdict(dict)
    for theater_code, dates in scheduled.items():
        for play_ymd, target_ids in sorted(dates.items()):
            rows = get_rows(theater_code, play_ymd)
            for target_id in target_ids:
                matched = filter_sessions(rows, target_by_id[target_id], play_ymd)
                if matched:
                    found[target_id][play_ymd] = matched

    for target_id, by_date in list(found.items()):
        if not by_date:
            continue
        target = target_by_id[target_id]
        theater_code = str(target["theater_code"]).strip()
        candidate = min(by_date)
        earlier_dates = [d for d in resolve_target_range(target) if d < candidate]
        for play_ymd in earlier_dates:
            rows = get_rows(theater_code, play_ymd)
            matched = filter_sessions(rows, target, play_ymd)
            if matched:
                by_date[play_ymd] = matched

    state_changed = False

    for target_id, target in target_by_id.items():
        by_date = found.get(target_id, {})
        if not by_date:
            print(f"[{target_id}] 예매 가능한 대상 회차 없음")
            continue

        earliest_date = min(by_date)
        matched = by_date[earliest_date]
        seen = set(state["seen"].get(target_id, []))
        new_sessions = [row for row in matched if row["_key"] not in seen]
        print(f"[{target_id}] 가장 빠른 날짜 {earliest_date} / 신규 {len(new_sessions)}개")

        if not new_sessions:
            continue

        message = build_message(target, earliest_date, new_sessions)
        print(message)

        if send_notifications(message):
            seen.update(row["_key"] for row in new_sessions)
            state["seen"][target_id] = sorted(seen)[-2000:]
            state_changed = True

    if state_changed:
        save_state(state)
        print("중복 알림 방지 상태를 state.json에 저장했습니다.")


def main():
    parser = argparse.ArgumentParser(description="CGV reservation alert checker")
    parser.add_argument("--self-test", action="store_true", help="CGV 공개 시간표 API 연결만 확인")
    args = parser.parse_args()

    config = load_json(CONFIG_PATH, {"request": {}})
    timeout = as_int(config.get("request", {}).get("timeout_seconds", 15), 15)

    try:
        if args.self_test:
            run_self_test(timeout)
        else:
            run_checker()
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
