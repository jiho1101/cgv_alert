"""Read-only independent-source eligibility probe. No login, evasion, or notifications."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import re
import requests

SITE = "0128"
TODAY = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
PRIORITY = "20261218"
UA = {"Accept": "text/html,application/json"}
S = requests.Session()
S.trust_env = True

def inspect(label, method, url, **kw):
    try:
        res = S.request(method, url, timeout=18, headers=UA, allow_redirects=True, **kw)
        html = res.text[:200000]
        matches = re.findall(r"(?:\\d{1,2}:\\d{2}).{0,40}(?:\\d{1,4}\\s*/\\s*\\d{1,4})", html)
        assets = list(dict.fromkeys(re.findall(r'(?:src|href)="([^"]+\\.js[^"]*)"', html)))[:8]
        markers = {
            "timelist": 'timelist' in html,
            "popupSchedule": 'popupSchedule(' in html,
            "next_data": '__NEXT_DATA__' in html,
            "showtime": any(s in html for s in ('showtime', 'scnsrtTm', '상영시간표')),
            "seat": any(s in html for s in ('remainingSeats', 'frSeatCnt', '잔여좌석', '잔여석')),
        }
        print("PROBE", json.dumps({
            "name":label,"status":res.status_code,"final_url":res.url,
            "content_type":res.headers.get("content-type",""),"length":len(html),
            "markers":markers,"seat_patterns":matches[:3], "assets":assets,
            "excerpt":re.sub(r'\\s+',' ',html[:500])[:330]
        }, ensure_ascii=False))
    except requests.RequestException as exc:
        print("PROBE",json.dumps({"name":label,"error":type(exc).__name__ + ": " + str(exc)[:180]},ensure_ascii=False))

print("INDEPENDENT_PROBE_START", TODAY)
inspect("moviebara_today","GET",f"https://moviebara.com/cinema/{SITE}",params={"date":f"{TODAY[:4]}-{TODAY[4:6]}-{TODAY[6:]}"})
inspect("moviebara_priority","GET",f"https://moviebara.com/cinema/{SITE}",params={"date":f"{PRIORITY[:4]}-{PRIORITY[4:6]}-{PRIORITY[6:]}"})
inspect("legacy_mobile_today","POST","https://m.cgv.co.kr/Schedule/cont/ajaxMovieSchedule.aspx",data={"theaterCd":SITE,"playYMD":TODAY})
inspect("legacy_iframe_today","GET","https://www.cgv.co.kr/common/showtimes/iframeTheater.aspx",params={"theatercode":SITE,"date":TODAY})
print("INDEPENDENT_PROBE_END")
