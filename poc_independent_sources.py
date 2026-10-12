"""Read-only inspection of independently hosted public timetable snapshot."""
import json
import re
import requests
from urllib.parse import urljoin
from urllib import robotparser

HOST="https://data.moviebara.com/"
s=requests.Session()
robots=s.get(urljoin(HOST,"robots.txt"),timeout=15)
rp=robotparser.RobotFileParser()
rp.parse(robots.text.splitlines())
def safe_get(path):
    url=urljoin(HOST,path)
    if not rp.can_fetch("*",url):
        raise ValueError("robots forbids "+path)
    r=s.get(url,timeout=20,headers={"Accept":"application/json"})
    r.raise_for_status()
    return r.json()
m=safe_get("manifest.json")
art=m.get("artifacts") or {}
print("FEED_MANIFEST",json.dumps({k:m.get(k) for k in ["generated_at","source_updated_at","changed_at","revision","counts","schema_version"]},ensure_ascii=False)[:1500])
print("FEED_KEYS",json.dumps({"total":len(art),"cinema_0128":[k for k in art if "0128" in k][:40],"cinema_prefix":[k for k in art if k.startswith("cinemas/")][:50],"availability":[k for k in art if "availability/" in k][:35],"other":[k for k in art if "screening" in k or "schedule" in k][:30]},ensure_ascii=False)[:10000])
def inspect(path):
    item=art.get(path)
    if not item:
        print("NO_ARTIFACT",path)
        return
    data=safe_get(item["key"])
    sample=data
    if isinstance(data,dict):
        for key,val in data.items():
            if isinstance(val,list) and val:
                sample={key:val[:2]}
                break
    print("FEED_ARTIFACT",json.dumps({"path":path,"bytes":item.get("bytes"),"keys":list(data)[:50] if isinstance(data,dict) else None,"sample":str(sample)[:2200]},ensure_ascii=False)[:4000])
for path in ["availability/all.json","cinemas/index.json","cinemas/profiles.json","cinemas/0128.json"]:
    inspect(path)
for path in [k for k in art if k.startswith("cinemas/") and "0128" in k][:3]:
    if path not in ["cinemas/0128.json"]:inspect(path)
