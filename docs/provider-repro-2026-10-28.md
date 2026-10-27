# Supplier issue draft — NOT SENT

Observed 2026-10-28 06:58–07:00 KST from the existing requests-based collector. Theater 0128 (Ulsan Samsan).

- GET https://mcp.aka.page/api/cgv/timetable?playDate=20261028&theaterCode=0128&limit=200 → 200, 50 actual sessions.
- Same request for 20261029 → 200, 48 actual sessions. Example: scheduleId 2026102901281, movieCode 30001440, movieName 수능, 출제의 비밀, startTime 10:00, totalSeats 441, remainingSeats 441.
- Same request for 20261115 → 502, CGV_RELAY_FAILED, diagnostics.upstreamStatus=502.
- Same request for 20261218 → 502, CGV_RELAY_FAILED, diagnostics.upstreamStatus=502.

This does not establish that December booking is closed, or that the entire relay is unavailable.

Source review: src/services/cgv/client.ts first queries fetchTimetableBySite. When it returns an empty list with no movieCode supplied, the client calls fetchMoviesByTheaterCode and then per-movie timetable routes. docs/cgv-relay-recovery.md describes the Oct 20 booking route change for theater timetables, while movie-list and movie-specific paths remain separate.

Please identify which operation fails for the future dates (theater timetable vs movie-list fallback vs movie timetable) and whether the live relay includes the Oct 20 source update. If the theater-level query validly returns an empty list, preserve that observation separately from a failing optional legacy fallback. A valid empty future response should remain explicitly unconfirmed, not proof of no booking. Do not convert unknown upstream errors to a success response.

A minimal reproduction requires only these public REST queries; no customer secrets, login cookies, proxy changes, or request-limit resets are needed. The failing internal operation has not been observed, so the fallback cause is a hypothesis requiring relay logs.

Independent source observation: data.moviebara.com published 198 rows for 0128 covering 20261027–20261101. Manifest generated 2026-10-27T21:49:35.735483Z; source screenings updated 2026-10-27T21:40:58.994744Z. This snapshot contains no December rows and cannot validate the watched dates. Its observed age is not a measured worst-case publication delay.
