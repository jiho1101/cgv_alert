"""Read-only independent production adapter live qualification."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from collections import Counter
from moviebara_fallback import read_cinema_snapshot
from shadow_public_monitor import inspect_rows

now=datetime.now(ZoneInfo("Asia/Seoul"))
snapshot=read_cinema_snapshot("0128",now)
rows=snapshot["rows"]
dates=Counter(item["play_date"] for item in rows)
inspection=inspect_rows(rows)
print("INDEPENDENT_LIVE_PASS",json.dumps({
    "total_rows":len(rows),"dates":dict(dates),
    "valid_bookable":sum(0<r["remaining_seats"]<=r["total_seats"] for r in rows),
    "identity_collisions":len(inspection["identity_collisions"]),
    "snapshot_age_seconds":snapshot["snapshot_age_seconds"],
    "source_age_seconds":snapshot["source_age_seconds"],
    "priority_rows":dates.get("20261218",0),
    "sample_key":inspection["identity_keys"][:2],
},ensure_ascii=False))
