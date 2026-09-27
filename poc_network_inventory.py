import json
import time
from pathlib import Path
from urllib.parse import urlparse

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By

from checker import CgvBrowser

SITE_NO = "0128"
SITE_NAME = "울산삼산"
PLAY_YMD = "20260928"
OUT = Path("network_inventory_result.json")


def main():
    browser = CgvBrowser(timeout=20)
    result = {
        "site_no": SITE_NO,
        "site_name": SITE_NAME,
        "play_ymd": PLAY_YMD,
        "page": {},
        "network": [],
    }

    try:
        browser.current_site = None
        browser._bootstrap(SITE_NO, SITE_NAME, PLAY_YMD)
        time.sleep(10)

        try:
            body = browser.driver.find_element(By.TAG_NAME, "body").text
        except WebDriverException:
            body = ""

        result["page"] = {
            "title": browser.driver.title,
            "current_url": browser.driver.current_url,
            "body_length": len(body),
            "has_site_name": SITE_NAME in body,
            "has_target_movie": "암살자" in body,
            "no_theater_selected": "선택 된 극장이 없습니다" in body,
            "reload_prompt": "다시 불러오기" in body,
            "body_prefix": body[:800],
        }

        try:
            entries = browser.driver.get_log("performance")
        except WebDriverException:
            entries = []

        seen = set()
        rows = []
        for entry in entries:
            try:
                outer = json.loads(entry.get("message") or "{}")
                message = outer.get("message") or {}
            except (TypeError, json.JSONDecodeError):
                continue

            if message.get("method") != "Network.responseReceived":
                continue

            response = (message.get("params") or {}).get("response") or {}
            raw_url = str(response.get("url") or "")
            if not raw_url:
                continue

            try:
                parsed = urlparse(raw_url)
            except ValueError:
                continue

            host = parsed.netloc.lower()
            path = parsed.path
            if "cgv.co.kr" not in host:
                continue
            if not (
                "/api/" in path
                or "/cnm/" in path
                or "booking" in path.lower()
                or "moviebook" in path.lower()
            ):
                continue

            status = int(float(response.get("status") or 0))
            item = (status, host, path)
            if item in seen:
                continue
            seen.add(item)
            rows.append(
                {
                    "status": status,
                    "host": host,
                    "path": path,
                }
            )

        rows.sort(key=lambda x: (x["host"], x["path"], x["status"]))
        result["network"] = rows

        print(
            "[network-inventory] page "
            f"title={result['page']['title']!r} "
            f"body_length={result['page']['body_length']} "
            f"site={result['page']['has_site_name']} "
            f"movie={result['page']['has_target_movie']} "
            f"no_theater_selected={result['page']['no_theater_selected']}"
        )
        for row in rows:
            print(
                "[network-inventory] "
                f"HTTP {row['status']} {row['host']}{row['path']}"
            )

    finally:
        browser.close()

    OUT.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    main()
