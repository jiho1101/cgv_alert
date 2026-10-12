"""Static feed read-only discovery; follows independent site's robots policy."""
from urllib import robotparser
import json
import re
import requests

HOST="https://data.moviebara.com"
s=requests.Session()
robots=s.get(HOST+"/robots.txt",timeout=12)
rp=robotparser.RobotFileParser()
rp.parse(robots.text.splitlines())
print("STATIC_ROBOTS",json.dumps({"status":robots.status_code,"text":robots.text[-3200:],"can_read_profile":rp.can_fetch("*",HOST+"/cinemas/profiles.json"),"can_read_cinema":rp.can_fetch("*",HOST+"/cinemas/0128.json")},ensure_ascii=False))
main=s.get("https://moviebara.com/static/js/main.js",timeout=15).text
patterns=["cinemas/","/cinemas/","movies/","catalogue/","loadPage(","loadPages(","artifacts[","manifest.json","public-data","/manifest","index.json","screenings", "seats"]
for token in patterns:
    matches=list(re.finditer(re.escape(token),main))
    print("JS_MARKER",json.dumps({"term":token,"count":len(matches),"contexts":[main[max(0,x.start()-170):x.end()+220][:410] for x in matches[:3]]},ensure_ascii=False)[:3400])
paths=sorted(set(re.findall(r'["\\x27]([a-zA-Z0-9/_-]{1,85}\\.json)["\\x27]',main)))
print("STATIC_DECLARED_JSON",json.dumps(paths[:110],ensure_ascii=False))
for path in ["cinemas/profiles.json","cinemas/0128.json","manifest.json"]:
    url=HOST+"/"+path
    if robots.status_code!=200 or not rp.can_fetch("*",url):
        print("STATIC_SKIP",path,"robots denied")
        continue
    try:
        res=s.get(url,timeout=12,headers={"Accept":"application/json"})
        js=res.json() if res.status_code==200 and "json" in res.headers.get("content-type","") else None
        print("STATIC_PROBE",json.dumps({"path":path,"status":res.status_code,"type":res.headers.get("content-type",""),"len":len(res.content),"keys":list(js)[:20] if isinstance(js,dict) else None,"example":str(js)[:450] if isinstance(js,dict) else None},ensure_ascii=False)[:1500])
    except requests.RequestException as e:
        print("STATIC_ERR",path,type(e).__name__)
