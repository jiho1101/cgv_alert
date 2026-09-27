import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from checker import CgvBrowser

KST = ZoneInfo("Asia/Seoul")
RESULT_PATH = Path("persistent_poc_result.json")

SITE_NO = "0128"
SITE_NAME = "울산삼산"
PLAY_YMD = "20260928"
CYCLES = 4
INTERVAL_SECONDS = 60


def row_key(row):
    """구조화 응답의 회차를 안정적으로 비교하기 위한 최소 키."""
    return "|".join(
        str(row.get(name) or "")
        for name in (
            "movNo",
            "siteNo",
            "scnYmd",
            "scnsNo",
            "scnSseq",
            "scnsrtTm",
        )
    )


def main():
    started = datetime.now(KST)
    browser = CgvBrowser(timeout=20)
    samples = []
    previous_keys = None

    try:
        for cycle in range(1, CYCLES + 1):
            cycle_started = datetime.now(KST)
            sample = {
                "cycle": cycle,
                "at": cycle_started.isoformat(),
                "ok": False,
                "status": None,
                "rows": 0,
                "same_as_previous": None,
                "error": None,
            }

            try:
                # 같은 Chrome 프로세스/쿠키/브라우저 세션은 유지하되,
                # 공식 CGV 예매 페이지만 새로 로드해 페이지 자체의
                # searchMovScnInfo 요청을 관찰한다.
                browser.current_site = None
                browser._bootstrap(SITE_NO, SITE_NAME, PLAY_YMD)
                result = browser._consume_browser_schedule(
                    SITE_NO,
                    PLAY_YMD,
                    wait_seconds=12.0,
                )

                if result and result.get("ok"):
                    rows = result.get("rows") or []
                    keys = {row_key(row) for row in rows if isinstance(row, dict)}
                    sample["ok"] = True
                    sample["status"] = 200
                    sample["rows"] = len(rows)
                    if previous_keys is not None:
                        sample["same_as_previous"] = keys == previous_keys
                    previous_keys = keys
                    print(
                        f"[persistent-poc] cycle {cycle}/{CYCLES}: "
                        f"STRUCTURED_OK rows={len(rows)} "
                        f"same_as_previous={sample['same_as_previous']}"
                    )
                elif result:
                    sample["status"] = int(result.get("status") or 0)
                    sample["error"] = str(result.get("error") or "structured response failed")
                    print(
                        f"[persistent-poc] cycle {cycle}/{CYCLES}: "
                        f"FAILED status={sample['status']} error={sample['error']}"
                    )
                else:
                    sample["status"] = 0
                    sample["error"] = "공식 페이지에서 구조화 응답을 관찰하지 못함"
                    print(
                        f"[persistent-poc] cycle {cycle}/{CYCLES}: "
                        "NO_STRUCTURED_RESPONSE"
                    )

            except Exception as exc:
                sample["status"] = 0
                sample["error"] = f"{type(exc).__name__}: {exc}"
                print(
                    f"[persistent-poc] cycle {cycle}/{CYCLES}: "
                    f"EXCEPTION {sample['error']}"
                )

            samples.append(sample)
            if cycle < CYCLES:
                time.sleep(INTERVAL_SECONDS)

    finally:
        browser.close()

    successes = sum(1 for sample in samples if sample["ok"])
    forbidden = sum(1 for sample in samples if sample.get("status") == 403)
    other_failures = len(samples) - successes - forbidden
    result = {
        "test": "persistent_real_chrome_structured_capture",
        "site_no": SITE_NO,
        "site_name": SITE_NAME,
        "play_ymd": PLAY_YMD,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(KST).isoformat(),
        "cycles": len(samples),
        "structured_successes": successes,
        "http_403": forbidden,
        "other_failures": other_failures,
        "success_rate": round(successes / len(samples), 4) if samples else 0,
        "samples": samples,
        "verdict": (
            "persistent_session_candidate"
            if successes == len(samples) and samples
            else "persistent_session_intermittent"
            if successes
            else "persistent_session_failed"
        ),
    }

    RESULT_PATH.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(
        "[persistent-poc] SUMMARY "
        f"structured={successes}/{len(samples)} "
        f"403={forbidden} other_failures={other_failures} "
        f"verdict={result['verdict']}"
    )


if __name__ == "__main__":
    main()
