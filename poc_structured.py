import json
import re
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from zoneinfo import ZoneInfo

from selenium import webdriver
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.support.ui import WebDriverWait

KST = ZoneInfo("Asia/Seoul")
BOOKING_URL = "https://cgv.co.kr/cnm/movieBook/cinema"
REPORT_PATH = Path("poc_structured_report.json")

SITE_NO = "0128"
SITE_NAME = "울산삼산"
PLAY_YMD = "20260928"

# 영화/상영관/상영시간처럼 '회차'임을 보여주는 구조화 필드 힌트.
TITLE_KEYS = {
    "movienmkor", "moviename", "movie_nm", "movie_nm_kor",
    "moviegroupnm", "movie_group_nm", "movienm",
}
TIME_KEYS = {
    "playstarttm", "play_start_tm", "starttime", "start_time",
    "scnstarttm", "scn_start_tm",
}
SCREEN_KEYS = {
    "screennm", "screen_nm", "screenname", "screen_name",
    "screenno", "screen_no",
}
DATE_KEYS = {
    "playymd", "play_ymd", "scnymd", "scn_ymd", "playdate", "play_date",
}
SEAT_KEYS = {
    "remainseatcnt", "remain_seat_cnt", "remainseat", "remainingseats",
    "seatcnt", "seat_count",
}


def safe_url(raw_url: str) -> str:
    """쿼리 값은 숨기고 endpoint 구조와 파라미터 이름만 남긴다."""
    try:
        parsed = urlparse(raw_url)
        keys = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
        query = urlencode([(key, "*") for key in keys])
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, ""))
    except Exception:
        return str(raw_url).split("?", 1)[0]


def is_cgv_url(raw_url: str) -> bool:
    try:
        host = (urlparse(raw_url).hostname or "").lower()
    except Exception:
        return False
    return host == "cgv.co.kr" or host.endswith(".cgv.co.kr")


def normalize_key(value) -> str:
    return re.sub(r"[^a-z0-9_]", "", str(value or "").casefold())


def collect_keys(value, out: Counter, depth=0):
    if depth > 8:
        return
    if isinstance(value, dict):
        for key, child in value.items():
            out[normalize_key(key)] += 1
            collect_keys(child, out, depth + 1)
    elif isinstance(value, list):
        for child in value[:100]:
            collect_keys(child, out, depth + 1)


def first_list_length(value, depth=0):
    if depth > 8:
        return None
    if isinstance(value, list):
        return len(value)
    if isinstance(value, dict):
        for child in value.values():
            result = first_list_length(child, depth + 1)
            if result is not None:
                return result
    return None


def classify_structured_body(body: str):
    body = str(body or "")
    if not body:
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        # 구형 CGV처럼 JSON 안이 아니라 XML 자체가 내려오는 경우도 후보로 잡는다.
        lowered = body.casefold()
        xml_title = any(tag in lowered for tag in ("<movie_nm", "<movie_group_nm", "<movienm"))
        xml_time = any(tag in lowered for tag in ("<play_start_tm", "<playstarttm", "<scn_start_tm"))
        xml_screen = any(tag in lowered for tag in ("<screen_nm", "<screennm", "<screen_no"))
        if xml_title and xml_time:
            return {
                "format": "xml",
                "score": 3 + int(xml_screen),
                "row_count_hint": None,
                "matched_groups": ["title", "time"] + (["screen"] if xml_screen else []),
            }
        return None

    keys = Counter()
    collect_keys(payload, keys)
    present = set(keys)

    groups = []
    if present & TITLE_KEYS:
        groups.append("title")
    if present & TIME_KEYS:
        groups.append("time")
    if present & SCREEN_KEYS:
        groups.append("screen")
    if present & DATE_KEYS:
        groups.append("date")
    if present & SEAT_KEYS:
        groups.append("seat")

    # 영화명 + 시작시간이 있으면 강한 후보.
    # 시간이 있고 상영관/날짜 중 하나가 함께 있어도 조사 가치가 있는 후보.
    strong = "time" in groups and (
        "title" in groups or "screen" in groups or "date" in groups
    )
    if not strong:
        return None

    return {
        "format": "json",
        "score": len(groups),
        "row_count_hint": first_list_length(payload),
        "matched_groups": groups,
        "sample_keys": sorted(present)[:80],
    }


def make_driver():
    options = Options()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--lang=ko-KR")
    options.page_load_strategy = "eager"
    options.set_capability("goog:loggingPrefs", {"performance": "ALL"})

    driver = webdriver.Chrome(options=options)
    driver.set_page_load_timeout(20)
    driver.set_script_timeout(20)
    driver.execute_cdp_cmd(
        "Network.enable",
        {
            "maxTotalBufferSize": 20_000_000,
            "maxResourceBufferSize": 5_000_000,
        },
    )
    return driver


def collect_network(driver, seconds=12):
    responses = {}
    finished = set()
    failed = set()
    deadline = time.monotonic() + seconds

    while time.monotonic() < deadline:
        try:
            entries = driver.get_log("performance")
        except WebDriverException:
            entries = []

        for entry in entries:
            try:
                outer = json.loads(entry.get("message") or "{}")
                message = outer.get("message") or {}
                method = message.get("method")
                params = message.get("params") or {}
            except Exception:
                continue

            if method == "Network.responseReceived":
                response = params.get("response") or {}
                raw_url = str(response.get("url") or "")
                if not is_cgv_url(raw_url):
                    continue
                request_id = str(params.get("requestId") or "")
                resource_type = str(params.get("type") or "")
                responses[request_id] = {
                    "url": raw_url,
                    "safe_url": safe_url(raw_url),
                    "status": int(float(response.get("status") or 0)),
                    "mime_type": str(response.get("mimeType") or ""),
                    "resource_type": resource_type,
                }
            elif method == "Network.loadingFinished":
                finished.add(str(params.get("requestId") or ""))
            elif method == "Network.loadingFailed":
                failed.add(str(params.get("requestId") or ""))

        time.sleep(0.25)

    # 마지막 로그 묶음까지 비운다.
    try:
        tail = driver.get_log("performance")
    except WebDriverException:
        tail = []
    for entry in tail:
        try:
            outer = json.loads(entry.get("message") or "{}")
            message = outer.get("message") or {}
            method = message.get("method")
            params = message.get("params") or {}
        except Exception:
            continue
        if method == "Network.loadingFinished":
            finished.add(str(params.get("requestId") or ""))
        elif method == "Network.loadingFailed":
            failed.add(str(params.get("requestId") or ""))

    candidates = []
    known_schedule_statuses = []
    endpoint_statuses = Counter()

    for request_id, info in responses.items():
        endpoint_statuses[(info["safe_url"], info["status"], info["resource_type"])] += 1

        if "searchMovScnInfo" in info["url"]:
            known_schedule_statuses.append(info["status"])

        if (
            request_id not in finished
            or request_id in failed
            or not (200 <= info["status"] < 300)
            or info["resource_type"] not in {"XHR", "Fetch"}
        ):
            continue

        try:
            raw = driver.execute_cdp_cmd(
                "Network.getResponseBody",
                {"requestId": request_id},
            )
            body = raw.get("body") or ""
        except WebDriverException:
            continue

        classification = classify_structured_body(body)
        if classification:
            candidates.append(
                {
                    "endpoint": info["safe_url"],
                    "status": info["status"],
                    "mime_type": info["mime_type"],
                    "resource_type": info["resource_type"],
                    **classification,
                }
            )

    compact_endpoints = [
        {
            "endpoint": url,
            "status": status,
            "resource_type": resource_type,
            "count": count,
        }
        for (url, status, resource_type), count in endpoint_statuses.most_common()
        if resource_type in {"XHR", "Fetch"}
    ][:80]

    return {
        "known_searchMovScnInfo_statuses": known_schedule_statuses,
        "structured_candidates": candidates,
        "cgv_xhr_fetch_endpoints": compact_endpoints,
    }


def run_cycle(driver, mode: str, cycle: int):
    url = (
        f"{BOOKING_URL}?siteNo={SITE_NO}"
        f"&siteNm={SITE_NAME}&scnYmd={PLAY_YMD}"
    )

    # 이전 cycle 로그를 버리되 쿠키/브라우저 세션은 persistent 모드에서 유지한다.
    try:
        driver.get_log("performance")
    except WebDriverException:
        pass

    started = time.monotonic()
    error = None
    ready_state = None
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            lambda d: d.execute_script("return document.readyState")
            in {"interactive", "complete"}
        )
        ready_state = driver.execute_script("return document.readyState")
        network = collect_network(driver, seconds=12)
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        network = {
            "known_searchMovScnInfo_statuses": [],
            "structured_candidates": [],
            "cgv_xhr_fetch_endpoints": [],
        }

    elapsed = round(time.monotonic() - started, 2)
    result = {
        "mode": mode,
        "cycle": cycle,
        "elapsed_seconds": elapsed,
        "ready_state": ready_state,
        "error": error,
        **network,
    }

    candidate_text = ", ".join(
        f"{c['endpoint']} score={c['score']} groups={c['matched_groups']}"
        for c in result["structured_candidates"]
    ) or "없음"
    print(
        f"[{mode} #{cycle}] {elapsed}s · "
        f"known_status={result['known_searchMovScnInfo_statuses']} · "
        f"구조화 후보={candidate_text}"
    )
    return result


def summarize(results):
    cycles = len(results)
    candidate_cycles = sum(bool(item["structured_candidates"]) for item in results)
    known_2xx_cycles = sum(
        any(200 <= int(status) < 300 for status in item["known_searchMovScnInfo_statuses"])
        for item in results
    )
    known_403_cycles = sum(
        403 in item["known_searchMovScnInfo_statuses"] for item in results
    )
    return {
        "cycles": cycles,
        "structured_candidate_cycles": candidate_cycles,
        "structured_candidate_rate": (
            round(candidate_cycles / cycles * 100, 1) if cycles else 0.0
        ),
        "known_endpoint_2xx_cycles": known_2xx_cycles,
        "known_endpoint_403_cycles": known_403_cycles,
    }


def main():
    report = {
        "generated_at": datetime.now(KST).isoformat(),
        "target": {
            "site_no": SITE_NO,
            "site_name": SITE_NAME,
            "play_ymd": PLAY_YMD,
        },
        "fresh_session": [],
        "persistent_session": [],
    }

    # A. GitHub Actions의 기존 특성을 흉내 낸 새 Chrome 세션.
    for cycle in range(1, 3):
        driver = None
        try:
            driver = make_driver()
            print(
                f"fresh Chrome {cycle} · "
                f"version={driver.capabilities.get('browserVersion')}"
            )
            report["fresh_session"].append(
                run_cycle(driver, "fresh", cycle)
            )
        finally:
            if driver:
                driver.quit()

    # B. VPS/상시 프로세스 후보와 비슷하게 한 Chrome 세션을 계속 유지.
    driver = None
    try:
        driver = make_driver()
        print(
            "persistent Chrome · "
            f"version={driver.capabilities.get('browserVersion')}"
        )
        for cycle in range(1, 5):
            report["persistent_session"].append(
                run_cycle(driver, "persistent", cycle)
            )
            if cycle < 4:
                time.sleep(3)
    finally:
        if driver:
            driver.quit()

    report["summary"] = {
        "fresh": summarize(report["fresh_session"]),
        "persistent": summarize(report["persistent_session"]),
    }

    # 발견된 endpoint를 mode 전체에서 합쳐 사람이 보기 쉽게 별도 정리.
    discovered = {}
    for item in report["fresh_session"] + report["persistent_session"]:
        for candidate in item["structured_candidates"]:
            key = candidate["endpoint"]
            discovered.setdefault(
                key,
                {
                    "endpoint": key,
                    "max_score": candidate["score"],
                    "matched_groups": candidate["matched_groups"],
                    "seen_cycles": 0,
                },
            )
            discovered[key]["seen_cycles"] += 1
            discovered[key]["max_score"] = max(
                discovered[key]["max_score"], candidate["score"]
            )
    report["discovered_structured_endpoints"] = list(discovered.values())

    REPORT_PATH.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print("\n=== POC SUMMARY ===")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    if report["discovered_structured_endpoints"]:
        print("발견된 구조화 endpoint:")
        for item in report["discovered_structured_endpoints"]:
            print(
                f"- {item['endpoint']} · "
                f"seen={item['seen_cycles']} · "
                f"groups={item['matched_groups']}"
            )
    else:
        print("구조화 시간표 후보 endpoint를 찾지 못했습니다.")


if __name__ == "__main__":
    main()
