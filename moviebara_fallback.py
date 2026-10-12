"""Independent, read-only CGV timetable fallback from public moviebara snapshots.

This is not a real-time CGV API: its source data may lag. Do not use an absent
future date as proof of no showtimes. Reject stale and malformed snapshots.
No login, proxies, identity spoofing, or CGV access-restriction workarounds.
"""
from datetime import datetime, timezone
import hashlib
import re
from urllib.parse import parse_qs, urlparse

import requests

ROOT = "https://data.moviebara.com/"
MAX_MANIFEST_AGE_SECONDS = 90 * 60
MAX_SOURCE_AGE_SECONDS = 12 * 3600
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


class SnapshotError(RuntimeError):
    pass


def _utc_datetime(raw, label):
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if value.tzinfo is None:
            raise ValueError("missing timezone")
        return value.astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise SnapshotError(f"{label}: timestamp invalid") from None


def _response_json(session, path):
    # Follow only this allowlisted host, never arbitrary URLs from a manifest.
    response = session.get(
        ROOT + path,
        timeout=12,
        headers={"Accept": "application/json"},
        allow_redirects=False,
    )
    if response.status_code != 200:
        raise SnapshotError(f"independent data HTTP {response.status_code}")
    if len(response.content) > 2_000_000:
        raise SnapshotError("independent data too large")
    try:
        return response, response.json()
    except ValueError:
        raise SnapshotError("independent data is not JSON") from None


def _as_int(value):
    if isinstance(value, bool):
        return None
    try:
        if isinstance(value, float) and not value.is_integer():
            return None
        parsed = int(value)
        return parsed if parsed >= 0 else None
    except (TypeError, ValueError, OverflowError):
        return None


def _canonical(row, site_no):
    if not isinstance(row, dict):
        raise SnapshotError("screening not object")
    if str(row.get("provider") or "") != "CGV":
        raise SnapshotError("provider is not CGV")
    if str(row.get("cinema_code") or "") != str(site_no):
        raise SnapshotError("cinema mismatch")
    play_date = str(row.get("play_date") or "").replace("-", "")
    if not re.fullmatch(r"\d{8}", play_date):
        raise SnapshotError("screening date invalid")
    start = str(row.get("start_dt") or "").strip()
    if not re.fullmatch(r"(?:[01]\d|2[0-9]):[0-5]\d", start):
        raise SnapshotError("screening start invalid")
    movie_code = str(row.get("source_movie_code") or "").strip()
    movie_name = str(row.get("movie_title") or "").strip()
    if not movie_code or not movie_name:
        raise SnapshotError("screening movie identity missing")
    total, remaining = (
        _as_int(row.get("total_seat_cnt")),
        _as_int(row.get("remain_seat_cnt")),
    )
    if total is None or total <= 0 or remaining is None or remaining > total:
        raise SnapshotError("screening seat evidence invalid")

    booking = urlparse(str(row.get("url") or ""))
    if booking.scheme != "https" or booking.netloc != "cgv.co.kr" or booking.path != "/cnm/movieBook/movie":
        raise SnapshotError("CGV booking identity URL invalid")
    params = parse_qs(booking.query)
    def param(name):
        values = params.get(name, [])
        return values[0] if len(values) == 1 else ""
    seq = param("scnSseq")
    screen = param("scnsNo")
    if (
        param("siteNo") != str(site_no)
        or param("scnYmd") != play_date
        or param("movNo") != movie_code
        or not re.fullmatch(r"\d+", seq)
        or not re.fullmatch(r"\d+", screen)
    ):
        raise SnapshotError("CGV booking identity fields mismatch")

    # Verified 44/44 common showtimes: source scheduleId is
    # YYYYMMDD + siteNo + scnSseq, NOT the UUID of the moviebara row.
    return {
        "movie_code": movie_code,
        "movie_name": movie_name,
        "theater_code": str(site_no),
        "theater_name": str(row.get("cinema_name") or ""),
        "play_date": play_date,
        "start_time": start,
        "end_time": str(row.get("end_dt") or ""),
        "schedule_id": play_date + str(site_no) + seq,
        "screen": str(row.get("screen_name") or screen),
        "total_seats": total,
        "remaining_seats": remaining,
    }


def read_cinema_snapshot(site_no, now, session=None):
    """Read one independent feed snapshot; caller caches it per theater/run.

    Returns all scheduled dates; a missing future date must not be considered
    proof of no booking. This function never sends notifications itself.
    """
    site_no = str(site_no)
    if not re.fullmatch(r"\d{4}", site_no):
        raise SnapshotError("theater code invalid")
    if now.tzinfo is None:
        raise SnapshotError("now must have timezone")
    session = session or requests.Session()
    _, manifest = _response_json(session, "manifest.json")
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise SnapshotError("manifest schema invalid")
    generated = _utc_datetime(manifest.get("generated_at"), "manifest")
    source_updated = _utc_datetime(
        (manifest.get("source_updated_at") or {}).get("screenings"),
        "screenings",
    )
    age = (now.astimezone(timezone.utc) - generated).total_seconds()
    source_age = (now.astimezone(timezone.utc) - source_updated).total_seconds()
    if age < -120 or age > MAX_MANIFEST_AGE_SECONDS:
        raise SnapshotError(f"manifest stale ({int(age)}s)")
    if source_age < -120 or source_age > MAX_SOURCE_AGE_SECONDS:
        raise SnapshotError(f"screenings source stale ({int(source_age)}s)")

    artifact = (manifest.get("artifacts") or {}).get(f"cinemas/{site_no}.json")
    if not isinstance(artifact, dict):
        raise SnapshotError("cinema artifact not published")
    digest = str(artifact.get("sha256") or "")
    key = str(artifact.get("key") or "")
    if not _DIGEST.fullmatch(digest) or key != f"objects/{digest}.json":
        raise SnapshotError("manifest artifact identity invalid")
    response, snapshot = _response_json(session, key)
    if hashlib.sha256(response.content).hexdigest() != digest:
        raise SnapshotError("snapshot content digest mismatch")
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("screenings"), list):
        raise SnapshotError("cinema snapshot structure invalid")
    cinema = snapshot.get("cinema") or {}
    if str(cinema.get("cinema_code") or "") != site_no or str(cinema.get("provider") or "") != "CGV":
        raise SnapshotError("cinema snapshot identity mismatch")

    rows = [_canonical(row, site_no) for row in snapshot["screenings"]]
    from shadow_public_monitor import inspect_rows
    inspection = inspect_rows(rows)
    if inspection["identity_collisions"] or any(inspection["missing_required"].values()):
        raise SnapshotError("screening identity collision or missing fields")
    return {
        "rows": rows,
        "generated_at": generated.isoformat(),
        "source_updated_at": source_updated.isoformat(),
        "snapshot_age_seconds": round(age),
        "source_age_seconds": round(source_age),
    }
