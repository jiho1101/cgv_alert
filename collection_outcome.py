"""Separate transport success, actual timetable evidence, and watch coverage.

An unpublished future date is not collection evidence. This module makes no
network requests and never changes notification/deduplication state.
"""
import json
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
    # No scheduled work is not a collection failure. Unpublished future dates
    # are reported as unknown, not artificially declared failures or successes.
    return 1 if snapshot.get("collection", {}).get("failed", 0) else 0


if __name__ == "__main__":
    path = Path("runtime_status.json")
    if not path.exists():
        raise SystemExit("COLLECTION_OUTCOME FAIL: runtime_status.json missing")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    if snapshot.get("collection_evidence_version") != 1:
        raise SystemExit("COLLECTION_OUTCOME FAIL: evidence missing")
    print("COLLECTION_OUTCOME", json.dumps(snapshot["collection"], ensure_ascii=False))
    raise SystemExit(workflow_exit_code(snapshot))
