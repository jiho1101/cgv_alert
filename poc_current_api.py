import json
from pathlib import Path

import requests

OUT = Path("current_api_probe_result.json")
PARAMS = {
    "coCd": "A420",
    "siteNo": "0128",
    "scnYmd": "20260928",
    "rtctlScopCd": "08",
}

ENDPOINTS = [
    {
        "name": "renewed_api_host",
        "url": "https://api.cgv.co.kr/cnm/atkt/searchMovScnInfo",
    },
    {
        "name": "legacy_same_origin_path",
        "url": "https://cgv.co.kr/api/v1/booking/searchMovScnInfo",
    },
    {
        "name": "renewed_movie_schedule",
        "url": "https://api.cgv.co.kr/cnm/atkt/searchSchByMov",
        "extra_params": {"movNo": "30001314"},
    },
    {
        "name": "renewed_movie_dates",
        "url": "https://api.cgv.co.kr/cnm/atkt/searchSiteScnscYmdListByMov",
        "extra_params": {"movNo": "30001314"},
    },
]


def summarize_response(response):
    content_type = response.headers.get("content-type", "")
    body = response.text
    result = {
        "status": response.status_code,
        "content_type": content_type,
        "body_length": len(body),
        "json": False,
        "status_code_field": None,
        "data_rows": None,
    }
    try:
        payload = response.json()
    except ValueError:
        payload = None

    if isinstance(payload, dict):
        result["json"] = True
        result["status_code_field"] = payload.get("statusCode")
        rows = payload.get("data")
        if isinstance(rows, list):
            result["data_rows"] = len(rows)
    return result


def main():
    results = []
    for item in ENDPOINTS:
        try:
            # 특별 헤더/쿠키/브라우저 위장 없이 일반적인 공개 GET만 검사한다.
            params = dict(PARAMS)
            params.update(item.get("extra_params") or {})
            response = requests.get(
                item["url"],
                params=params,
                headers={"Accept": "application/json"},
                timeout=20,
            )
            summary = summarize_response(response)
            result = {
                "name": item["name"],
                "endpoint": item["url"],
                **summary,
            }
        except requests.RequestException as exc:
            result = {
                "name": item["name"],
                "endpoint": item["url"],
                "error": f"{type(exc).__name__}: {exc}",
            }

        results.append(result)
        print(
            f"[current-api-poc] {result['name']} "
            f"status={result.get('status')} "
            f"json={result.get('json')} "
            f"rows={result.get('data_rows')} "
            f"error={result.get('error')}"
        )

    OUT.write_text(
        json.dumps({"params": PARAMS, "results": results}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
