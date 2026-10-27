# Supplier issue draft — NOT SENT

Observed 2026-10-28 06:58–07:00 KST from the existing requests-based collector. Theater 0128 (Ulsan Samsan).

- GET https://mcp.aka.page/api/cgv/timetable?playDate=20261028&theaterCode=0128&limit=200 → 200, 50 actual sessions.
- Same request for 20261029 → 200, 48 actual sessions. Example: scheduleId 2026102901281, movieCode 30001440, movieName 수능, 출제의 비밀, startTime 10:00, totalSeats 441, remainingSeats 441.
- Same request for 20261115 → 502, CGV_RELAY_FAILED, diagnostics.upstreamStatus=502.
- Same request for 20261218 → 502, CGV_RELAY_FAILED, diagnostics.upstreamStatus=502.

This does not establish that December booking is closed, or that the entire relay is unavailable.

Source review: src/services/cgv/client.ts first queries fetchTimetableBySite. When it returns an empty list with no movieCode supplied, the client calls fetchMoviesByTheaterCode and then per-movie timetable routes. docs/cgv-relay-recovery.md describes the Oct 20 booking route change for theater timetables, while movie-list and movie-specific paths remain separate.

Additional observation at 07:07 KST: /api/cgv/movies returned HTTP 200 for both 20261028 and 20261218 (the same 12 listed movies). This rules out a blanket movie-list endpoint outage; it does not prove the theater timetable succeeded or the listed movies have sessions on the requested date. The relay itself maps non-200 upstream responses and invalid response envelopes to 502, so diagnostics.upstreamStatus=502 is not proof that CGV itself returned HTTP 502.

Please identify which operation fails for the future dates (theater timetable vs movie-list fallback vs movie timetable) and whether the live relay includes the Oct 20 source update. If the theater-level query validly returns an empty list, preserve that observation separately from a failing optional legacy fallback. A valid empty future response should remain explicitly unconfirmed, not proof of no booking. Do not convert unknown upstream errors to a success response.

A minimal reproduction requires only these public REST queries; no customer secrets, login cookies, proxy changes, or request-limit resets are needed. The failing internal operation has not been observed, so the fallback cause is a hypothesis requiring relay logs.

Independent source observation: data.moviebara.com published 198 rows for 0128 covering 20261027–20261101. Manifest generated 2026-10-27T21:49:35.735483Z; source screenings updated 2026-10-27T21:40:58.994744Z. This snapshot contains no December rows and cannot validate the watched dates. Its observed age is not a measured worst-case publication delay.


## 추가 관측: 07:17~07:31 KST

- 같은 0128 / 30001440 영화 코드 지정 요청: 20261028은 HTTP 200과 9회차, 20261218은 HTTP 502. 영화 코드를 지정하는 방법도 복구 경로로 검증되지 않았습니다.
- 20261101은 HTTP 200과 2회차, 20261102와 20261108은 HTTP 502였습니다. 12월에만 한정된 현상은 아닙니다.
- 표준 클라우드 브라우저에서 공식 예매 화면을 열어 울산삼산을 선택하면 당일 영화·시간·총좌석·잔여좌석이 표시됐습니다. 11/8 날짜를 선택하면 현재 상영 스케줄이 없다는 화면이 나왔습니다. 해당 내부 HTTP 응답은 읽지 않았으므로 유효한 빈 원본 응답이라고 단정하지 않습니다. 쿠키 복사, 지문 위장, 접근 제한 우회는 하지 않았습니다. 공식 화면 관측이 자동 수집 허가를 증명하지 않습니다.
- 무비바라 manifest를 일반 HTTP 캐시 재검증으로 읽었을 때 CF-Cache-Status=REVALIDATED, Cache-Control=public,max-age=60이었지만 generated_at은 21:49:35Z, screenings 원본 시각은 21:40:58Z로 같았습니다. 캐시 재검증만으로 갱신 지연을 해결하지 못했습니다.
- 공급자의 공개 Health Checks 40592423625 artifact에서 CGV 검사는 cgv.theaters 한 개였고 시간표·미래 날짜 검사는 없었습니다. 그 실행의 success는 이 문제 해결의 증거가 아닙니다.
- 우리 Production 40595317437의 07:30 Shadow는 가까운 날짜의 주 공급자 50회차, 독립 공급자 공통 49회차, 파서 146건 통과를 기록했지만 12/18 수집은 계속 실패였습니다. 보조 원본은 약 49분 전 데이터였습니다.

극장별 원본 작업만 선택해 후속 폴백 실패와 분리하는 최소 패치를 docs/patches/cgv-theater-only.patch에 준비했습니다. 기본 동작을 유지하는 선택 기능이며 운영 502의 원인이나 복구 성공을 확정하는 패치가 아닙니다. 공급자에 전송하거나 공급자 서버에 배포하지 않았습니다.
