# cgv_alert — CGV 울산삼산 신규 예매 회차 알림

## 현재 운영 상태 (2026-10-14)

**데이터 수집 미복구.** GitHub Actions가 성공해도 예매 오픈 감지가 정상이라는 뜻은 아닙니다.

- 대상: CGV 울산삼산(0128), **어벤져스: 둠스데이 / 듄: 파트 3**, 2026-12-18~31.
- 12/18 우선 감시: 약 **5분마다** 확인 시도.
- 12/19~31: 2026-11-15까지 약 **30분**, 11/16부터 **15분**.
- 신규 **예매 가능 회차만** Discord로 알림. 기존 회차의 좌석 수 변화는 제외.
- 실제 예매·로그인·결제 자동화 없음.
- 현재 1차 제3자 데이터 공급자 `mcp.aka.page`는 반복적 HTTP 502; 독립 게시물 `data.moviebara.com`은 오래된 데이터 또는 목표 날짜 미게시로 5분 감지를 뒷받침하지 못함.
- CGV 공식 시간표/날짜 API는 현재 GitHub 환경에서 403으로 확인되었으며, 명시적 제한을 우회하지 않음.
- **확인된 안정적인 실시간 회차 수집원 없음. 실제 예매 오픈 지연시간 미측정.** 사용자에게 복구 완료라고 보고하지 말 것.

상세 실측·제약·기존 연구: [2026-10-14 복구 인수인계](docs/recovery-2026-10-14.md).

## 실제 동작 방식

```text
Cloudflare Cron (~5분) → Cloudflare Worker → GitHub Actions workflow_dispatch
                                                 │
                                                 ▼
                                            checker.py
                                         ↙           ↘
                               mcp.aka.page       검증된 독립 게시물
                                   │                    │
                                   └───── 판정·비교 ───┘
                                             │
                                신규 회차만 Discord 알림
                                  (seen 중복 방지 유지)
```

- Production 워크플로: [`cgv-alert.yml`](.github/workflows/cgv-alert.yml), 수동 실행 및 관련 코드 push도 가능.
- 시간표 수집, 중복 판정: [`checker.py`](checker.py). 상태: `state.json` (자동 저장하며 사용자가 임의 초기화하지 않음).
- 개별 실행의 사실상 수집 결과: artifact `runtime_status.json` → `collection.outcome`, `collection_pages` 및 Cloudflare 상태.
- `collected` = 적합한 회차 데이터 확보, `unconfirmed` = 빈 미래 응답 등 미확정, `failed` = 조회 실패, `not_due` = 이번 실행 확인 날짜 없음.
- **GitHub Actions success는 수집 success가 아님.** 알려진 공급자 502·보조 게시물 만료는 결과를 `failed`로 그대로 저장하면서 이메일 폭주를 방지하기 위해 워크플로 자체는 성공 종료할 수 있음. 예상치 못한 코드 오류는 실패로 유지.
- [`collection_outcome.py`](collection_outcome.py)에서 실제 수집 결과와 Actions 이메일 정책을 분리.

## 복구 판단과 작업 규칙

1. CGV 울산삼산에서 **현재 실제 예매 가능한 날짜**의 영화·극장·회차·좌석 수를 유효한 데이터로 **반복 자동 수집**해야 함. 12월 목표 상영일이 아직 미게시일 수 있으므로 빈 결과만으로 수집 성공을 주장하면 안 됨.
2. 신규 오픈 감지·회차 고유키·좌석 변화 무시·중복 방지·Discord 전송을 통합 검증해야 함. 오픈→알림 지연은 실제 오픈 관측 전에는 측정 불가.
3. 허용된 수집 경로만 사용. 403/429 접근 제한을 우회하거나 타 업체의 오래된 데이터만으로 실시간 성공이라고 표시하지 말 것.
4. 새로운 공급원을 검증하지 않은 상태에서 기존 운영을 전면 교체하지 말 것. 전환 시 `config.json`, `state.json`, D1, Secrets, Discord Webhook을 보존해야 함.
5. 실제 장애 중에도 Discord 신규 회차 알림용 경로를 유지하고, 알려진 장애의 반복 GitHub 실패 이메일만 억제. 장애 상태를 정상 상태로 바꾸지 말 것.

## 보안

공개 저장소입니다. Discord Webhook URL, 토큰, API Key, 로그인 쿠키, 비밀번호, PAT 등을 코드·설정·로그·문서·커밋에 넣지 말고 GitHub/Cloudflare Secret에만 보관하세요.
