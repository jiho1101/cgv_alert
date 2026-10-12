"""Diagnose content-addressed snapshot hash format; read-only."""
import json,hashlib,requests
s=requests.Session()
base="https://data.moviebara.com/"
m=s.get(base+"manifest.json",timeout=15).json()
a=m["artifacts"]["cinemas/0128.json"]
r=s.get(base+a["key"],timeout=15)
js=r.json()
sha=lambda b:hashlib.sha256(b).hexdigest()
candidates={
 "raw":r.content,
 "raw_strip":r.content.strip(),
 "sort_compact_ascii":json.dumps(js,sort_keys=True,separators=(",",":"),ensure_ascii=True).encode(),
 "sort_compact_utf8":json.dumps(js,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode(),
 "compact_utf8":json.dumps(js,separators=(",",":"),ensure_ascii=False).encode(),
 "sort_pretty_utf8":json.dumps(js,sort_keys=True,ensure_ascii=False,indent=2).encode(),
}
print("HASH_DIAG",json.dumps({"expected":a["sha256"],"actual":{k:sha(v) for k,v in candidates.items()},"bytes":len(r.content),"expected_bytes":a.get("bytes"),"headers":{k:r.headers.get(k) for k in ["content-type","content-encoding","etag","cache-control"]},"prefix_repr":repr(r.content[:130]),"suffix_repr":repr(r.content[-90:])},ensure_ascii=False))
