"""Read-only deployment evidence and one explicitly labeled Discord smoke.

No CGV access workaround; no production state/config changes. HTTP bodies and
authentication material from the Worker are deliberately not dumped.
"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import requests
from moviebara_fallback import read_cinema_snapshot


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--discord-smoke", action="store_true")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    report = {"observed_at": now.isoformat(), "production_recovered": False}
    try:
        feed = read_cinema_snapshot("0128", now)
        report["independent_source"] = {
            "accepted": True, "rows": len(feed["rows"]),
            "generated_at": feed["generated_at"],
            "source_updated_at": feed["source_updated_at"],
            "source_age_seconds": feed["source_age_seconds"],
            "priority_rows": sum(r["play_date"] == "20261218" for r in feed["rows"]),
        }
    except Exception as exc:
        report["independent_source"] = {"accepted": False, "error": str(exc)[:180]}
    token = os.environ.get("STATUS_API_TOKEN", "")
    if token:
        try:
            r = requests.post(
                "https://cgv-alert-trigger.choi1101jh.workers.dev/api/runtime-check",
                headers={"Authorization": "Bearer " + token}, json={}, timeout=18,
            )
            report["worker"] = {"http_status": r.status_code}
            if r.status_code == 200:
                d = r.json()
                report["worker"].update({"version": d.get("version"),
                    "browser_binding": d.get("browser_binding"),
                    "monitoring_stats_24h": d.get("monitoring_stats_24h"),
                    "emergency_last_checked_at": (d.get("emergency_fallback", {}).get("state") or {}).get("last_checked_at"),
                })
        except requests.RequestException as exc:
            report["worker"] = {"error": type(exc).__name__}
    if args.discord_smoke:
        webhook = os.environ.get("DISCORD_WEBHOOK_URL", "")
        if not webhook:
            report["discord"] = {"ok": False, "reason": "secret unavailable"}
        else:
            try:
                r = requests.post(webhook, params={"wait": "true"}, json={
                    "content": "🧪 **CGV 복구 검증 테스트 — 예매 오픈 알림 아님**\n"
                        "Discord 전송 경로만 확인합니다. 실제 수집 복구는 아직 입증되지 않았습니다.",
                    "allowed_mentions": {"parse": []},
                }, timeout=15)
                report["discord"] = {"ok": r.status_code == 200, "http_status": r.status_code}
                if r.status_code == 200:
                    report["discord"]["message_id"] = r.json().get("id")
            except requests.RequestException as exc:
                report["discord"] = {"ok": False, "error": type(exc).__name__}
    Path("recovery_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print("RECOVERY_VALIDATION", json.dumps(report, ensure_ascii=False))
    if args.discord_smoke and not report.get("discord", {}).get("ok"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
