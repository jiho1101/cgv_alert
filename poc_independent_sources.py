"""Read-only discovery for independent movie schedule APIs. Do not attempt to bypass HTTP 403."""
import hashlib
import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin
import requests

BASE = "https://moviebara.com"
URL = BASE + "/cinema/0128"
SESSION = requests.Session()

class AssetFinder(HTMLParser):
    def __init__(self):
        super().__init__()
        self.scripts = []
        self.links = []
    def handle_starttag(self, tag, attrs):
        at = dict(attrs)
        if tag == "script" and at.get("src"):
            self.scripts.append(at["src"])
        if tag == "link" and at.get("href"):
            self.links.append(at["href"])

for label, url in [
    ("robots", BASE + "/robots.txt"),
    ("page_today", URL + "?date=2026-10-12"),
    ("page_priority", URL + "?date=2026-12-18"),
]:
    try:
        r = SESSION.get(url, timeout=15, headers={"Accept": "text/html"})
        text = r.text
        finder=AssetFinder()
        finder.feed(text[:100000])
        print("DISCOVERY",json.dumps({
            "label":label,"status":r.status_code,"len":len(text),
            "hash":hashlib.sha256(text.encode()).hexdigest()[:16],
            "script_srcs":finder.scripts[:15],
            "css":finder.links[:6],
            "contains_20261218":"20261218" in text or "2026-12-18" in text,
            "seat_terms": {x:(x in text) for x in ["remainingSeats","remaining_seats","잔여석","잔여좌석"]},
            "robots":text[:500] if label=="robots" else None,
        },ensure_ascii=False))
        if label=="page_today":
            assets=[urljoin(BASE,x) for x in finder.scripts if x.endswith(".js") or ".js?" in x]
    except requests.RequestException as e:
        print("DISCOVERY_ERROR",label,type(e).__name__,str(e)[:200])
try:
    print("ASSET_URLS",json.dumps(assets[:8],ensure_ascii=False))
    for url in assets[:2]:
        r=SESSION.get(url,timeout=18)
        body=r.text
        print("ASSET", json.dumps({"status":r.status_code,"url":url,"size":len(body),
            "markers":{k:body.count(k) for k in ["mcp.aka.page","/api/","showtimes","schedule","cinema/","supabase","graphql","cgv","remainingSeats"]},
            "context":{k:[body[max(0,m.start()-80):min(len(body),m.start()+130)] for m in list(re.finditer(re.escape(k),body,flags=re.IGNORECASE))[:3]]
              for k in ["mcp.aka.page","/api/","showtimes","schedule","/cinema/","remainingSeats"]}},
            ensure_ascii=False)[:7000])
except Exception as e:
    print("ASSET_ERROR",type(e).__name__,str(e)[:250])
