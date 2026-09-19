import argparse
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

import requests
from selenium import webdriver
from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait

CONFIG_PATH = Path("config.json")
STATE_PATH = Path("state.json")
KST = ZoneInfo("Asia/Seoul")
BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"


def load_json(path: Path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state):
    with STATE_PATH.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2, sort_keys=True)
        f.write("\n")


def save_config(config):
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)
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
    return [str(target["date"])]


def target_end_ymd(target):
    date_range = target.get("date_range") or {}
    if date_range:
        return str(date_range.get("end", ""))
    return str(target.get("date", ""))


def prune_expired_targets(config, state, today_ymd: str):
    removed_ids = []
    kept = []
    for target in config.get("targets", []):
        end_ymd = target_end_ymd(target)
        if end_ymd and end_ymd < today_ymd:
            removed_ids.append(str(target.get("id", "")))
            print(
                f"[자동정리] 감시 종료 대상 삭제: "
                f"{target.get('label', target.get('id', 'unknown'))} ({end_ymd})"
            )
            continue
        kept.append(target)

    config_changed = len(kept) != len(config.get("targets", []))
    if config_changed:
        config["targets"] = kept

    state_changed = False
    seen = state.setdefault("seen", {})
    for target_id in removed_ids:
        if target_id and target_id in seen:
            seen.pop(target_id, None)
            state_changed = True

    pages = state.setdefault("health", {}).setdefault("pages", {})
    for page_id in list(pages):
        parts = page_id.split("|", 1)
        if len(parts) == 2 and parts[1] < today_ymd:
            pages.pop(page_id, None)
            state_changed = True

    return config_changed, state_changed


def is_due(interval_minutes: int, now: datetime) -> bool:
    interval_minutes = max(int(interval_minutes), 5)
    minute_of_day = now.hour * 60 + now.minute
    return (minute_of_day % interval_minutes) < 5


def planned_dates(target, now: datetime):
    all_dates = resolve_target_range(target)
    if len(all_dates) <= 1:
        return all_dates

    strategy = target.get("scan_strategy", {})
    priority = strategy.get("priority_range") or {}
    p_start = str(priority.get("start", all_dates[0]))
    p_end = str(priority.get("end", all_dates[-1]))
    priority_dates = [d for d in all_dates if p_start <= d <= p_end]

    fast_mode_from = str(strategy.get("fast_mode_from", all_dates[0]))
    today_ymd = now.strftime("%Y%m%d")

    if today_ymd < fast_mode_from:
        interval = int(strategy.get("preopen_interval_minutes", 360))
        return priority_dates if is_due(interval, now) else []

    selected = set()
    if is_due(int(strategy.get("priority_interval_minutes", 15)), now):
        selected.update(priority_dates)
    if is_due(int(strategy.get("full_interval_minutes", 120)), now):
        selected.update(all_dates)
    return sorted(selected)


def normalize(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(text)).casefold()


def target_aliases(target):
    aliases = target.get("movie_aliases") or [target.get("label", "")]
    return [normalize(alias) for alias in aliases if str(alias).strip()]


def page_site_name(target):
    name = str(target.get("site_name") or target.get("theater_name") or "").strip()
    return name.removeprefix("CGV ").strip()


class CgvBrowser:
    def __init__(self, timeout=15):
        options = Options()
        options.add_argument("--headless=new")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_argument("--lang=ko-KR")
        options.add_argument(
            "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
        )
        options.page_load_strategy = "eager"
        try:
            self.driver = webdriver.Chrome(options=options)
        except WebDriverException as exc:
            raise RuntimeError(f"Chrome 시작 실패 ({type(exc).__name__})") from None
        self.timeout = timeout
        self.last_request_at = 0.0

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    def fetch_text(
        self,
        site_no: str,
        site_name: str,
        play_ymd: str,
        attempts: int = 2,
        retry_delay: float = 3.0,
    ) -> str:
        url = (
            f"{BOOKING_URL}?siteNo={quote(site_no)}&siteNm={quote(site_name)}"
            f"&scnYmd={quote(play_ymd)}"
        )
        attempts = max(1, int(attempts))
        last_error = None

        for attempt in range(1, attempts + 1):
            elapsed = time.monotonic() - self.last_request_at
            if self.last_request_at and elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            try:
                self.driver.get(url)
                WebDriverWait(self.driver, self.timeout).until(
                    lambda d: len(d.find_element(By.TAG_NAME, "body").text.strip()) > 120
                )
                time.sleep(1.5)
                text = self.driver.find_element(By.TAG_NAME, "body").text

                lowered = text.casefold()
                blocked = ["access denied", "just a moment", "비정상적인 접근", "captcha"]
                if any(word in lowered for word in blocked):
                    raise RuntimeError("CGV가 GitHub Actions 브라우저 접속을 제한했습니다")
                return text
            except TimeoutException:
                last_error = RuntimeError("CGV 예매 페이지 로딩 시간 초과")
            except WebDriverException as exc:
                last_error = RuntimeError(
                    f"CGV 예매 페이지 확인 실패 ({type(exc).__name__})"
                )
            finally:
                self.last_request_at = time.monotonic()

            if attempt < attempts:
                print(
                    f"CGV 페이지 일시 오류 - {retry_delay:g}초 후 재시도 "
                    f"({attempt + 1}/{attempts})"
                )
                try:
                    self.driver.execute_script("window.stop();")
                except WebDriverException:
                    pass
                time.sleep(retry_delay)

        raise last_error from None


def find_screen_name(lines, time_index):
    keywords = ("imax", "4dx", "screenx", "관", "cinema", "box")
    for i in range(time_index - 1, max(-1, time_index - 9), -1):
        candidate = lines[i].strip()
        lowered = candidate.casefold()
        if any(keyword in lowered for keyword in keywords) and not re.search(r"\d{1,2}:\d{2}", candidate):
            return candidate[:80]
    return "상영관 정보 확인 필요"


def extract_sessions(body_text: str, target, play_ymd: str):
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    aliases = target_aliases(target)
    title_indexes = [
        i for i, line in enumerate(lines)
        if any(alias and alias in normalize(line) for alias in aliases)
    ]
    if not title_indexes:
        return []

    sessions = []
    seen_sessions = set()
    for title_index in title_indexes:
        chunk_end = min(len(lines), title_index + 70)
        for i in range(title_index + 1, chunk_end):
            matches = re.findall(r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)", lines[i])
            for hour, minute in matches:
                hour_int = int(hour)
                if hour_int > 29:
                    continue
                display = f"{hour_int:02d}:{minute}"
                screen_name = find_screen_name(lines, i)
                session_identity = (screen_name, display)
                if session_identity in seen_sessions:
                    continue
                seen_sessions.add(session_identity)
                sessions.append(
                    {
                        "MovieNmKor": target.get("label", "영화"),
                        "PlayStartTm": display.replace(":", ""),
                        "PlayYmd": play_ymd,
                        "ScreenNm": screen_name,
                        "_key": (
                            f"{target['theater_code']}|{play_ymd}|{target['id']}|"
                            f"{screen_name}|{display}"
                        ),
                    }
                )
        if sessions:
            break

    sessions.sort(key=lambda row: (row["PlayStartTm"], row.get("ScreenNm", "")))
    return sessions


def pretty_time(value) -> str:
    raw = str(value or "").replace(":", "").zfill(4)
    if len(raw) == 4 and raw.isdigit():
        return f"{raw[:2]}:{raw[2:]}"
    return str(value or "-")


def build_hierarchical_message(notification_items):
    checked_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    hierarchy = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    new_keys = set()

    for item in notification_items:
        target = item["target"]
        movie_name = target.get("label", target["id"])
        theater_name = target.get("theater_name", "CGV")
        new_keys.update(item["new_keys"])

        for play_ymd, sessions in item["by_date"].items():
            hierarchy[movie_name][theater_name][play_ymd].extend(sessions)

    lines = [
        "🎬 CGV 예매 오픈 감지",
        f"확인시각: {checked_at}",
        "",
    ]

    for movie_name in sorted(hierarchy):
        lines.append(f"■ 영화: {movie_name}")

        for theater_name in sorted(hierarchy[movie_name]):
            lines.append(f"  └ 극장: {theater_name}")

            for play_ymd in sorted(hierarchy[movie_name][theater_name]):
                date_text = datetime.strptime(play_ymd, "%Y%m%d").strftime("%Y-%m-%d")
                sessions = hierarchy[movie_name][theater_name][play_ymd]

                unique = {}
                for row in sessions:
                    unique[row["_key"]] = row
                ordered = sorted(
                    unique.values(),
                    key=lambda row: (row.get("PlayStartTm", ""), row.get("ScreenNm", "")),
                )

                lines.append(f"     └ 날짜: {date_text} ({len(ordered)}회차)")
                for row in ordered:
                    mark = " 🆕" if row["_key"] in new_keys else ""
                    lines.append(
                        f"        {pretty_time(row.get('PlayStartTm'))} · "
                        f"{row.get('ScreenNm') or '상영관 정보 확인 필요'}{mark}"
                    )
            lines.append("")
        lines.append("")

    lines.extend([
        "※ 알림 발송 시점에 확인된 현재 정보입니다.",
        "예매: https://cgv.co.kr/cnm/movieBook/cinema",
    ])
    return "\n".join(lines).strip()


def split_discord_message(message: str, limit=1900):
    if len(message) <= limit:
        return [message]

    chunks = []
    current = []
    current_len = 0
    for line in message.splitlines():
        line_len = len(line) + 1
        if current and current_len + line_len > limit:
            chunks.append("\n".join(current))
            current = []
            current_len = 0
        if len(line) > limit:
            if current:
                chunks.append("\n".join(current))
                current = []
                current_len = 0
            chunks.append(line[:limit])
            continue
        current.append(line)
        current_len += line_len

    if current:
        chunks.append("\n".join(current))
    return chunks


def send_discord(message: str) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("DISCORD_WEBHOOK_URL Secret이 아직 없어 실제 알림은 보내지 않았습니다.")
        return False

    chunks = split_discord_message(message)
    for index, chunk in enumerate(chunks, start=1):
        if len(chunks) > 1:
            chunk = f"[{index}/{len(chunks)}]\n{chunk}"
        try:
            response = requests.post(webhook, json={"content": chunk}, timeout=15)
        except requests.RequestException as exc:
            raise RuntimeError(f"Discord 전송 실패 ({type(exc).__name__})") from None
        if response.status_code not in {200, 204}:
            raise RuntimeError(f"Discord 전송 실패 (HTTP {response.status_code})")
    print("Discord 알림 전송 완료")
    return True


def send_health_message(message: str) -> bool:
    try:
        return send_discord(message)
    except RuntimeError as exc:
        print(f"경고: 감시 상태 Discord 알림 전송 실패: {exc}")
        return False


def update_page_health(state, page_results, now: datetime) -> bool:
    pages = state.setdefault("health", {}).setdefault("pages", {})
    changed = False
    alert_after = timedelta(minutes=30)

    for page_id, result in page_results.items():
        entry = pages.get(page_id)

        if result["ok"]:
            if not entry:
                continue

            if entry.get("alerted"):
                first_failure_at = entry.get("first_failure_at", "-")
                message = (
                    "✅ **CGV 감시 복구**\n"
                    f"- 극장: {result['theater_name']}\n"
                    f"- 날짜: {result['play_ymd']}\n"
                    f"- 장애 시작: {first_failure_at}\n"
                    f"- 복구 확인: {now.strftime('%Y-%m-%d %H:%M:%S KST')}"
                )
                if not send_health_message(message):
                    continue

            pages.pop(page_id, None)
            changed = True
            continue

        if not entry:
            entry = {
                "first_failure_at": now.isoformat(),
                "alerted": False,
                "theater_name": result["theater_name"],
                "play_ymd": result["play_ymd"],
                "last_error": result["error"],
            }
            pages[page_id] = entry
            changed = True
        elif entry.get("last_error") != result["error"]:
            entry["last_error"] = result["error"]
            changed = True

        try:
            first_failure = datetime.fromisoformat(entry["first_failure_at"])
        except (KeyError, TypeError, ValueError):
            entry["first_failure_at"] = now.isoformat()
            first_failure = now
            changed = True

        if now - first_failure >= alert_after and not entry.get("alerted", False):
            message = (
                "⚠️ **CGV 감시 이상**\n"
                f"- 극장: {result['theater_name']}\n"
                f"- 날짜: {result['play_ymd']}\n"
                "- 상태: 30분 이상 정상 조회 실패\n"
                f"- 최근 오류: {result['error']}\n"
                f"- 확인 시각: {now.strftime('%Y-%m-%d %H:%M:%S KST')}"
            )
            if send_health_message(message):
                entry["alerted"] = True
                entry["alerted_at"] = now.isoformat()
                changed = True

    return changed


def build_alert_embeds(notification_items):
    checked_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    embeds = []

    for item in notification_items:
        target = item["target"]
        movie_name = target.get("label", target["id"])
        theater_name = target.get("theater_name", "CGV")
        new_keys = item["new_keys"]
        new_count = len(new_keys)
        pending_fields = []

        for play_ymd in sorted(item["by_date"]):
            date_text = datetime.strptime(play_ymd, "%Y%m%d").strftime("%Y-%m-%d")
            unique = {}
            for row in item["by_date"][play_ymd]:
                unique[row["_key"]] = row
            ordered = sorted(
                unique.values(),
                key=lambda row: (row.get("PlayStartTm", ""), row.get("ScreenNm", "")),
            )

            session_lines = []
            for row in ordered:
                mark = "🆕 " if row["_key"] in new_keys else ""
                session_lines.append(
                    f"{mark}`{pretty_time(row.get('PlayStartTm'))}` · "
                    f"{row.get('ScreenNm') or '상영관 정보 확인 필요'}"
                )

            value = "\n".join(session_lines) or "회차 정보 없음"
            while value:
                chunk = value[:950]
                value = value[950:]
                pending_fields.append({
                    "name": f"📅 {date_text} · {len(ordered)}회차",
                    "value": chunk,
                    "inline": False,
                })

        current_fields = []
        current_chars = 0
        part = 1
        for field in pending_fields:
            field_chars = len(field["name"]) + len(field["value"])
            if current_fields and (len(current_fields) >= 20 or current_chars + field_chars > 4500):
                embeds.append({
                    "title": f"🎬 {movie_name}" + (f" · {part}" if part > 1 else ""),
                    "url": BOOKING_URL,
                    "description": f"**극장** {theater_name}\n**신규 회차** {new_count}개",
                    "fields": current_fields,
                    "footer": {"text": f"CGV 예매 오픈 감지 · {checked_at}"},
                })
                current_fields = []
                current_chars = 0
                part += 1
            current_fields.append(field)
            current_chars += field_chars

        if current_fields or not pending_fields:
            embeds.append({
                "title": f"🎬 {movie_name}" + (f" · {part}" if part > 1 else ""),
                "url": BOOKING_URL,
                "description": f"**극장** {theater_name}\n**신규 회차** {new_count}개",
                "fields": current_fields,
                "footer": {"text": f"CGV 예매 오픈 감지 · {checked_at}"},
            })

    return embeds


def send_discord_embeds(embeds) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("DISCORD_WEBHOOK_URL Secret이 아직 없어 실제 알림은 보내지 않았습니다.")
        return False

    for index, embed in enumerate(embeds):
        payload = {"embeds": [embed]}
        if index == 0:
            payload["content"] = "🎟️ **CGV 예매 오픈 감지**"
        try:
            response = requests.post(webhook, json=payload, timeout=15)
        except requests.RequestException as exc:
            raise RuntimeError(f"Discord 전송 실패 ({type(exc).__name__})") from None
        if response.status_code not in {200, 204}:
            raise RuntimeError(f"Discord 전송 실패 (HTTP {response.status_code})")

    print("Discord Embed 알림 전송 완료")
    return True


def run_self_test(timeout: int):
    browser = CgvBrowser(timeout=timeout)
    try:
        today = datetime.now(KST).strftime("%Y%m%d")
        text = browser.fetch_text("0128", "울산삼산", today)
        if len(text.strip()) < 120:
            raise RuntimeError("CGV 예매 페이지 내용이 비어 있습니다")
        print(f"CGV 울산삼산 브라우저 자체점검 완료: 본문 {len(text)}자")
    finally:
        browser.close()


def run_checker():
    config = load_json(CONFIG_PATH, {"targets": []})
    state = load_json(STATE_PATH, {"version": 1, "seen": {}})
    state.setdefault("version", 1)
    state.setdefault("seen", {})
    state.setdefault("health", {}).setdefault("pages", {})

    now = datetime.now(KST)
    config_changed, state_changed = prune_expired_targets(
        config, state, now.strftime("%Y%m%d")
    )
    if config_changed:
        save_config(config)
    if state_changed:
        save_state(state)

    timeout = int(config.get("request", {}).get("timeout_seconds", 15))
    targets = [target for target in config.get("targets", []) if target.get("enabled", False)]
    if not targets:
        print("활성화된 감시 조건이 없습니다.")
        return

    target_by_id = {str(t["id"]): t for t in targets}
    scheduled = defaultdict(lambda: defaultdict(set))
    for target in targets:
        for play_ymd in planned_dates(target, now):
            scheduled[str(target["theater_code"])][play_ymd].add(str(target["id"]))

    if not scheduled:
        print("이번 5분 실행은 CGV 조회 차례가 아니므로 외부 요청을 건너뜁니다.")
        return

    browser = CgvBrowser(timeout=timeout)
    page_cache = {}
    page_results = {}
    found = defaultdict(dict)

    try:
        def get_text(target, play_ymd):
            key = (str(target["theater_code"]), play_ymd)
            page_id = f"{target['theater_code']}|{play_ymd}"
            if key not in page_cache:
                print(f"[{target['theater_name']}] {play_ymd} 확인")
                try:
                    page_cache[key] = browser.fetch_text(
                        str(target["theater_code"]), page_site_name(target), play_ymd
                    )
                    page_results[page_id] = {
                        "ok": True,
                        "theater_name": target["theater_name"],
                        "play_ymd": play_ymd,
                    }
                except RuntimeError as exc:
                    message = str(exc)
                    print(
                        f"경고: [{target['theater_name']}] {play_ymd} "
                        f"조회 실패로 이번 회차만 건너뜁니다: {message}"
                    )
                    page_cache[key] = None
                    page_results[page_id] = {
                        "ok": False,
                        "theater_name": target["theater_name"],
                        "play_ymd": play_ymd,
                        "error": message,
                    }
            return page_cache[key]

        for theater_code, dates in scheduled.items():
            for play_ymd, target_ids in sorted(dates.items()):
                sample_target = target_by_id[next(iter(target_ids))]
                text = get_text(sample_target, play_ymd)
                if not text:
                    continue
                for target_id in target_ids:
                    sessions = extract_sessions(text, target_by_id[target_id], play_ymd)
                    if sessions:
                        found[target_id][play_ymd] = sessions

        # 후보가 생기면 그보다 앞선 날짜를 즉시 확인해 감시 범위 내 최초 날짜를 확정한다.
        for target_id, by_date in list(found.items()):
            target = target_by_id[target_id]
            candidate = min(by_date)
            for earlier in [d for d in resolve_target_range(target) if d < candidate]:
                text = get_text(target, earlier)
                if not text:
                    continue
                sessions = extract_sessions(text, target, earlier)
                if sessions:
                    by_date[earlier] = sessions

        if update_page_health(state, page_results, now):
            save_state(state)

        notification_items = []
        for target_id, target in target_by_id.items():
            by_date = found.get(target_id, {})
            if not by_date:
                print(f"[{target_id}] 예매 가능한 대상 회차 없음")
                continue

            seen = set(state["seen"].get(target_id, []))
            all_sessions = [
                row
                for play_ymd in sorted(by_date)
                for row in by_date[play_ymd]
            ]
            new_sessions = [row for row in all_sessions if row["_key"] not in seen]
            print(
                f"[{target_id}] 확인 날짜 {len(by_date)}개, "
                f"현재 {len(all_sessions)}개, 신규 {len(new_sessions)}개"
            )
            if not new_sessions:
                continue

            notification_items.append(
                {
                    "target_id": target_id,
                    "target": target,
                    "by_date": by_date,
                    "new_keys": {row["_key"] for row in new_sessions},
                }
            )

        if not notification_items:
            return

        message = build_hierarchical_message(notification_items)
        print(message)
        embeds = build_alert_embeds(notification_items)
        if send_discord_embeds(embeds):
            for item in notification_items:
                target_id = item["target_id"]
                seen = set(state["seen"].get(target_id, []))
                for sessions in item["by_date"].values():
                    seen.update(row["_key"] for row in sessions)
                state["seen"][target_id] = sorted(seen)[-1000:]
            save_state(state)
            print("중복 알림 방지 상태를 저장했습니다.")
    finally:
        browser.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()
    config = load_json(CONFIG_PATH, {"request": {}})
    timeout = int(config.get("request", {}).get("timeout_seconds", 15))
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
