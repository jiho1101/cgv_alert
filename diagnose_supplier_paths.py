"""Read-only, bounded CGV supplier diagnostic. Never sends alerts or writes state.

Compares the published theater-date timetable path with the currently watched
future date and tests whether the public API supports a documented theater-only
path. Unknown / invalid responses are failures, never evidence of "not on sale".

No CGV direct requests, bypass headers, proxies, credentials or cache busting.
"""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import requests

BASE = "https://mcp.aka.page"
SITE = "0128"
PROBES = (
    ("near_movies", "/api/cgv/movies", {"theaterCode": SITE, "playDate": "20261109"}),
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
            # Listings are not booking showtimes. Expose only the watched
            # movie identities for a pre-publication signal, not full payloads.
            result["watched_movie_listings"] = [
                {"movie_code": str(item.get("movieCode") or item.get("movNo") or "")[:50],
                 "movie_name": str(item.get("movieName") or item.get("movNm") or "")[:80]}
                for item in rows
                if isinstance(item, dict)
                and any(word in str(item.get("movieName") or item.get("movNm") or "")
                        for word in ("어벤져스", "둠스데이", "듄"))
            ]
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
            if name.endswith("_movies") and isinstance(payload, dict):
                data = payload.get("data") or {}
                movies = data.get("movies") if isinstance(data, dict) else None
                if isinstance(movies, list):
                    keys = sorted(
                        (str(row.get("movieCode") or row.get("movNo") or ""),
                         str(row.get("movieName") or row.get("movNm") or ""))
                        for row in movies if isinstance(row, dict)
                    )
                    result["listing_identity_sha256"] = hashlib.sha256(
                        json.dumps(keys, ensure_ascii=False).encode("utf-8")).hexdigest()
        except requests.RequestException as exc:
            result = summarize(None, None, type(exc).__name__)
        results.append({"probe": name, **result})
        print("SOURCE_PATH_DIAGNOSTIC", json.dumps(results[-1], ensure_ascii=False))
        if result.get("http_status") == 429:
            print("SOURCE_PATH_DIAGNOSTIC stopped: provider rate limit, no further requests")
            break
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
