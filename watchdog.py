#!/usr/bin/env python3
import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DEFAULT_STALE_SECONDS = 11 * 60
WORKFLOW_FILE = "cgv-alert.yml"
BRANCH = "main"


def utc_now():
    return datetime.now(timezone.utc)


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def decide_watchdog(runs, now, stale_seconds=DEFAULT_STALE_SECONDS):
    """Cloudflare→GitHub trigger gap만 감지한다.

    queued/in_progress 실행이 이미 있으면 추가 dispatch를 쌓지 않는다.
    최근 실행이 있으면 conclusion과 무관하게 트리거 자체는 살아 있는 것으로 본다.
    """
    rows = [row for row in (runs or []) if isinstance(row, dict)]
    if not rows:
        return {
            "action": "dispatch",
            "reason": "no_runs",
            "age_seconds": None,
            "latest": None,
        }

    def created(row):
        try:
            return parse_time(row.get("created_at"))
        except (TypeError, ValueError):
            return datetime.min.replace(tzinfo=timezone.utc)

    latest = max(rows, key=created)
    try:
        latest_at = parse_time(latest.get("created_at"))
    except (TypeError, ValueError):
        return {
            "action": "dispatch",
            "reason": "invalid_latest_timestamp",
            "age_seconds": None,
            "latest": latest,
        }

    age = max(0, int((now - latest_at).total_seconds()))
    status = str(latest.get("status") or "")

    if status in {"queued", "in_progress", "pending", "waiting"}:
        return {
            "action": "wait",
            "reason": "run_already_pending",
            "age_seconds": age,
            "latest": latest,
        }

    if age <= int(stale_seconds):
        return {
            "action": "healthy",
            "reason": "recent_run",
            "age_seconds": age,
            "latest": latest,
        }

    return {
        "action": "dispatch",
        "reason": "stale_run",
        "age_seconds": age,
        "latest": latest,
    }


def github_json(method, url, token, payload=None):
    body = None
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cgv-alert-watchdog",
    }
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")
            if not raw:
                return response.status, None
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"GitHub API HTTP {exc.code}: {raw[:500]}"
        ) from None
    except URLError as exc:
        raise RuntimeError(
            f"GitHub API connection failed: {type(exc.reason).__name__}"
        ) from None


def fetch_alert_runs(repository, token):
    """Repository-wide run 목록에서 실제 Cloudflare dispatch만 고른다.

    workflow-specific runs endpoint가 드물게 오래된 결과를 반환해
    Watchdog이 정상 감시를 장기 공백으로 오판한 사례가 있었다.
    repository-wide endpoint를 사용하고, 정확한 workflow path +
    workflow_dispatch event만 liveness로 인정한다.
    """
    url = (
        f"https://api.github.com/repos/{repository}/actions/runs"
        f"?branch={BRANCH}&event=workflow_dispatch&per_page=100"
    )
    status, payload = github_json("GET", url, token)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected repository-runs response: HTTP {status}")

    rows = payload.get("workflow_runs") or []
    filtered = [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("path") or "") == f".github/workflows/{WORKFLOW_FILE}"
        and str(row.get("event") or "") == "workflow_dispatch"
    ]

    if rows and not filtered:
        raise RuntimeError(
            "Repository run list returned data but no CGV Alert workflow_dispatch runs"
        )
    return filtered


def fetch_recent_alert_runs(repository, token, now, stale_seconds):
    """Primary 목록이 stale처럼 보일 때 동적 시간창으로 한 번 더 확인한다.

    GitHub Actions 목록이 드물게 오래된 결과를 반환해 정상 5분 감시를
    장기 공백으로 오판한 사례가 있어, URL 자체가 매 실행마다 달라지는
    created 범위 조회를 독립 확인으로 사용한다.
    """
    start = now - timedelta(seconds=int(stale_seconds))
    end = now + timedelta(minutes=1)
    created = (
        f"{start.strftime('%Y-%m-%dT%H:%M:%SZ')}.."
        f"{end.strftime('%Y-%m-%dT%H:%M:%SZ')}"
    )
    url = (
        f"https://api.github.com/repos/{repository}/actions/runs"
        f"?branch={BRANCH}&event=workflow_dispatch"
        f"&created={quote(created, safe='')}&per_page=100"
    )
    status, payload = github_json("GET", url, token)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"Unexpected recent-window response: HTTP {status}")

    rows = payload.get("workflow_runs") or []
    return [
        row
        for row in rows
        if isinstance(row, dict)
        and str(row.get("path") or "") == f".github/workflows/{WORKFLOW_FILE}"
        and str(row.get("event") or "") == "workflow_dispatch"
    ]


def dispatch_alert(repository, token, source="github_watchdog"):
    url = (
        f"https://api.github.com/repos/{repository}/actions/workflows/"
        f"{WORKFLOW_FILE}/dispatches"
    )
    status, _ = github_json(
        "POST",
        url,
        token,
        {"ref": BRANCH, "inputs": {"source": source}},
    )
    if status != 204:
        raise RuntimeError(f"Workflow dispatch failed: HTTP {status}")


def send_discord(message):
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook:
        print("Watchdog Discord webhook 미설정 · 백업 실행 자체는 계속 진행")
        return False

    body = json.dumps({"content": message}).encode("utf-8")
    request = Request(
        webhook,
        data=body,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "cgv-alert-watchdog",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=15) as response:
            if response.status not in {200, 204}:
                print(f"Watchdog Discord 경고 전송 실패: HTTP {response.status}")
                return False
    except (HTTPError, URLError) as exc:
        print(f"Watchdog Discord 경고 전송 실패: {type(exc).__name__}")
        return False
    print("Watchdog Discord 경고 전송 완료")
    return True


def run_self_test():
    now = datetime(2099, 1, 1, 12, 0, tzinfo=timezone.utc)

    recent = [{
        "id": 1,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2099-01-01T11:55:00Z",
    }]
    if decide_watchdog(recent, now)["action"] != "healthy":
        raise RuntimeError("Watchdog self-test failed: recent run")

    recent_failed = [{
        "id": 2,
        "status": "completed",
        "conclusion": "failure",
        "created_at": "2099-01-01T11:55:00Z",
    }]
    if decide_watchdog(recent_failed, now)["action"] != "healthy":
        raise RuntimeError("Watchdog self-test failed: trigger liveness vs failure")

    stale = [{
        "id": 3,
        "status": "completed",
        "conclusion": "success",
        "created_at": "2099-01-01T11:40:00Z",
    }]
    if decide_watchdog(stale, now)["action"] != "dispatch":
        raise RuntimeError("Watchdog self-test failed: stale run")

    pending = [{
        "id": 4,
        "status": "in_progress",
        "conclusion": None,
        "created_at": "2099-01-01T11:30:00Z",
    }]
    if decide_watchdog(pending, now)["action"] != "wait":
        raise RuntimeError("Watchdog self-test failed: duplicate prevention")

    if decide_watchdog([], now)["action"] != "dispatch":
        raise RuntimeError("Watchdog self-test failed: no-run recovery")

    push_only = [{
        "id": 5,
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "path": ".github/workflows/cgv-alert.yml",
        "created_at": "2099-01-01T11:59:00Z",
    }]
    if decide_watchdog(push_only, now)["action"] != "healthy":
        raise RuntimeError("Watchdog self-test failed: decision helper regression")

    print(
        "WATCHDOG_SELF_TEST PASS · recent=healthy · stale=dispatch · "
        "pending=no-duplicate · repo-wide dispatch filtering enabled"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force-dispatch", action="store_true")
    parser.add_argument("--no-discord", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        run_self_test()
        return

    repository = os.getenv("GITHUB_REPOSITORY", "").strip()
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not repository or not token:
        raise RuntimeError("GITHUB_REPOSITORY/GITHUB_TOKEN is missing")

    stale_seconds = int(
        os.getenv("WATCHDOG_STALE_SECONDS", str(DEFAULT_STALE_SECONDS))
    )

    if args.force_dispatch:
        decision = {
            "action": "dispatch",
            "reason": "forced_installation_test",
            "age_seconds": None,
            "latest": None,
        }
    else:
        now = utc_now()
        runs = fetch_alert_runs(repository, token)
        decision = decide_watchdog(runs, now, stale_seconds)

        if decision["action"] == "dispatch":
            try:
                recent_runs = fetch_recent_alert_runs(
                    repository,
                    token,
                    now,
                    stale_seconds,
                )
                confirmation = decide_watchdog(
                    recent_runs,
                    now,
                    stale_seconds,
                )
                confirmed_latest = confirmation.get("latest") or {}
                confirmed_age = confirmation.get("age_seconds")
                confirmed_age_text = (
                    "-"
                    if confirmed_age is None
                    else f"{confirmed_age // 60}m {confirmed_age % 60}s"
                )
                print(
                    "WATCHDOG_CONFIRM "
                    f"action={confirmation['action']} "
                    f"reason={confirmation['reason']} "
                    f"age={confirmed_age_text} "
                    f"latest_run={confirmed_latest.get('id', '-')}"
                )
                if confirmation["action"] in {"healthy", "wait"}:
                    decision = dict(confirmation)
                    decision["reason"] = "recent_window_confirmed"
            except Exception as exc:
                print(
                    "WATCHDOG_CONFIRM_WARNING "
                    f"{type(exc).__name__}: {exc}"
                )

    latest = decision.get("latest") or {}
    age = decision.get("age_seconds")
    age_text = "-" if age is None else f"{age // 60}m {age % 60}s"
    print(
        "WATCHDOG_CHECK "
        f"action={decision['action']} reason={decision['reason']} "
        f"age={age_text} latest_run={latest.get('id', '-')}"
    )

    if decision["action"] in {"healthy", "wait"}:
        return

    if args.dry_run:
        print("WATCHDOG_DRY_RUN · dispatch 필요 조건 확인, 실제 dispatch 생략")
        return

    source = (
        "github_watchdog_install_test"
        if args.force_dispatch
        else "github_watchdog"
    )
    dispatch_alert(repository, token, source=source)
    print(f"WATCHDOG_DISPATCH PASS · source={source}")

    if not args.no_discord and not args.force_dispatch:
        last_at = latest.get("created_at") or "기록 없음"
        send_discord(
            "⚠️ **CGV 감시 트리거 공백 감지**\n"
            f"- 마지막 CGV Alert 시작: {last_at}\n"
            f"- 감지 공백: {age_text}\n"
            "- GitHub Watchdog이 백업 감시를 즉시 실행했습니다.\n"
            "- 영화 감지 로직과 중복 알림 방지 상태는 그대로 유지됩니다."
        )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"WATCHDOG_ERROR: {exc}", file=sys.stderr)
        raise
