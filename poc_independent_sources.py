"""Live read-only verification of alternate CGV seat + session source."""
from datetime import datetime
from zoneinfo import ZoneInfo
import json
from collections import Counter

from moviebara_fallback import read_cinema_snapshot, SnapshotError
from shadow_public_monitor import inspect_rows

today = datetime.now(ZoneInfo("Asia/Seoul"))
try:
    result = read_cinema_snapshot("0128", today)
    rows = result["rows"]
    dates = Counter(item["play_date"] for item in rows)
    valid_seats = sum(0 < item["remaining_seats"] <= item["total_seats"] for item in rows)
    ins = inspect_rows(rows)
    print("INDEPENDENT_LIVE_PASS",json.dumps({
        "total_rows":len(rows),"dates":dict(dates),"valid_bookable":valid_seats,
        "collisions":len(ins["identity_collisions"]),
        "source_age_seconds":result["source_age_seconds"],
        "snapshot_age_seconds":result["snapshot_age_seconds"],
        "priority_rows":dates.get("20261218",0),
        "example_keys":ins["identity_keys"][:3],
    },ensure_ascii=False))
except (SnapshotError, Exception) as exc:
    print("INDEPENDENT_LIVE_FAIL",type(exc).__name__,str(exc)[:300])
    raise
