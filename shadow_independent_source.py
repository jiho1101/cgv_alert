"""Independent CGV feed shadow. No Discord or state mutation."""
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
import json
import requests
from moviebara_fallback import read_cinema_snapshot
from checker import extract_public_sessions
from shadow_fallback_validation import audit_rows
from shadow_public_monitor import canonical, extract_rows, identity_key, inspect_rows

now = datetime.now(ZoneInfo("Asia/Seoul"))
date = now.strftime("%Y%m%d")
sample = {
    "at": now.isoformat(), "theater_code": "0128", "date": date,
    "priority_date": "20261218", "secondary": {"ok": False},
    "primary": {"ok": False}, "comparison": {},
}
try:
    feed = read_cinema_snapshot("0128", now)
    rows = feed["rows"]
    current = [r for r in rows if r["play_date"] == date]
    future = [r for r in rows if r["play_date"] == "20261218"]
    inspect = inspect_rows(rows)
    parser_audit = audit_rows(rows, now, extract_public_sessions)
    sample["secondary"] = {
        "ok": True,
        "rows_all_dates": len(rows),
        "rows_today": len(current),
        "rows_priority": len(future),
        "identity_collisions": len(inspect["identity_collisions"]),
        "bookable_rows": sum(0 < r["remaining_seats"] <= r["total_seats"] for r in rows),
        "snapshot_age_seconds": feed["snapshot_age_seconds"],
        "source_age_seconds": feed["source_age_seconds"],
        "parser_audit": parser_audit,
    }
    if parser_audit["status"] == "rejected":
        sample["secondary"]["ok"] = False
        sample["secondary"]["error"] = "production session extractor rejected independent feed rows"
    secondary_keys = {identity_key(x) for x in current}
except Exception as exc:
    sample["secondary"]["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    secondary_keys = set()

try:
    response = requests.get(
        "https://mcp.aka.page/api/cgv/timetable",
        params={"playDate": date, "theaterCode": "0128", "limit": 200},
        headers={"Accept": "application/json"},
        timeout=15,
    )
    sample["primary"]["http_status"] = response.status_code
    payload = response.json() if response.status_code == 200 else {}
    raw, shape_valid = extract_rows(payload)
    normalized = [r for r in (canonical(x) for x in raw) if r]
    inspect = inspect_rows(normalized)
    ok = (
        response.status_code == 200 and shape_valid
        and not inspect["identity_collisions"]
        and all(x == 0 for x in inspect["missing_required"].values())
    )
    sample["primary"].update({"ok": ok, "rows_today": len(normalized)})
    if ok and sample["secondary"]["ok"]:
        primary_keys = {identity_key(x) for x in normalized}
        common = primary_keys & secondary_keys
        sample["comparison"] = {
            "shared": len(common),
            "primary_only": len(primary_keys - secondary_keys),
            "secondary_only": len(secondary_keys - primary_keys),
            "primary_covered_pct": round(100 * len(common) / len(primary_keys), 2) if primary_keys else None,
        }
except Exception as exc:
    sample["primary"]["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"

Path("independent_shadow_sample.json").write_text(
    json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print("INDEPENDENT_SHADOW_SAMPLE", json.dumps(sample, ensure_ascii=False))
if not sample["secondary"]["ok"]:
    reason = str(sample["secondary"].get("error") or "")
    if ("manifest stale" in reason or "source stale" in reason
            or "production session extractor rejected" in reason):
        # A documented, ongoing source outage is still collected as evidence,
        # but should not cause an email every scheduled shadow execution.
        print(f"::warning::CGV independent source not qualified: {reason}")
        raise SystemExit(0)
    raise SystemExit("independent source shadow failed")
