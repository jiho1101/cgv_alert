import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import checker

KST = ZoneInfo("Asia/Seoul")


def fetch_with_transient_retry(state, site_no, play_ymd, now, attempts=3):
    """Smoke에서만 일시 5xx/네트워크 실패를 짧게 재확인한다.

    200 구조 오류, 4xx, 429는 감추지 않고 즉시 실패시켜 실제 계약 변경이나
    호출 제한을 그대로 드러낸다.
    """
    result = None
    for attempt in range(1, attempts + 1):
        result = checker._fetch_public_page(
            state, site_no, play_ymd, now
        )
        if result.get("accepted"):
            return result

        status = result.get("status")
        transient = status is None or (
            isinstance(status, int) and 500 <= status <= 599
        )
        if not transient or attempt >= attempts:
            return result

        delay = 3 * attempt
        print(
            "PRIMARY_SMOKE transient retry "
            f"{attempt}/{attempts} · {play_ymd} · "
            f"status={status} · {delay}s 대기"
        )
        time.sleep(delay)

    return result or {}


def main():
    now = datetime.now(KST)
    state = {"version": 2}
    today = now.strftime("%Y%m%d")

    live = fetch_with_transient_retry(
        state, "0128", today, now
    )
    if not live.get("accepted"):
        raise RuntimeError(
            "production primary live source rejected: "
            + str(live.get("error") or live.get("state"))
        )

    rows = live.get("rows") or []
    if not rows:
        raise RuntimeError(
            "production primary smoke needs a non-empty current-day timetable"
        )

    sample = rows[0]
    synthetic_target = {
        "id": "production-smoke",
        "label": sample["movie_name"],
        "theater_code": "0128",
        "theater_name": "CGV 울산삼산",
        "movie_aliases": [sample["movie_name"]],
        "screen_keywords": [],
        "min_remaining_seats": 0,
    }
    sessions = checker.extract_public_sessions(
        rows, synthetic_target, today
    )
    if not sessions:
        raise RuntimeError("production public parser returned zero sessions")

    keys = [row["_key"] for row in sessions]
    if len(keys) != len(set(keys)):
        raise RuntimeError("production public parser generated duplicate keys")

    future = fetch_with_transient_retry(
        state, "0128", "20261218", now
    )
    if not future.get("accepted"):
        raise RuntimeError(
            "future-date public primary rejected unexpectedly: "
            + str(future.get("error") or future.get("state"))
        )
    if not (future.get("rows") or []):
        if not future.get("empty_unconfirmed"):
            raise RuntimeError(
                "empty future response was incorrectly treated as confirmed"
            )


    print("PRODUCTION_DRY_RUN START · force_all=True · Discord secrets not provided")
    checker.run_checker(force_all=True)
    print("PRODUCTION_DRY_RUN PASS")

    print(
        "PRIMARY_SMOKE PASS "
        + json.dumps(
            {
                "today": today,
                "today_rows": len(rows),
                "sample_movie": sample["movie_name"],
                "sample_sessions": len(sessions),
                "future_rows": len(future.get("rows") or []),
                "future_state": future.get("state"),
                "future_empty_unconfirmed": bool(
                    future.get("empty_unconfirmed")
                ),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
