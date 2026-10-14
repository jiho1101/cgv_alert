"""Shadow probe of a real configured publisher; never sends Discord or state."""
from datetime import datetime, timezone
import json
from pathlib import Path
import time
from verified_feed import FeedError, enabled, read_verified_feed


def main():
    evidence = {"live_collection_verified": False, "production_promoted": False, "samples": []}
    try:
        if not enabled():
            raise FeedError("no authorized/qualified publisher configured")
        for index in range(2):
            now = datetime.now(timezone.utc)
            feed = read_verified_feed("0128", now)
            sample = {k: v for k, v in feed.items() if k not in {"complete_dates", "rows"}}
            sample["received_at"] = now.isoformat()
            sample["row_count"] = len(feed["rows"])
            sample["complete_dates"] = sorted(feed["complete_dates"])
            sample["session_samples"] = feed["rows"][:8]
            evidence["samples"].append(sample)
            if index == 0:
                # External workflow sleep, not a blocking tool call. One minute
                # separates observations; no retry after an access restriction.
                time.sleep(60)
        evidence["live_collection_verified"] = (
            evidence["samples"][1]["observed_at"] > evidence["samples"][0]["observed_at"]
            and all(s["row_count"] > 0 for s in evidence["samples"])
        )
        if not evidence["live_collection_verified"]:
            raise FeedError("publisher did not provide a later actual observation")
    except FeedError as exc:
        evidence["error"] = str(exc)
    finally:
        Path("verified_feed_live.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
        print("VERIFIED_FEED_LIVE", json.dumps(evidence, ensure_ascii=False))
    if not evidence["live_collection_verified"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
