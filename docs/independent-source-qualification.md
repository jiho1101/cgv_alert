# Independent CGV source qualification — 2026-10-08

Status: NOT QUALIFIED. Existing 5-minute production monitor remains unchanged.

Candidate 1: CGV first-party public booking API (searchMovScnInfo and searchSchByMov). Structurally independent of the third-party provider, but the existing production probes returned HTTP 403. Never evade access restrictions.

Candidate 2: Open-source CGV API clients. They query the same CGV upstream; an alternate client is not independent data or proof of reliability.

Candidate 3: Other cinema listings. No verified Ulsan Samsan theater 0128, December 18, 2026 screening identities and remaining/total seats.

Candidate 4: The provider's movies and timetable endpoints. Same provider infrastructure; not independent.

Admission requirements: permitted access; live CGV Ulsan Samsan data on a known bookable date; full movie/theater/date/screen/start/session identity; totalSeats > 0 and 1 <= remainingSeats <= totalSeats; 24-hour shadow comparison without false alerts; rate limit compliance; zero future rows remain unconfirmed. No promotion based only on HTTP 200 or green GitHub Actions.

Action: Keep current monitor, seek authorized first-party data access or a genuinely independent licensed timetable feed, and shadow-validate before connecting to production.
