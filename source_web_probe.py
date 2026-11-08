"""One-shot qualification of the public CGV web timetable interface.

Does not change CGV Alert, send Discord, retry blocked requests, or claim
future empty rows mean the booking has not opened.
"""
import json
from datetime import datetime, timezone
from pathlib import Path
from curl_cffi import requests

ROOT = "https://cgv.co.kr"
SITE = "0128"
DATES = ["20261109", "20261218"]
OUT = []

def emit(kind, **kwargs):
    row = {"probe": kind, **kwargs}
    OUT.append(row)
    print("CGV_WEB_SOURCE", json.dumps(row, ensure_ascii=False, sort_keys=True), flush=True)

def call(session, url, **kwargs):
    return session.get(url, timeout=18, allow_redirects=False, **kwargs)

def main():
    try:
        with requests.Session(impersonate="chrome") as session:
            try:
                landing = call(session, ROOT + "/cnm/movieBook")
                emit("landing", http_status=landing.status_code,
                     content_type=landing.headers.get("Content-Type", "")[:70])
                if landing.status_code != 200:
                    return
            except Exception as exc:
                emit("landing", exception=type(exc).__name__)
                return
            for day in DATES:
                try:
                    response = call(
                        session, ROOT + "/api/v1/booking/searchMovScnInfo",
                        params={"coCd": "A420", "siteNo": SITE,
                                "scnYmd": day, "rtctlScopCd": "08"},
                        headers={"Accept": "application/json",
                                 "Referer": ROOT + "/cnm/movieBook"},
                    )
                    if response.status_code != 200:
                        emit("showtime", date=day, http_status=response.status_code,
                             qualified=False)
                        break
                    try:
                        packet = response.json()
                    except ValueError:
                        emit("showtime", date=day, http_status=200,
                             qualified=False, invalid_json=True)
                        break
                    raw = packet.get("data") if isinstance(packet, dict) else None
                    structure = (isinstance(packet, dict)
                                 and packet.get("statusCode") in [0, "0"]
                                 and isinstance(raw, list))
                    valid = []
                    if structure:
                        for r in raw:
                            if not isinstance(r, dict):
                                continue
                            if str(r.get("siteNo") or SITE) != SITE: continue
                            if str(r.get("scnYmd") or day) != day: continue
                            if not str(r.get("movNo") or "").strip(): continue
                            if not str(r.get("scnsrtTm") or "").strip(): continue
                            try:
                                seats = int(r.get("stcnt") or 0)
                                remaining = int(r.get("frSeatCnt") or 0)
                            except (TypeError, ValueError):
                                continue
                            if seats > 0 and 0 <= remaining <= seats:
                                valid.append(r)
                    emit("showtime", date=day, http_status=200, status_code=packet.get("statusCode") if isinstance(packet,dict) else None,
                         structured=structure, raw_rows=len(raw) if isinstance(raw,list) else None,
                         validated_rows=len(valid),
                         qualified=structure and len(valid)>0,
                         interpretation=("observed_showtimes" if valid else
                                         "empty_unconfirmed" if structure and not raw else
                                         "invalid_or_unqualified"))
                    if not structure:
                        break
                except Exception as exc:
                    emit("showtime", date=day, exception=type(exc).__name__, qualified=False)
                    break
    finally:
        Path("cgv_web_supplier_probe.json").write_text(
            json.dumps({"at":datetime.now(timezone.utc).isoformat(),
                        "site":SITE,"results":OUT,"production_untouched":True},
                        ensure_ascii=False, indent=2)+"\n", encoding="utf-8")

if __name__ == "__main__":
    main()
