"""Cross-check canonical seat and schedule identities across two different public hosts."""
from collections import Counter
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlparse
import json, requests

s=requests.Session()
m=s.get("https://data.moviebara.com/manifest.json",timeout=20).json()
data=m["artifacts"]["cinemas/0128.json"]
c=s.get("https://data.moviebara.com/"+data["key"],timeout=20).json()
rows=c.get("screenings") or []
dates=Counter(str(x.get("play_date")) for x in rows)
invalid=[r for r in rows if str(r.get("cinema_code"))!="0128" or str(r.get("provider"))!="CGV"]
bookable=[r for r in rows if isinstance(r.get("remain_seat_cnt"),int) and isinstance(r.get("total_seat_cnt"),int) and 0<r["remain_seat_cnt"]<=r["total_seat_cnt"]]
keys=[]
for r in rows:
    qs=parse_qs(urlparse(r.get("url") or "").query)
    key=(r.get("source_movie_code"),str(r.get("play_date")).replace("-",""),(qs.get("scnsNo") or [""])[0],(qs.get("scnSseq") or [""])[0],r.get("start_dt"))
    keys.append(key)
print("CINEMA_0128_SUMMARY",json.dumps({"generated_at":m["generated_at"],"source_updated_at":m.get("source_updated_at",{}).get("screenings"),"artifact_last_changed":m.get("changed_at",{}).get("cinemas/0128.json"),"count":len(rows),"dates":dict(dates),"bookable":len(bookable),"non_CGV":len(invalid),"identity_collisions":len(keys)-len(set(keys)),"top_keys":keys[:4],"example":rows[:2],"cinema":c.get("cinema")},ensure_ascii=False)[:8000])
api=s.get("https://mcp.aka.page/api/cgv/timetable",params={"playDate":datetime.now(timezone.utc).strftime("%Y%m%d"),"theaterCode":"0128","limit":200},timeout=15)
payload=api.json() if api.status_code==200 else {}
raw=payload.get("data")
if isinstance(raw,dict):raw=raw.get("timetable",raw.get("items",[]))
if not isinstance(raw,list):raw=[]
print("PRIMARY_COMPARISON",json.dumps({"status":api.status_code,"count":len(raw),"rows":[{k:r.get(k) for k in ["movieCode","movieName","theaterCode","playDate","startTime","screenName","scheduleId","totalSeats","remainingSeats"]}for r in raw[:3]]},ensure_ascii=False))
