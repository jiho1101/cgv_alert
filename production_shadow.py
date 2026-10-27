"""Thirty-minute source audit driven by the production five-minute clock.

Reuse production snapshots and page responses. This audit never sends alerts,
changes seen keys, or promotes an unpublished watch date to collection success.
"""
import json
from pathlib import Path

from shadow_fallback_validation import audit_rows
from shadow_public_monitor import canonical, extract_rows, identity_key, inspect_rows


def record_shadow(state, now, independent_cache, public_cache, extract_sessions,
                  fetch_primary, output_path):
    slot = int(now.timestamp()) // 1800
    previous = state.get("production_shadow") or {}
    if previous.get("slot") == slot:
        return False
    today = now.strftime("%Y%m%d")
    sites = {}
    for site, feed in independent_cache.items():
        # An audit requires the validated publication timestamps, never test
        # fixtures or an unconfigured alternative feed. Failed real reads are
        # still recorded without making another supplier request.
        if not feed.get("generated_at") and not feed.get("error"):
            continue
        rows = feed.get("rows") or []
        secondary = {
            "ok": not bool(feed.get("error")),
            "error": feed.get("error"),
            "rows_all_dates": len(rows),
            "published_dates": sorted({r["play_date"] for r in rows}),
            "generated_at": feed.get("generated_at"),
            "source_updated_at": feed.get("source_updated_at"),
            "source_age_seconds": feed.get("source_age_seconds"),
            "parser_audit": audit_rows(rows, now, extract_sessions, site),
        }
        if secondary["parser_audit"]["status"] == "rejected":
            secondary["ok"] = False
            secondary["error"] = "production parser rejected published rows"
        cached = public_cache.get((site, today))
        if cached is None and feed.get("error"):
            cached = {"status": None, "rows": [], "accepted": False,
                      "error": "independent read failed; no additional audit request"}
        if cached is None:
            status, elapsed, payload, error, _ = fetch_primary(site, today)
            raw, shape = extract_rows(payload)
            primary_rows = [r for r in (canonical(x) for x in raw) if r]
            inspection = inspect_rows(primary_rows)
            cached = {
                "status": status, "rows": primary_rows, "error": error,
                "accepted": status == 200 and shape and not error
                and not inspection["identity_collisions"]
                and not any(inspection["missing_required"].values()),
            }
            public_cache[(site, today)] = cached
        primary_rows = cached.get("rows") or []
        primary_keys = {identity_key(r) for r in primary_rows}
        secondary_keys = {identity_key(r) for r in rows if r["play_date"] == today}
        sites[site] = {
            "secondary": secondary,
            "primary": {"http_status": cached.get("status"),
                        "ok": bool(cached.get("accepted") and primary_rows),
                        "rows_today": len(primary_rows), "error": cached.get("error")},
            "comparison": {
                "shared": len(primary_keys & secondary_keys),
                "primary_only": len(primary_keys - secondary_keys),
                "secondary_only": len(secondary_keys - primary_keys),
            } if cached.get("accepted") and secondary["ok"] else None,
        }
    if not sites:
        return False
    sample = {"at": now.isoformat(), "slot": slot, "sites": sites,
              "clock": "production", "watch_coverage_proven": False,
              "production_discord_called": False}
    Path(output_path).write_text(json.dumps(sample, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    state["production_shadow"] = {"slot": slot, "at": now.isoformat()}
    print("PRODUCTION_SHADOW_SAMPLE", json.dumps(sample, ensure_ascii=False))
    return True
