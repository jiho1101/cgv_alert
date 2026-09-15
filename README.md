# cgv_alert

GitHub Actions에서 5분마다 실행되는 CGV 예매 오픈/회차 감지 알리미입니다.

## 현재 동작 방식

- `config.json`의 활성화된 조건만 확인합니다.
- CGV 모바일 공개 시간표 API를 조회합니다.
- 영화명, 상영관, 예매 가능 여부, 잔여 좌석 조건으로 회차를 필터링합니다.
- 처음 발견한 회차만 Telegram 또는 Discord로 알립니다.
- 이미 알린 회차는 `state.json`에 기록해 중복 알림을 막습니다.
- 실제 예매/로그인/결제는 자동화하지 않습니다.

## 보안 규칙

이 저장소는 Public입니다. 아래 값은 절대로 코드, `config.json`, README, 이슈, 커밋에 넣지 마세요.

- Telegram Bot Token
- Discord Webhook URL
- 비밀번호
- 로그인 쿠키/세션
- API Key / Personal Access Token

비밀값은 반드시 GitHub Repository의 **Settings → Secrets and variables → Actions → Repository secrets**에 저장합니다.

지원하는 Secret 이름:

- Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Discord: `DISCORD_WEBHOOK_URL`

실제 Secret 값은 ChatGPT 채팅에도 붙여 넣지 않는 것을 권장합니다.

## 감시 조건 설정

`config.json` 예시:

```json
{
  "targets": [
    {
      "id": "imax-example",
      "enabled": true,
      "label": "원하는 영화 IMAX 오픈",
      "theater_code": "0056",
      "theater_name": "CGV 강남",
      "date": "20260920",
      "movie_keywords": ["영화 제목"],
      "screen_keywords": ["IMAX"],
      "require_sale_open": true,
      "min_remaining_seats": 1
    }
  ],
  "request": {
    "timeout_seconds": 15
  }
}
```

날짜는 `YYYYMMDD`, `today`, `tomorrow`, `today+N` 형식을 지원합니다.

## 자동 실행

`.github/workflows/cgv-alert.yml`이 다음 상황에서 실행됩니다.

- 5분마다 예약 실행
- Actions 화면에서 수동 실행
- 핵심 코드/설정이 변경된 push

코드 변경 push에서는 CGV 공개 시간표 API 연결을 한 번 자체점검합니다.

## 파일

- `checker.py`: 시간표 조회, 필터링, 알림, 중복 방지
- `config.json`: 공개해도 되는 감시 조건
- `state.json`: 이미 알린 회차 기록
- `.github/workflows/cgv-alert.yml`: GitHub Actions 설정
- `.gitignore`: 로컬 비밀 파일 및 불필요 파일 차단
