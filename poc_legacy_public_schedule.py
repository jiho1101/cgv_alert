"""One-off, unauthenticated discovery of publicly accessible CGV schedule endpoints.

Strict stop on HTTP 403/429. No cookies, impersonation, proxy or browser tricks.
Diagnostic only; never touches production state or sends Discord.
"""
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
endpoints = [
    ("legacy_public_mobile_schedule", "POST",
     "https://m.cgv.co.kr/Schedule/cont/ajaxMovieSchedule.aspx",
     {"theaterCd": "0128", "playYMD": today}),
    ("new_public_structured_schedule", "GET",
     "https://cgv.co.kr/api/v1/booking/searchMovScnInfo",
     {"coCd": "A", "siteNo": "0128", "scnYmd": today, "rtctlScopCd": "08"}),
]
report = {"date": today, "theater": "0128", "endpoint_results": []}
with requests.Session() as session:
    for name, method, url, params in endpoints:
        result = {"endpoint": name, "http_status": None,
                  "is_structured_json": False, "timetable_indicators": 0}
        try:
            if method == "POST":
                response = session.post(url, data=params, timeout=12,
                                        allow_redirects=False)
            else:
                response = session.get(url, params=params, timeout=12,
                                       allow_redirects=False)
            result["http_status"] = response.status_code
            result["response_bytes"] = min(len(response.content), 10000000)
            result["content_type"] = response.headers.get("content-type", "")[:100]
            # A blocked endpoint is NOT a fallback: do not retry or manipulate headers.
            if response.status_code == 200:
                body = response.text
                result["timetable_indicators"] = len(
                    re.findall(r"popupSchedule\\(|timelist|scnsrtTm", body, re.I))
                try:
                    obj = response.json()
                    result["is_structured_json"] = isinstance(obj, (dict, list))
                    result["json_top_keys"] = list(obj)[:12] if isinstance(obj, dict) else []
                except ValueError:
                    pass
            else:
                result["restriction_or_error"] = (
                    "access blocked: DO NOT retry through alternative transport"
                    if response.status_code in (401, 403, 429)
                    else "unavailable"
                )
        except requests.RequestException as exc:
            result["network_error_type"] = type(exc).__name__
        report["endpoint_results"].append(result)
print("LEGACY_PUBLIC_ENDPOINT_PROBE", json.dumps(report, ensure_ascii=False))
with open("cgv_legacy_public_probe_result.json", "w", encoding="utf-8") as f:
    json.dump(report, f, ensure_ascii=False, indent=2)
