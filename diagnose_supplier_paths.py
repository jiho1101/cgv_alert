"""Read-only, bounded CGV supplier diagnostic. Never sends alerts or writes state.

Compares the published theater-date timetable path with the currently watched
future date and tests whether the public API supports a documented theater-only
path. Unknown / invalid responses are failures, never evidence of "not on sale".

No CGV direct requests, bypass headers, proxies, credentials or cache busting.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE = "https://mcp.aka.page"
SITE = "0128"
PROBES = (
    ("near_published_default", "/api/cgv/timetable", {"theaterCode": SITE, "playDate": "20261109", "limit": "200"}),
    ("near_published_theater_only", "/api/cgv/timetable", {"theaterCode": SITE, "playDate": "20261109", "limit": "200", "queryMode": "theater-only"}),
    ("priority_default", "/api/cgv/timetable", {"theaterCode": SITE, "playDate": "20261218", "limit": "200"}),
    ("priority_theater_only", "/api/cgv/timetable", {"theaterCode": SITE, "playDate": "20261218", "limit": "200", "queryMode": "theater-only"}),
    ("priority_movies", "/api/cgv/movies", {"theaterCode": SITE, "playDate": "20261218"}),
)

def summarize(status, payload, error=None):
    result = {"http_status": status, "transport_error": error}
    if not isinstance(payload, dict):
        result["shape"] = "invalid_or_non_json"
        return result
    result["success"] = payload.get("success") if type(payload.get("success")) is bool else None
    diagnostic = payload.get("diagnostics")
    if isinstance(diagnostic, dict):
        result["diagnostic_code"] = str(diagnostic.get("code") or "")[:80] or None
        upstream = diagnostic.get("upstreamStatus")
        result["upstream_status"] = upstream if type(upstream) is int else None
    data = payload.get("data")
    if isinstance(data, dict):
        rows = data.get("timetable")
        if not isinstance(rows, list):
            rows = data.get("movies")
        if isinstance(rows, list):
            result["shape"] = "rows"
            result["row_count"] = len(rows)
        else:
            result["shape"] = "data_object_without_rows"
        result["theater_only_acknowledged"] = data.get("queryMode") == "theater-only"
        result["source_observation"] = (data.get("observation")
            if data.get("observation") in ("published_rows", "empty_unconfirmed")
            else None)
    elif isinstance(data, list):
        result["shape"] = "data_array"
        result["row_count"] = len(data)
    else:
        result["shape"] = "no_valid_data"
    result["interpretation"] = (
        "structured_rows" if status == 200 and result.get("success") is True and result.get("row_count", 0) > 0
        else "empty_unconfirmed" if status == 200 and result.get("success") is True and result.get("row_count") == 0
        else "failed_or_unconfirmed"
    )
    return result

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="supplier_path_matrix.json")
    args = parser.parse_args()
    results = []
    session = requests.Session()
    for name, path, params in PROBES:
        try:
            response = session.get(BASE + path, params=params, headers={"Accept": "application/json"},
                                   timeout=17, allow_redirects=False)
            try:
                payload = response.json()
            except ValueError:
                payload = None
            result = summarize(response.status_code, payload)
        except requests.RequestException as exc:
            result = summarize(None, None, type(exc).__name__)
        results.append({"probe": name, **result})
        print("SOURCE_PATH_DIAGNOSTIC", json.dumps(results[-1], ensure_ascii=False))
    packet = {"time_utc": datetime.now(timezone.utc).isoformat(),
              "theater_code": SITE, "priority_date": "20261218", "probes": results,
              "note": "A future empty response is unconfirmed, not proof of no showtimes. "
                      "An unacknowledged theater-only query is NOT an alternate path.",
              "production_changes": False, "discord_called": False, "d1_modified": False}
    Path(args.output).write_text(json.dumps(packet, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    if any(x.get("http_status") == 200 for x in results):
        print("SOURCE_PATH_DIAGNOSTIC COMPLETED: public API was reachable on at least one probe")
    else:
        print("SOURCE_PATH_DIAGNOSTIC COMPLETED: no confirmed HTTP 200 from public API; diagnostic artifact retained")

if __name__ == "__main__":
    main()
