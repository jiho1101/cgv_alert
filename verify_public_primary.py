import json
from datetime import datetime
from zoneinfo import ZoneInfo

import checker

KST = ZoneInfo("Asia/Seoul")


def main():
    now = datetime.now(KST)
    state = {"version": 2}
    today = now.strftime("%Y%m%d")

    live = checker._fetch_public_page(state, "0128", today, now)
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

    future = checker._fetch_public_page(
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
