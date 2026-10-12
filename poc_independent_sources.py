"""Read-only public-page transport discovery, respecting site robots rules."""
import json
import re
from urllib.parse import urlparse
import requests

BASE="https://moviebara.com"
s=requests.Session()
robots=s.get(BASE+"/robots.txt",timeout=15)
print("ROBOTS",json.dumps({"status":robots.status_code,"body":robots.text},ensure_ascii=False))
if robots.status_code!=200:
    raise SystemExit("robots unavailable; no API probing")
main=s.get(BASE+"/static/js/main.js",timeout=20)
text=main.text
urls=sorted(set(re.findall(r'https?://[a-zA-Z0-9./:_?-]+',text)))
print("BUNDLE",json.dumps({"status":main.status_code,"bytes":len(text),"url_domains":sorted({urlparse(u).netloc for u in urls})[:35],
    "relevant_urls":[u[:180] for u in urls if any(v in u for v in ["supabase","moviebara","cgv","api"])][:45]},ensure_ascii=False))
for term in ["/api/","supabase","/showings","showings?","/schedules","showtime","remaining_seats","available_seats","seat_count","/cinemas","fetch(Oe","function Oe","graphql","rpc("]:
    m=list(re.finditer(re.escape(term),text,re.IGNORECASE))
    snippets=[re.sub(r"\\s+"," ",text[max(0,x.start()-140):x.end()+200])[:340] for x in m[:4]]
    print("BUNDLE_TERM",json.dumps({"term":term,"count":len(m),"excerpts":snippets},ensure_ascii=False))
