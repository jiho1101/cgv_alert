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
        "public_primary", "cgv_official", "independent_static", "verified_feed",
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
            "source_age_seconds": result.get("source_age_seconds", result.get("independent_source_age_seconds")),
            "source_id": result.get("source_id"),
            "source_origin": result.get("source_origin"),
            "source_observed_at": result.get("source_observed_at"),
            "error": result.get("error"),
        })
    snapshot["verified_feed_configured"] = os.getenv("CGV_VERIFIED_FEED_ENABLED", "") == "1"
    snapshot["collection_evidence_version"] = 1
    snapshot["collection"] = summarize(list(page_results.values()))
    snapshot["collection_pages"] = pages
    snapshot["last_response_success_at"] = (
        now.isoformat() if any(r.get("ok") for r in page_results.values()) else None
    )
    actual = [r for r in page_results.values() if page_outcome(r) == "collected"]
    snapshot["last_monitoring_success_at"] = now.isoformat() if actual else None
    primary = any(r.get("source_mode") in {"public_primary", "verified_feed"} for r in actual)
    snapshot["last_primary_success_at"] = now.isoformat() if primary else None
    snapshot["last_cgv_success_at"] = (
        now.isoformat() if any(r.get("source_mode") == "cgv_official" or r.get("source_origin") == "cgv_first_party" for r in actual) else None
    )
    target_observed = 0
    priority_observed = 0
    checked_target_count = 0
    checked_priority_count = 0
    for target in snapshot.get("targets", []):
        watched_dates = set(target.get("watched_dates") or [])
        priority_dates = set(target.get("priority_dates") or [])
        site = target.get("theater_code")
        matching = [r for key, r in page_results.items()
                    if key.split("|", 1)[0] == site
                    and key.split("|", 1)[1] in watched_dates]
        priority_matching = [r for key, r in page_results.items()
                             if key.split("|", 1)[0] == site
                             and key.split("|", 1)[1] in priority_dates]
        # A nonempty page may contain ONLY unrelated movies. Do not call
        # that evidence of a watched movie, or of its priority date opening.
        # These are positive observations, not claims of complete coverage.
        target_matches = int(target.get("available_session_count") or 0)
        priority_matches = int(target.get("priority_available_session_count") or 0)
        observed = target_matches > 0 and any(
            page_outcome(r) == "collected" for r in matching)
        priority_seen = priority_matches > 0 and any(
            page_outcome(r) == "collected" for r in priority_matching)
        target["target_session_observed_now"] = observed
        target["priority_target_session_observed_now"] = priority_seen
        target["target_session_evidence"] = (
            "observed" if observed else "unproven" if matching else "not_due")
        target["priority_target_session_evidence"] = (
            "observed" if priority_seen else
            "unproven" if priority_matching else "not_due")
        target["target_session_observed_at"] = now.isoformat() if observed else None
        target["priority_target_session_observed_at"] = (
            now.isoformat() if priority_seen else None)
        target["last_page_collection_success_at"] = (
            now.isoformat() if any(page_outcome(r) == "collected" for r in matching)
            else None)
        target_observed += int(observed)
        priority_observed += int(priority_seen)
        checked_target_count += int(bool(matching))
        checked_priority_count += int(bool(priority_matching))
        target["collection"] = summarize(matching)
        target["last_success_at"] = (
            now.isoformat() if any(page_outcome(r) == "collected" for r in matching) else None
        )
        primary_now = any(page_outcome(r) == "collected" and
                          r.get("source_mode") in {"public_primary", "verified_feed"} for r in matching)
        target["last_primary_success_at"] = now.isoformat() if primary_now else None
        target["last_structured_success_at"] = target["last_primary_success_at"]
        outcome = target["collection"]["outcome"]
        if outcome == "failed":
            target["health_status"] = "error"
        elif outcome in {"partial", "unconfirmed"}:
            target["health_status"] = "warning"
        if outcome == "unconfirmed" and not target.get("last_error"):
            target["last_error"] = "회차 데이터 미확정: 빈 응답·미게시·텍스트 후보는 수집 성공이 아닙니다."
    # Separate collected timetable pages from positive observations of the
    # actual watched films. In particular, a 200 for an unrelated 12/29
    # session must not be reported as evidence for the 12/18 watch.
    count = len(snapshot.get("targets", []))
    def evidence_summary(observed_count, checked_count):
        return {
            "outcome": (
                "not_due" if not checked_count else
                "observed" if count and observed_count == count else
                "partial" if observed_count else "unproven"
            ),
            "target_count": count,
            "checked_targets": checked_count,
            "observed_targets": observed_count,
        }
    snapshot["active_target_evidence_version"] = 1
    snapshot["active_target_evidence"] = evidence_summary(
        target_observed, checked_target_count)
    snapshot["priority_target_evidence"] = evidence_summary(
        priority_observed, checked_priority_count)
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
            and "독립 조회 미확정: " in reason
            and any(mark in reason for mark in (
                "SnapshotError: manifest stale",
                "해당 날짜 독립 데이터 미게시 (예매 여부 미확정)",
            ))
        )
        if not (primary_down or guard_down):
            return False
    return True


def ci_exit_code(snapshot):
    """Validate telemetry, not the health of a third-party CGV data source.

    GitHub should email for a broken program/evidence pipeline, not for each
    provider outage or unpublished future date. The real collection verdict
    stays failed in runtime_status.json, artifacts and Cloudflare.
    """
    if snapshot.get("collection_evidence_version") != 1:
        return 1
    collection = snapshot.get("collection")
    pages = snapshot.get("collection_pages")
    if not isinstance(collection, dict) or not isinstance(pages, list):
        return 1
    if any(not isinstance(p, dict) or p.get("outcome") not in
           {"failed", "collected", "unconfirmed"} for p in pages):
        return 1
    for outcome in ("failed", "collected", "unconfirmed"):
        if collection.get(outcome) != sum(p["outcome"] == outcome for p in pages):
            return 1
    if collection.get("checked_pages") != len(pages):
        return 1
    expected = (
        "not_due" if not pages else
        "failed" if collection["failed"] == len(pages) else
        "partial" if collection["failed"] else
        "unconfirmed" if collection["unconfirmed"] else "collected"
    )
    if collection.get("outcome") != expected:
        return 1
    # Uncaught execution errors already fail the checker.py Actions step.
    # A recognized internal parser/programming exception recorded as a page
    # failure also remains a CI failure; new supplier errors alone do not.
    for p in pages:
        if p["outcome"] == "failed":
            reason = str(p.get("error") or "")
            if any(exc in reason for exc in (
                "ValueError: broken parser", "RuntimeError: broken parser",
                "TypeError:", "KeyError:", "AttributeError:", "NameError:",
                "AssertionError:", "SyntaxError:", "ImportError:",
            )):
                return 1
    return 0


if __name__ == "__main__":
    path = Path("runtime_status.json")
    if not path.exists():
        raise SystemExit("COLLECTION_OUTCOME FAIL: runtime_status.json missing")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("collection_evidence_version") != 1:
        raise SystemExit("COLLECTION_OUTCOME FAIL: evidence missing")
    print("COLLECTION_OUTCOME", json.dumps(snapshot["collection"], ensure_ascii=False))
    if "--mute-known-upstream" in sys.argv and workflow_exit_code(snapshot):
        kind = "KNOWN_EXTERNAL_OUTAGE" if known_supplier_outage(snapshot) else "SOURCE_COLLECTION_FAILURE"
        message = (
            f"{kind}: genuine CGV collection failures remain recorded in "
            "runtime_status.json and Cloudflare; upstream availability "
            "does not trigger repeat GitHub Actions failure emails."
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
