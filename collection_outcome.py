"""Separate transport success, actual timetable evidence, and watch coverage.

An unpublished future date is not collection evidence. This module makes no
network requests and never changes notification/deduplication state.
"""
import json
import os
import sys
from pathlib import Path


def page_outcome(result):
    if not result.get("ok"):
        return "failed"
    if result.get("empty_unconfirmed") or not isinstance(result.get("row_count"), int) or result["row_count"] <= 0:
        return "unconfirmed"
    if result.get("source_mode") in {"text_fallback", "cloudflare_fallback"}:
        return "unconfirmed"
    if result.get("source_mode") in {
        "public_primary", "cgv_official", "independent_static",
        "primary_independent_combined",
    }:
        return "collected"
    return "unconfirmed"


def summarize(results):
    counts = {"collected": 0, "unconfirmed": 0, "failed": 0}
    for result in results:
        counts[page_outcome(result)] += 1
    total = sum(counts.values())
    if not total:
        outcome = "not_due"
    elif counts["failed"] == total:
        outcome = "failed"
    elif counts["failed"]:
        outcome = "partial"
    elif counts["unconfirmed"]:
        outcome = "unconfirmed"
    else:
        outcome = "collected"
    return {"outcome": outcome, "checked_pages": total, **counts}


def annotate(snapshot, page_results, now):
    """Attach per-date evidence; never turn unknown future dates into success."""
    pages = []
    for key, result in sorted(page_results.items()):
        site, date = key.split("|", 1)
        pages.append({
            "theater_code": site, "play_date": date,
            "outcome": page_outcome(result),
            "source_mode": result.get("source_mode"),
            "response_ok": bool(result.get("ok")),
            "row_count": result.get("row_count"),
            "empty_unconfirmed": bool(result.get("empty_unconfirmed")),
            "source_age_seconds": result.get("independent_source_age_seconds"),
            "error": result.get("error"),
        })
    snapshot["collection_evidence_version"] = 1
    snapshot["collection"] = summarize(list(page_results.values()))
    snapshot["collection_pages"] = pages
    snapshot["last_response_success_at"] = (
        now.isoformat() if any(r.get("ok") for r in page_results.values()) else None
    )
    actual = [r for r in page_results.values() if page_outcome(r) == "collected"]
    snapshot["last_monitoring_success_at"] = now.isoformat() if actual else None
    primary = any(r.get("source_mode") == "public_primary" for r in actual)
    snapshot["last_primary_success_at"] = now.isoformat() if primary else None
    snapshot["last_cgv_success_at"] = (
        now.isoformat() if any(r.get("source_mode") == "cgv_official" for r in actual) else None
    )
    for target in snapshot.get("targets", []):
        matching = [r for key, r in page_results.items()
                    if key.split("|", 1)[0] == target.get("theater_code")
                    and key.split("|", 1)[1] in target.get("watched_dates", [])]
        target["collection"] = summarize(matching)
        target["last_success_at"] = (
            now.isoformat() if any(page_outcome(r) == "collected" for r in matching) else None
        )
        primary_now = any(page_outcome(r) == "collected" and
                          r.get("source_mode") == "public_primary" for r in matching)
        target["last_primary_success_at"] = now.isoformat() if primary_now else None
        target["last_structured_success_at"] = target["last_primary_success_at"]
        outcome = target["collection"]["outcome"]
        if outcome == "failed":
            target["health_status"] = "error"
        elif outcome in {"partial", "unconfirmed"}:
            target["health_status"] = "warning"
        if outcome == "unconfirmed" and not target.get("last_error"):
            target["last_error"] = "회차 데이터 미확정: 빈 응답·미게시·텍스트 후보는 수집 성공이 아닙니다."
    levels = {"normal": 0, "fallback": 1, "warning": 2, "error": 3}
    snapshot["health_summary"] = max(
        (t.get("health_status", "normal") for t in snapshot.get("targets", [])),
        key=lambda v: levels.get(v, 3), default="normal",
    )
    if snapshot["collection"]["outcome"] == "unconfirmed" and not snapshot.get("recent_error"):
        snapshot["recent_error"] = "응답은 수신했지만 감시 날짜의 실제 회차 데이터는 미확정입니다."
    return snapshot


def workflow_exit_code(snapshot):
    # The actual data-collection verdict never depends on notification policy.
    return 1 if snapshot.get("collection", {}).get("failed", 0) else 0


def known_supplier_outage(snapshot):
    """Only silence *known external outages*, never unclassified code failures.

    CI may finish without emailing on a known incident. Runtime/Worker status,
    page evidence and the true collection outcome remain unchanged.
    """
    pages = [p for p in snapshot.get("collection_pages", []) if p.get("outcome") == "failed"]
    if not pages or snapshot.get("collection_evidence_version") != 1:
        return False
    for page in pages:
        reason = str(page.get("error") or "")
        primary_down = (
            "주 공급자 실패: 제3자 API" in reason
            and "독립 공급자:" in reason
            and any(mark in reason for mark in (
                "HTTP 403", "HTTP 429", "HTTP 500", "HTTP 502",
                "HTTP 503", "HTTP 504", "HTTP 520", "HTTP 522",
                "보호 대기",
            ))
        )
        guard_down = (
            reason.startswith("주 API 보호 중 ")
            and "독립 조회 미확정: SnapshotError: manifest stale" in reason
        )
        if not (primary_down or guard_down):
            return False
    return True


def ci_exit_code(snapshot):
    """GitHub email policy differs from truthful collection accounting."""
    if workflow_exit_code(snapshot) == 0:
        return 0
    return 0 if known_supplier_outage(snapshot) else 1


if __name__ == "__main__":
    path = Path("runtime_status.json")
    if not path.exists():
        raise SystemExit("COLLECTION_OUTCOME FAIL: runtime_status.json missing")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("collection_evidence_version") != 1:
        raise SystemExit("COLLECTION_OUTCOME FAIL: evidence missing")
    print("COLLECTION_OUTCOME", json.dumps(snapshot["collection"], ensure_ascii=False))
    if "--mute-known-upstream" in sys.argv and known_supplier_outage(snapshot):
        message = (
            "KNOWN_EXTERNAL_OUTAGE: source remains DOWN; all failed pages and "
            "their real errors are retained in runtime_status.json and Cloudflare. "
            "Expected upstream outage does not generate repeated Actions failure email."
        )
        print(f"::warning::{message}")
        print(message)
        path = os.environ.get("GITHUB_STEP_SUMMARY")
        if path:
            with open(path, "a", encoding="utf-8") as f:
                f.write("### CGV 수집 상태: 장애 지속 (알림 메일만 억제)\n\n")
                f.write("실제 회차 수집 실패. 이것은 복구 완료가 아닙니다.\n\n")
                f.write("상세 정보: runtime_status.json artifact 및 Cloudflare 운영 상태.\n")
    raise SystemExit(ci_exit_code(snapshot) if "--mute-known-upstream" in sys.argv else workflow_exit_code(snapshot))
