"""Cross-source exact showtime identity comparison, read-only."""
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse,parse_qs
import json,requests

site="0128"
day=datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
s=requests.Session()
base="https://data.moviebara.com/"
manifest=s.get(base+"manifest.json",timeout=15).json()
art=manifest["artifacts"]["cinemas/0128.json"]
moviebara=s.get(base+art["key"],timeout=15).json()["screenings"]
def mb_key(row):
    qs=parse_qs(urlparse(str(row.get("url")or"")).query)
    movie=str(row.get("source_movie_code")or"")
    dt=str(row.get("play_date")or"").replace("-","")
    code=str(row.get("cinema_code")or"")
    seq=(qs.get("scnSseq")or[""])[0]
    return movie+"|"+dt+"|"+code+"|"+dt+code+seq+"|"+str(row.get("start_dt")or"").replace(":","")
bar={mb_key(r):r for r in moviebara if str(r.get("play_date")or"").replace("-","")==day}
r=s.get("https://mcp.aka.page/api/cgv/timetable",params={"playDate":day,"theaterCode":site,"limit":200},timeout=15)
data=r.json() if r.status_code==200 else {}
raw=data.get("data")
if isinstance(raw,dict):raw=raw.get("timetable",raw.get("items",[]))
if not isinstance(raw,list):raw=[]
def pk(x):
    return "|".join([str(x.get("movieCode")or""),str(x.get("playDate")or""),str(x.get("theaterCode")or""),str(x.get("scheduleId")or""),str(x.get("startTime")or"").replace(":","")])
primary={pk(x):x for x in raw}
same=set(bar)&set(primary)
example=[{"key":key,"alternate":bar[key].get("screen_name"),"primary_remain":primary[key].get("remainingSeats"),"alt_remain":bar[key].get("remain_seat_cnt")}for key in sorted(same)[:6]]
print("CROSS_SOURCE",json.dumps({"date":day,"primary_http":r.status_code,"primary_count":len(raw),"primary_unique":len(primary),"secondary_total":len(moviebara),"secondary_date_count":len(bar),"secondary_identity_collisions":len(bar)<sum(str(r.get("play_date")).replace("-","")==day for r in moviebara),"overlap":len(same),"primary_only":len(set(primary)-set(bar)),"secondary_only":len(set(bar)-set(primary)),"sample_matched":example,"primary_only_examples":list(sorted(set(primary)-set(bar)))[:5],"secondary_only_examples":list(sorted(set(bar)-set(primary)))[:5]},ensure_ascii=False)[:7000])
