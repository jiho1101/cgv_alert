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
RUNTIME_STATUS_PATH = Path("runtime_status.json")
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


def migrate_state(state) -> bool:
    """v1의 혼합 감지 기록을 v2에서 구조화/보조 기록으로 분리한다."""
    try:
        version = int(state.get("version", 1))
    except (TypeError, ValueError):
        version = 1

    if version >= 2:
        return False

    seen = state.setdefault("seen", {})
    fallback_seen = state.setdefault("fallback_seen", {})

    # v1에서는 보조 감지 결과도 seen에 섞여 저장될 수 있었다.
    # 기존 기록을 보조 후보 기록으로 옮기고, 구조화 감지는 새로 기준을 잡는다.
    for target_id, keys in list(seen.items()):
        merged = set(fallback_seen.get(target_id, []))
        merged.update(keys or [])
        fallback_seen[target_id] = sorted(merged)[-1000:]

    state["seen"] = {}
    state["version"] = 2
    print(
        "[상태 마이그레이션] 기존 혼합 감지 기록을 보조 후보 기록으로 "
        "분리했습니다. 다음 정상 구조화 조회에서 실제 회차 기준을 새로 잡습니다."
    )
    return True


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
    fallback_seen = state.setdefault("fallback_seen", {})
    for target_id in removed_ids:
        if target_id and target_id in seen:
            seen.pop(target_id, None)
            state_changed = True
        if target_id and target_id in fallback_seen:
            fallback_seen.pop(target_id, None)
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


def planned_dates(target, now: datetime, force_all: bool = False):
    all_dates = resolve_target_range(target)
    if force_all:
        return all_dates
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


def format_ymd(value: str) -> str:
    value = str(value or "")
    if len(value) == 8 and value.isdigit():
        return f"{value[:4]}-{value[4:6]}-{value[6:8]}"
    return value or "-"


def target_date_text(target) -> str:
    dates = resolve_target_range(target)
    if not dates:
        return "-"
    if len(dates) == 1:
        return format_ymd(dates[0])
    return f"{format_ymd(dates[0])} ~ {format_ymd(dates[-1])}"


def current_interval_text(target, now: datetime) -> str:
    strategy = target.get("scan_strategy") or {}
    if not strategy:
        return "5분"

    today = now.strftime("%Y%m%d")
    for stage in strategy.get("preopen_stages") or []:
        start = str(stage.get("from", "00000000"))
        end = str(stage.get("until", "99999999"))
        if start <= today <= end:
            return f"{int(stage.get('interval_minutes', 360))}분"

    fast_from = str(strategy.get("fast_mode_from", "99999999"))
    if today < fast_from:
        return f"{int(strategy.get('preopen_interval_minutes', 360))}분"

    priority = int(strategy.get("priority_interval_minutes", 15))
    full = int(strategy.get("full_interval_minutes", 120))
    return f"우선 {priority}분 / 전체 {full}분"


def build_runtime_snapshot(
    config, state, targets, now: datetime, page_results, found=None
):
    pages = state.setdefault("health", {}).setdefault("pages", {})
    found = found or {}
    target_items = []
    health_levels = {"normal": 0, "warning": 1, "error": 2}
    overall = "normal"
    recent_error = None
    any_success = False

    for target in targets:
        target_id = str(target["id"])
        theater_code = str(target["theater_code"])
        target_dates = set(resolve_target_range(target))

        matching_results = []
        for page_id, result in page_results.items():
            parts = page_id.split("|", 1)
            if len(parts) != 2:
                continue
            if parts[0] == theater_code and parts[1] in target_dates:
                matching_results.append(result)

        matching_health = []
        for page_id, entry in pages.items():
            parts = page_id.split("|", 1)
            if len(parts) != 2:
                continue
            if parts[0] == theater_code and parts[1] in target_dates:
                matching_health.append(entry)

        success_now = any(result.get("ok") for result in matching_results)
        if success_now:
            any_success = True

        failed_now = next(
            (result for result in matching_results if not result.get("ok")),
            None,
        )
        degraded_now = next(
            (
                result
                for result in matching_results
                if result.get("ok") and result.get("degraded")
            ),
            None,
        )
        alerted = any(entry.get("alerted") for entry in matching_health)

        if alerted:
            health_status = "error"
        elif failed_now or degraded_now or matching_health:
            health_status = "warning"
        else:
            health_status = "normal"

        if health_levels[health_status] > health_levels[overall]:
            overall = health_status

        last_error = None
        if failed_now:
            last_error = failed_now.get("error")
        elif degraded_now:
            last_error = degraded_now.get("error")
        elif matching_health:
            last_error = matching_health[0].get("last_error")

        if last_error and recent_error is None:
            recent_error = last_error

        available_session_count = sum(
            len(rows) for rows in (found.get(target_id) or {}).values()
        )
        detection_mode = (
            "failed"
            if failed_now
            else "fallback"
            if degraded_now
            else "structured"
            if matching_results
            else "not_checked"
        )

        target_items.append(
            {
                "id": target_id,
                "label": target.get("label", target_id),
                "theater_name": target.get("theater_name", "CGV"),
                "date_text": target_date_text(target),
                "interval_text": current_interval_text(target, now),
                "health_status": health_status,
                "last_success_at": now.isoformat() if success_now else None,
                "last_error": last_error,
                "available_session_count": available_session_count,
                "detection_mode": detection_mode,
            }
        )

    return {
        "last_run_at": now.isoformat(),
        "last_cgv_success_at": now.isoformat() if any_success else None,
        "health_summary": overall,
        "recent_error": recent_error,
        "active_count": len(target_items),
        "targets": target_items,
    }


def write_runtime_status(snapshot):
    with RUNTIME_STATUS_PATH.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)
        f.write("\n")


def normalize(text: str) -> str:
    return re.sub(r"[^0-9a-zA-Z가-힣]", "", str(text)).casefold()


def target_aliases(target):
    aliases = target.get("movie_aliases") or [target.get("label", "")]
    return [normalize(alias) for alias in aliases if str(alias).strip()]


def page_site_name(target):
    name = str(target.get("site_name") or target.get("theater_name") or "").strip()
    return name.removeprefix("CGV ").strip()


def summarize_cgv_error(value, limit=160) -> str:
    text = str(value or "").strip()
    if not text:
        return "응답 내용 없음"

    lowered = text.casefold()
    if "<!doctype html" in lowered or "<html" in lowered:
        return "CGV가 HTML 오류/차단 페이지를 반환함"

    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] or "응답 내용 없음"


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
            self.driver.set_script_timeout(max(int(timeout) + 5, 20))
        except WebDriverException as exc:
            raise RuntimeError(f"Chrome 시작 실패 ({type(exc).__name__})") from None
        self.timeout = timeout
        self.last_request_at = 0.0
        self.current_site = None

    def close(self):
        try:
            self.driver.quit()
        except Exception:
            pass

    def _bootstrap(self, site_no: str, site_name: str, play_ymd: str):
        site_key = (str(site_no), str(site_name))
        if self.current_site == site_key:
            return

        url = (
            f"{BOOKING_URL}?siteNo={quote(site_no)}&siteNm={quote(site_name)}"
            f"&scnYmd={quote(play_ymd)}"
        )
        self.driver.get(url)
        WebDriverWait(self.driver, self.timeout).until(
            lambda d: len(d.find_element(By.TAG_NAME, "body").text.strip()) > 120
        )
        time.sleep(1.0)

        text = self.driver.find_element(By.TAG_NAME, "body").text
        lowered = text.casefold()
        blocked = ["access denied", "just a moment", "비정상적인 접근", "captcha"]
        if any(word in lowered for word in blocked):
            raise RuntimeError("CGV가 GitHub Actions 브라우저 접속을 제한했습니다")

        self.current_site = site_key

    def fetch_schedule(
        self,
        site_no: str,
        site_name: str,
        play_ymd: str,
        attempts: int = 2,
        retry_delay: float = 3.0,
    ) -> list:
        attempts = max(1, int(attempts))
        last_error = None

        for attempt in range(1, attempts + 1):
            elapsed = time.monotonic() - self.last_request_at
            if self.last_request_at and elapsed < 1.0:
                time.sleep(1.0 - elapsed)

            try:
                self._bootstrap(site_no, site_name, play_ymd)

                result = self.driver.execute_async_script(
                    """
                    const siteNo = arguments[0];
                    const playYmd = arguments[1];
                    const done = arguments[arguments.length - 1];
                    const url =
                      "/api/v1/booking/searchMovScnInfo" +
                      "?coCd=A420" +
                      "&siteNo=" + encodeURIComponent(siteNo) +
                      "&scnYmd=" + encodeURIComponent(playYmd) +
                      "&rtctlScopCd=08";

                    fetch(url, {
                      method: "GET",
                      credentials: "include",
                      headers: { "Accept": "application/json" },
                    })
                      .then(async (response) => {
                        const text = await response.text();
                        done({
                          ok: response.ok,
                          status: response.status,
                          text,
                        });
                      })
                      .catch((error) => {
                        done({
                          ok: false,
                          status: 0,
                          text: "",
                          error: String(error),
                        });
                      });
                    """,
                    str(site_no),
                    str(play_ymd),
                )

                if not isinstance(result, dict):
                    raise RuntimeError("CGV 상영정보 API 응답을 받지 못했습니다")

                body = str(result.get("text") or "")
                lowered = body.casefold()
                blocked = ["access denied", "just a moment", "비정상적인 접근", "captcha"]
                if any(word in lowered for word in blocked):
                    raise RuntimeError("CGV가 GitHub Actions 브라우저 접속을 제한했습니다")

                if not result.get("ok"):
                    status = result.get("status", 0)
                    if int(status or 0) == 403:
                        raise RuntimeError(
                            "CGV 상영정보 API 접근 거부 (HTTP 403)"
                        )
                    detail = summarize_cgv_error(
                        result.get("error") or body
                    )
                    raise RuntimeError(
                        f"CGV 상영정보 API 조회 실패 "
                        f"(HTTP {status}: {detail})"
                    )

                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    raise RuntimeError("CGV 상영정보 API가 JSON이 아닌 응답을 반환했습니다") from None

                if not isinstance(payload, dict):
                    raise RuntimeError("CGV 상영정보 API 응답 구조가 예상과 다릅니다")

                status_code = payload.get("statusCode")
                if status_code not in (None, 0, "0"):
                    raise RuntimeError(
                        f"CGV 상영정보 API 오류 (statusCode={status_code})"
                    )

                rows = payload.get("data")
                if rows is None:
                    return []
                if not isinstance(rows, list):
                    raise RuntimeError("CGV 상영정보 API data 구조가 예상과 다릅니다")

                return rows
            except TimeoutException:
                last_error = RuntimeError("CGV 상영정보 API 응답 시간 초과")
            except WebDriverException as exc:
                last_error = RuntimeError(
                    f"CGV 상영정보 브라우저 조회 실패 ({type(exc).__name__})"
                )
            except RuntimeError as exc:
                last_error = exc
            finally:
                self.last_request_at = time.monotonic()

            if attempt < attempts:
                print(
                    f"CGV 상영정보 일시 오류 - {retry_delay:g}초 후 재시도 "
                    f"({attempt + 1}/{attempts})"
                )
                self.current_site = None
                try:
                    self.driver.execute_script("window.stop();")
                except WebDriverException:
                    pass
                time.sleep(retry_delay)

        raise last_error from None

    def fetch_text_fallback(
        self,
        site_no: str,
        site_name: str,
        play_ymd: str,
        attempts: int = 2,
        retry_delay: float = 3.0,
    ) -> str:
        """구조화 API가 실패했을 때만 쓰는 보조 경로.

        예매 페이지 본문을 읽어 기존 텍스트 파서로 한 번 더 확인한다.
        오탐 가능성은 있으므로 fallback 결과에는 별도 표시를 붙인다.
        """
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
                    lambda d: len(
                        d.find_element(By.TAG_NAME, "body").text.strip()
                    ) > 120
                )
                time.sleep(1.5)
                text = self.driver.find_element(By.TAG_NAME, "body").text
                lowered = text.casefold()
                blocked = [
                    "access denied",
                    "just a moment",
                    "비정상적인 접근",
                    "captcha",
                ]
                if any(word in lowered for word in blocked):
                    raise RuntimeError(
                        "CGV가 GitHub Actions 브라우저 접속을 제한했습니다"
                    )
                return text
            except TimeoutException:
                last_error = RuntimeError("CGV 예매 페이지 보조 확인 시간 초과")
            except WebDriverException as exc:
                last_error = RuntimeError(
                    f"CGV 예매 페이지 보조 확인 실패 ({type(exc).__name__})"
                )
            except RuntimeError as exc:
                last_error = exc
            finally:
                self.last_request_at = time.monotonic()

            if attempt < attempts:
                print(
                    f"CGV 보조 확인 일시 오류 - {retry_delay:g}초 후 재시도 "
                    f"({attempt + 1}/{attempts})"
                )
                time.sleep(retry_delay)

        raise last_error from None


def _safe_int(value, default=0):
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def extract_sessions(api_rows: list, target, play_ymd: str):
    aliases = target_aliases(target)
    screen_keywords = [
        normalize(keyword)
        for keyword in (target.get("screen_keywords") or [])
        if str(keyword).strip()
    ]
    min_remaining = max(_safe_int(target.get("min_remaining_seats"), 0), 0)

    sessions = []
    seen_sessions = set()

    for row in api_rows:
        if not isinstance(row, dict):
            continue

        row_date = str(row.get("scnYmd") or "")
        if row_date and row_date != str(play_ymd):
            continue

        movie_name = str(row.get("movNm") or "")
        product_name = str(row.get("expoProdNm") or "")
        movie_norm = normalize(movie_name)
        product_norm = normalize(product_name)

        if not any(
            alias
            and (
                alias == movie_norm
                or (product_norm and (alias == product_norm or product_norm.startswith(alias)))
            )
            for alias in aliases
        ):
            continue

        screen_name = str(row.get("scnsNm") or row.get("scnsEnm") or "").strip()
        if not screen_name:
            continue

        if screen_keywords:
            screen_haystack = normalize(
                " ".join(
                    [
                        screen_name,
                        str(row.get("scnsEnm") or ""),
                        product_name,
                    ]
                )
            )
            if not any(keyword in screen_haystack for keyword in screen_keywords):
                continue

        remaining = _safe_int(row.get("frSeatCnt"), 0)
        if min_remaining and remaining < min_remaining:
            continue

        start_raw = str(row.get("scnsrtTm") or "").replace(":", "").strip()
        if not start_raw.isdigit() or len(start_raw) not in (3, 4):
            continue
        start_raw = start_raw.zfill(4)
        hour = _safe_int(start_raw[:2], -1)
        minute = _safe_int(start_raw[2:], -1)
        if hour < 0 or hour > 29 or minute < 0 or minute > 59:
            continue

        display = f"{hour:02d}:{minute:02d}"
        session_identity = (screen_name, display)
        if session_identity in seen_sessions:
            continue
        seen_sessions.add(session_identity)

        sessions.append(
            {
                "MovieNmKor": movie_name or target.get("label", "영화"),
                "PlayStartTm": start_raw,
                "PlayYmd": str(play_ymd),
                "ScreenNm": screen_name,
                "RemainingSeats": remaining,
                "_key": (
                    f"{target['theater_code']}|{play_ymd}|{target['id']}|"
                    f"{screen_name}|{display}"
                ),
            }
        )

    sessions.sort(key=lambda row: (row["PlayStartTm"], row.get("ScreenNm", "")))
    return sessions


def find_screen_name_fallback(lines, time_index):
    keywords = ("imax", "4dx", "screenx", "관", "cinema", "box")
    for i in range(time_index - 1, max(-1, time_index - 9), -1):
        candidate = lines[i].strip()
        lowered = candidate.casefold()
        if (
            any(keyword in lowered for keyword in keywords)
            and not re.search(r"\d{1,2}:\d{2}", candidate)
        ):
            return candidate[:80]
    return "상영관 정보 확인 필요"


def extract_sessions_fallback(body_text: str, target, play_ymd: str):
    """API 장애 때만 사용하는 보조 텍스트 파서.

    놓치는 것보다 확인 가능한 알림을 우선하기 위한 안전망이다.
    결과 행에 _fallback=True를 붙여 Discord에서 검증 필요 표시를 한다.
    """
    lines = [line.strip() for line in body_text.splitlines() if line.strip()]
    aliases = target_aliases(target)
    title_indexes = [
        i
        for i, line in enumerate(lines)
        if any(alias and alias in normalize(line) for alias in aliases)
    ]
    if not title_indexes:
        return []

    sessions = []
    seen_sessions = set()
    for title_index in title_indexes:
        chunk_end = min(len(lines), title_index + 70)
        for i in range(title_index + 1, chunk_end):
            matches = re.findall(
                r"(?<!\d)([0-2]?\d):([0-5]\d)(?!\d)",
                lines[i],
            )
            for hour, minute in matches:
                hour_int = int(hour)
                if hour_int > 29:
                    continue
                display = f"{hour_int:02d}:{minute}"
                screen_name = find_screen_name_fallback(lines, i)
                session_identity = (screen_name, display)
                if session_identity in seen_sessions:
                    continue
                seen_sessions.add(session_identity)
                sessions.append(
                    {
                        "MovieNmKor": target.get("label", "영화"),
                        "PlayStartTm": display.replace(":", ""),
                        "PlayYmd": str(play_ymd),
                        "ScreenNm": screen_name,
                        "_fallback": True,
                        "_key": (
                            f"{target['theater_code']}|{play_ymd}|{target['id']}|"
                            f"{screen_name}|{display}"
                        ),
                    }
                )
        if sessions:
            break

    sessions.sort(
        key=lambda row: (row["PlayStartTm"], row.get("ScreenNm", ""))
    )
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
    """페이지별 장애 알림은 전역 장기 장애 알림으로 통합한다.

    예전 pages 상태가 남아 있으면 한 번만 정리하고, 이후에는 매 5분
    상태 변화마다 state.json을 갱신하지 않는다.
    """
    pages = state.setdefault("health", {}).setdefault("pages", {})
    if pages:
        pages.clear()
        return True
    return False


def notify_failed_pages(state, page_results, now: datetime) -> bool:
    """5분 감시는 유지하되, 30분 지속 장애와 15분 안정 복구만 알린다."""
    health = state.setdefault("health", {})
    incident = health.get("run_warning")
    changed = False

    unhealthy_ids = {
        page_id
        for page_id, result in page_results.items()
        if (not result.get("ok")) or result.get("degraded")
    }

    # 장애가 처음 시작되면 시각/대상만 저장한다. Discord에는 아직 알리지 않는다.
    if not incident and unhealthy_ids:
        health["run_warning"] = {
            "state": "pending",
            "first_failure_at": now.isoformat(),
            "page_ids": sorted(unhealthy_ids),
            "alerted": False,
        }
        print(
            "CGV 조회 불안정 시작: 5분 감시는 계속하며, "
            "30분 이상 지속될 때만 Discord 장애 알림을 보냅니다."
        )
        return True

    if not incident:
        return False

    tracked_ids = set(incident.get("page_ids") or [])
    if not tracked_ids:
        tracked_ids = set(unhealthy_ids)
        if tracked_ids:
            incident["page_ids"] = sorted(tracked_ids)
            changed = True

    # 새로 불안정해진 날짜도 같은 장애 구간에 포함한다.
    new_ids = unhealthy_ids - tracked_ids
    if new_ids:
        tracked_ids.update(new_ids)
        incident["page_ids"] = sorted(tracked_ids)
        changed = True

    # 이번 실행에서 기존 장애 대상이 하나도 조회되지 않았다면 상태를 판단하지 않는다.
    checked_tracked = tracked_ids.intersection(page_results)
    if tracked_ids and not checked_tracked:
        return changed

    relevant_unhealthy = {
        page_id
        for page_id in checked_tracked
        if (
            (not page_results[page_id].get("ok"))
            or page_results[page_id].get("degraded")
        )
    }

    if relevant_unhealthy:
        # 복구 확인 중 다시 흔들리면 복구 타이머만 취소한다.
        if incident.get("recovery_started_at"):
            incident.pop("recovery_started_at", None)
            incident["state"] = "failed" if incident.get("alerted") else "pending"
            changed = True

        try:
            first_failure = datetime.fromisoformat(
                incident.get("first_failure_at", "")
            )
        except (TypeError, ValueError):
            incident["first_failure_at"] = now.isoformat()
            first_failure = now
            changed = True

        # 30분 전에는 계속 조용히 재시도한다.
        if now - first_failure < timedelta(minutes=30):
            return changed

        if incident.get("alerted"):
            return changed

        details = []
        for page_id in sorted(relevant_unhealthy)[:5]:
            result = page_results[page_id]
            mode = (
                "보조 감지만 동작 중"
                if result.get("ok") and result.get("degraded")
                else "구조화/보조 감지 모두 실패"
            )
            details.append(
                f"- {result.get('theater_name', 'CGV')} "
                f"{result.get('play_ymd', '-')}: {mode}"
            )
        extra = (
            f"\n- 외 {len(relevant_unhealthy) - 5}개 날짜"
            if len(relevant_unhealthy) > 5
            else ""
        )
        message = (
            "⚠️ **CGV 확인 장애 지속**\n"
            "정상 구조화 조회가 30분 이상 안정적으로 동작하지 않았습니다. "
            "예매 없음으로 처리하지 않으며 5분 감시는 계속 재시도합니다.\n"
            + "\n".join(details)
            + extra
            + f"\n- 장애 시작: {incident.get('first_failure_at', '-')}"
            + f"\n- 확인 시각: {now.strftime('%Y-%m-%d %H:%M:%S KST')}"
        )
        if send_health_message(message):
            incident["state"] = "failed"
            incident["alerted"] = True
            incident["alerted_at"] = now.isoformat()
            changed = True
        return changed

    # 일부 장애 대상이 이번 실행에 빠졌다면 아직 복구로 확정하지 않는다.
    if tracked_ids and checked_tracked != tracked_ids:
        return changed

    # 30분 장애 알림 전 정상화된 짧은 흔들림은 Discord 알림 없이 종료.
    if not incident.get("alerted"):
        health.pop("run_warning", None)
        print(
            "CGV 일시 조회 불안정이 정상화되었습니다. "
            "30분 미만 장애이므로 Discord 알림 없이 종료합니다."
        )
        return True

    # 실제 장애 알림을 보낸 뒤에는 15분 동안 구조화 조회가 안정적으로
    # 정상이어야 완전 복구 알림을 한 번만 보낸다.
    recovery_started_at = incident.get("recovery_started_at")
    if not recovery_started_at:
        incident["recovery_started_at"] = now.isoformat()
        incident["state"] = "recovering"
        print(
            "CGV 구조화 조회 정상화 확인 시작: "
            "15분 동안 안정적으로 유지되면 완전 복구 알림을 보냅니다."
        )
        return True

    try:
        recovery_started = datetime.fromisoformat(recovery_started_at)
    except (TypeError, ValueError):
        incident["recovery_started_at"] = now.isoformat()
        return True

    if now - recovery_started < timedelta(minutes=15):
        return changed

    message = (
        "✅ **CGV 완전 복구**\n"
        "- 1차 구조화 조회가 15분 이상 안정적으로 정상 동작했습니다.\n"
        "- 보조 감지에 의존하지 않는 정상 감시 상태입니다.\n"
        f"- 복구 확인: {now.strftime('%Y-%m-%d %H:%M:%S KST')}"
    )
    if send_health_message(message):
        health.pop("run_warning", None)
        return True

    return changed


def build_booking_url(target, play_ymd: str) -> str:
    return (
        f"{BOOKING_URL}?siteNo={quote(str(target['theater_code']))}"
        f"&siteNm={quote(page_site_name(target))}&scnYmd={quote(play_ymd)}"
    )


def build_alert_embeds(notification_items):
    checked_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S KST")
    embeds = []

    for item in notification_items:
        target = item["target"]
        movie_name = target.get("label", target["id"])
        theater_name = target.get("theater_name", "CGV")
        new_keys = item["new_keys"]

        for play_ymd in sorted(item["by_date"]):
            date_text = datetime.strptime(play_ymd, "%Y%m%d").strftime("%Y-%m-%d")
            unique = {}
            for row in item["by_date"][play_ymd]:
                unique[row["_key"]] = row

            ordered = sorted(
                unique.values(),
                key=lambda row: (
                    row.get("ScreenNm", ""),
                    row.get("PlayStartTm", ""),
                ),
            )
            new_count = sum(1 for row in ordered if row["_key"] in new_keys)
            fallback_used = any(row.get("_fallback") for row in ordered)

            by_screen = defaultdict(list)
            for row in ordered:
                screen = row.get("ScreenNm") or "상영관 정보 확인 필요"
                mark = "🆕 " if row["_key"] in new_keys else ""
                by_screen[screen].append(
                    f"{mark}`{pretty_time(row.get('PlayStartTm'))}`"
                )

            fields = []
            for screen in sorted(by_screen):
                value = "  ".join(by_screen[screen])
                while value:
                    chunk = value[:1000]
                    value = value[1000:]
                    fields.append({
                        "name": f"🎥 {screen}",
                        "value": chunk,
                        "inline": False,
                    })

            if not fields:
                fields = [{
                    "name": "🎥 상영관 / 시간",
                    "value": "회차 정보 없음",
                    "inline": False,
                }]

            chunks = []
            current = []
            current_chars = 0
            for field in fields:
                size = len(field["name"]) + len(field["value"])
                if current and (len(current) >= 20 or current_chars + size > 4200):
                    chunks.append(current)
                    current = []
                    current_chars = 0
                current.append(field)
                current_chars += size
            if current:
                chunks.append(current)

            for part, chunk in enumerate(chunks, start=1):
                title_suffix = f" · {part}/{len(chunks)}" if len(chunks) > 1 else ""
                embeds.append({
                    "_fallback": fallback_used,
                    "title": (
                        f"⚠️ 보조 감지 후보 · {movie_name}{title_suffix}"
                        if fallback_used
                        else f"🎟️ 예매 오픈 · {movie_name}{title_suffix}"
                    ),
                    "url": build_booking_url(target, play_ymd),
                    "description": (
                        f"🏢 **극장**  {theater_name}\n"
                        f"📅 **날짜**  {date_text}\n"
                        f"✨ **신규 회차**  **{new_count}개**\n"
                        + (
                            "⚠️ **보조 감지 사용** — 구조화 데이터 조회가 실패해 "
                            "CGV 예매 페이지 텍스트로 재확인했습니다. "
                            "오탐 가능성이 있으니 앱/웹에서 한 번 확인하세요.\n"
                            if fallback_used
                            else ""
                        )
                        + "\n아래에서 상영관별 시간을 확인하세요. "
                        "🆕 표시는 이번에 새로 감지된 회차입니다."
                    ),
                    "fields": chunk,
                    "footer": {
                        "text": (
                            f"CGV 보조 감지 후보 · {checked_at} · 제목을 누르면 예매 페이지로 이동"
                            if fallback_used
                            else f"CGV 예매 오픈 감지 · {checked_at} · 제목을 누르면 예매 페이지로 이동"
                        )
                    },
                })

    return embeds


def send_discord_embeds(embeds) -> bool:
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("DISCORD_WEBHOOK_URL Secret이 아직 없어 실제 알림은 보내지 않았습니다.")
        return False

    any_fallback = any(embed.get("_fallback") for embed in embeds)
    any_structured = any(not embed.get("_fallback") for embed in embeds)

    for index, embed in enumerate(embeds):
        payload_embed = dict(embed)
        payload_embed.pop("_fallback", None)
        payload = {"embeds": [payload_embed]}
        if index == 0:
            if any_fallback and any_structured:
                payload["content"] = "🔎 **CGV 감지 결과**"
            elif any_fallback:
                payload["content"] = "⚠️ **CGV 보조 감지 후보**"
            else:
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
    sample_target = {
        "id": "self-test",
        "label": "테스트 영화",
        "theater_code": "0128",
        "movie_aliases": ["테스트 영화"],
        "screen_keywords": [],
        "min_remaining_seats": 1,
    }
    sample_rows = [
        {
            "scnYmd": "20990101",
            "movNm": "테스트 영화",
            "scnsNm": "1관",
            "scnsrtTm": "1230",
            "frSeatCnt": "10",
        },
        {
            "scnYmd": "20990101",
            "movNm": "다른 영화",
            "scnsNm": "2관",
            "scnsrtTm": "1300",
            "frSeatCnt": "10",
        },
    ]
    parsed = extract_sessions(sample_rows, sample_target, "20990101")
    if len(parsed) != 1 or parsed[0]["ScreenNm"] != "1관":
        raise RuntimeError("CGV 구조화 파서 자체점검 실패")

    fallback_text = """
    테스트 영화
    1관
    12:30
    다른 영화
    2관
    13:00
    """
    fallback_parsed = extract_sessions_fallback(
        fallback_text, sample_target, "20990101"
    )
    if not fallback_parsed or not fallback_parsed[0].get("_fallback"):
        raise RuntimeError("CGV 보조 파서 자체점검 실패")

    browser = CgvBrowser(timeout=timeout)
    try:
        today = datetime.now(KST).strftime("%Y%m%d")
        try:
            rows = browser.fetch_schedule("0128", "울산삼산", today)
            if not isinstance(rows, list):
                raise RuntimeError("CGV 상영정보 API 결과가 리스트가 아닙니다")
            print(
                f"CGV 울산삼산 구조화 API 자체점검 완료: "
                f"{today} 응답 {len(rows)}개"
            )
        except RuntimeError as primary_exc:
            print(
                "구조화 API 자체점검 실패. 보조 경로 자체점검으로 전환: "
                f"{primary_exc}"
            )
            text = browser.fetch_text_fallback(
                "0128", "울산삼산", today
            )
            if len(text.strip()) < 120:
                raise RuntimeError(
                    "CGV 구조화 API와 보조 예매 페이지 자체점검 모두 실패"
                )
            print(
                f"CGV 울산삼산 보조 경로 자체점검 완료: "
                f"본문 {len(text)}자"
            )
    finally:
        browser.close()


def run_checker(force_all: bool = False):
    config = load_json(CONFIG_PATH, {"targets": []})
    state = load_json(STATE_PATH, {"version": 2, "seen": {}})
    state.setdefault("version", 2)
    state.setdefault("seen", {})
    state.setdefault("fallback_seen", {})
    state.setdefault("health", {}).setdefault("pages", {})

    migration_changed = migrate_state(state)
    now = datetime.now(KST)
    config_changed, state_changed = prune_expired_targets(
        config, state, now.strftime("%Y%m%d")
    )
    state_changed = state_changed or migration_changed
    if config_changed:
        save_config(config)
    if state_changed:
        save_state(state)

    timeout = int(config.get("request", {}).get("timeout_seconds", 15))
    targets = [target for target in config.get("targets", []) if target.get("enabled", False)]
    if not targets:
        print("활성화된 감시 조건이 없습니다.")
        write_runtime_status(
            build_runtime_snapshot(config, state, targets, now, {})
        )
        return

    target_by_id = {str(t["id"]): t for t in targets}
    scheduled = defaultdict(lambda: defaultdict(set))
    if force_all:
        print(
            "즉시확인 모드: 주기를 무시하고 활성 감시 대상의 전체 날짜를 확인합니다."
        )
    for target in targets:
        for play_ymd in planned_dates(target, now, force_all=force_all):
            scheduled[str(target["theater_code"])][play_ymd].add(str(target["id"]))

    if not scheduled:
        print("이번 5분 실행은 CGV 조회 차례가 아니므로 외부 요청을 건너뜁니다.")
        write_runtime_status(
            build_runtime_snapshot(config, state, targets, now, {})
        )
        return

    browser = CgvBrowser(timeout=timeout)
    page_cache = {}
    page_results = {}
    found = defaultdict(dict)

    try:
        def get_source(target, play_ymd):
            key = (str(target["theater_code"]), play_ymd)
            page_id = f"{target['theater_code']}|{play_ymd}"
            if key not in page_cache:
                print(
                    f"[{target['theater_name']}] {play_ymd} "
                    "구조화 상영정보 확인"
                )
                try:
                    rows = browser.fetch_schedule(
                        str(target["theater_code"]),
                        page_site_name(target),
                        play_ymd,
                    )
                    page_cache[key] = {
                        "mode": "api",
                        "data": rows,
                    }
                    page_results[page_id] = {
                        "ok": True,
                        "degraded": False,
                        "theater_name": target["theater_name"],
                        "play_ymd": play_ymd,
                    }
                except RuntimeError as primary_exc:
                    primary_message = summarize_cgv_error(primary_exc)
                    print(
                        f"경고: [{target['theater_name']}] {play_ymd} "
                        f"구조화 조회 실패. 보조 감지로 재확인: "
                        f"{primary_message}"
                    )
                    try:
                        text = browser.fetch_text_fallback(
                            str(target["theater_code"]),
                            page_site_name(target),
                            play_ymd,
                        )
                        page_cache[key] = {
                            "mode": "fallback",
                            "data": text,
                        }
                        page_results[page_id] = {
                            "ok": True,
                            "degraded": True,
                            "theater_name": target["theater_name"],
                            "play_ymd": play_ymd,
                            "error": (
                                "구조화 조회 실패 후 보조 감지 사용: "
                                f"{primary_message}"
                            ),
                        }
                        print(
                            f"[{target['theater_name']}] {play_ymd} "
                            "보조 감지로 확인 계속"
                        )
                    except RuntimeError as fallback_exc:
                        message = (
                            f"구조화 조회 실패: {primary_message}; "
                            "보조 감지도 실패: "
                            f"{summarize_cgv_error(fallback_exc)}"
                        )
                        print(
                            f"경고: [{target['theater_name']}] {play_ymd} "
                            f"두 확인 경로 모두 실패: {message}"
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
                source = get_source(sample_target, play_ymd)
                if source is None:
                    continue
                for target_id in target_ids:
                    target = target_by_id[target_id]
                    if source["mode"] == "api":
                        sessions = extract_sessions(
                            source["data"], target, play_ymd
                        )
                    else:
                        sessions = extract_sessions_fallback(
                            source["data"], target, play_ymd
                        )
                    if sessions:
                        found[target_id][play_ymd] = sessions

        # 후보가 생기면 그보다 앞선 날짜를 즉시 확인해 감시 범위 내 최초 날짜를 확정한다.
        for target_id, by_date in list(found.items()):
            target = target_by_id[target_id]
            candidate = min(by_date)
            for earlier in [d for d in resolve_target_range(target) if d < candidate]:
                source = get_source(target, earlier)
                if source is None:
                    continue
                if source["mode"] == "api":
                    sessions = extract_sessions(
                        source["data"], target, earlier
                    )
                else:
                    sessions = extract_sessions_fallback(
                        source["data"], target, earlier
                    )
                if sessions:
                    by_date[earlier] = sessions

        failed_pages = [
            result for result in page_results.values()
            if not result.get("ok")
        ]
        if failed_pages:
            first = failed_pages[0]
            print(
                "주의: 이번 실행에서 두 확인 경로가 모두 실패한 날짜가 "
                f"{len(failed_pages)}개 있습니다. 다음 5분 실행에서 다시 시도합니다. "
                f"첫 오류: {first.get('error')}"
            )

        health_changed = notify_failed_pages(
            state, page_results, now
        )
        health_changed = (
            update_page_health(state, page_results, now)
            or health_changed
        )
        if health_changed:
            save_state(state)

        write_runtime_status(
            build_runtime_snapshot(
                config, state, targets, now, page_results, found=found
            )
        )

        notification_items = []
        for target_id, target in target_by_id.items():
            by_date = found.get(target_id, {})
            if not by_date:
                print(f"[{target_id}] 예매 가능한 대상 회차 없음")
                continue

            seen = set(state["seen"].get(target_id, []))
            fallback_seen = set(
                state["fallback_seen"].get(target_id, [])
            )
            all_sessions = [
                row
                for play_ymd in sorted(by_date)
                for row in by_date[play_ymd]
            ]
            new_sessions = [
                row
                for row in all_sessions
                if row["_key"]
                not in (
                    fallback_seen
                    if row.get("_fallback")
                    else seen
                )
            ]
            structured_count = sum(
                1 for row in all_sessions if not row.get("_fallback")
            )
            fallback_count = len(all_sessions) - structured_count
            print(
                f"[{target_id}] 확인 날짜 {len(by_date)}개, "
                f"구조화 {structured_count}개, 보조 후보 {fallback_count}개, "
                f"신규 {len(new_sessions)}개"
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
                fallback_seen = set(
                    state["fallback_seen"].get(target_id, [])
                )
                for sessions in item["by_date"].values():
                    for row in sessions:
                        if row.get("_fallback"):
                            fallback_seen.add(row["_key"])
                        else:
                            seen.add(row["_key"])
                state["seen"][target_id] = sorted(seen)[-1000:]
                state["fallback_seen"][target_id] = sorted(
                    fallback_seen
                )[-1000:]
            save_state(state)
            print(
                "중복 알림 방지 상태를 저장했습니다. "
                "구조화 감지와 보조 후보 기록은 서로 분리됩니다."
            )
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
            force_all = os.getenv("FORCE_ALL_CGV_DATES", "").strip().lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
            run_checker(force_all=force_all)
    except Exception as exc:
        print(f"오류: {exc}", file=sys.stderr)
        write_runtime_status(
            {
                "last_run_at": datetime.now(KST).isoformat(),
                "health_summary": "error",
                "recent_error": str(exc),
                "targets": [],
                "preserve_targets": True,
            }
        )
        raise SystemExit(1)


if __name__ == "__main__":
    main()
