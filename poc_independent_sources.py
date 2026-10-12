"""Inspect declared public static-data paths, without using disallowed /api/ paths."""
import json
import re
import requests

s=requests.Session()
for host in ["https://data.moviebara.com", "https://eta.moviebara.com"]:
    try:
        r=s.get(host+"/robots.txt",timeout=12)
        print("DATA_ROBOTS",json.dumps({"host":host,"status":r.status_code,"body":r.text[:1800]},ensure_ascii=False))
    except requests.RequestException as e:
        print("DATA_ROBOTS_ERROR",host,type(e).__name__)

r=s.get("https://moviebara.com/static/js/main.js",timeout=20)
source=r.text
terms=["REACT_APP_PUBLIC_DATA_BASE_URL", "data.moviebara.com", "REACT_APP_SUPABASE_URL", "getPublic", "showing", ".json", "/v1/", "/v2/", "screenings", "showtimes", "schedule", "cinemaSchedules", "seatAvailable", "availableSeats"]
for token in terms:
    hits=list(re.finditer(re.escape(token),source,re.IGNORECASE))
    snippets=[re.sub(r"\\s+"," ",source[max(0,m.start()-200):min(len(source),m.end()+300)])[:540] for m in hits[:5]]
    print("TOKEN",json.dumps({"term":token,"count":len(hits),"examples":snippets},ensure_ascii=False)[:3700])
