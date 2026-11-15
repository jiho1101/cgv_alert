import { chromium } from "playwright";

// Read-only verification of CGV's own publicly rendered booking page.
// This is not a collector. No stealth, cookies, authentication, replay,
// proxy, fingerprint changes, retries, Discord, or state writes.
const theater = "0128";
const cases = ["20261116", "20261218"];
const recorded = [];

function inspectRows(packet, expected) {
  if (!packet || ![0, "0"].includes(packet.statusCode)) return {valid: false, count: 0};
  const data = Array.isArray(packet.data) ? packet.data : null;
  if (!data) return {valid: false, count: 0};
  let count = 0;
  for (const r of data) {
    if (!r || typeof r !== "object") continue;
    if (String(r.siteNo || theater) !== theater ||
        String(r.scnYmd || expected) !== expected ||
        !String(r.movNo || "").trim() ||
        !String(r.scnsrtTm || "").trim() ||
        !String(r.scnSseq || "").trim()) continue;
    const total = Number(r.stcnt), left = Number(r.frSeatCnt);
    if (Number.isInteger(total) && total > 0 && Number.isInteger(left) && left >= 0 && left <= total) count++;
  }
  return {valid: true, count};
}

let browser;
try {
  browser = await chromium.launch({headless: true});
  const context = await browser.newContext({locale: "ko-KR"});
  for (const date of cases) {
    const page = await context.newPage();
    const requests = [], pending = [];
    page.on("response", response => {
      if (!response.url().includes("/api/v1/booking/")) return;
      const action = response.url().split("?")[0].split("/").at(-1);
      if (requests.length >= 25) return;
      const item = {action, status: response.status(), date, qualified: false, validated_rows: 0};
      requests.push(item);
      if (response.status() !== 200) return;
      pending.push((async () => {
        try {
          const scan = inspectRows(await response.json(), date);
          item.qualified = scan.valid && scan.count > 0;
          item.validated_rows = scan.count;
          item.structured = scan.valid;
        } catch { item.parse_failed = true; }
      })());
    });
    let status = null, error = null;
    try {
      const url = "https://cgv.co.kr/cnm/movieBook/cinema" +
        "?siteNo=0128&siteNm=%EC%9A%B8%EC%82%B0%EC%82%BC%EC%82%B0&scnYmd=" + date;
      const response = await page.goto(url, {waitUntil: "domcontentloaded", timeout: 20000});
      status = response?.status() ?? null;
      if (status === 200) await page.waitForTimeout(7000);
    } catch (e) {error = e.name;}
    await Promise.allSettled(pending);
    const result = {date, page_http: status, error, public_api_responses: requests,
      qualified_session_count: requests.reduce((a, r) => a + (r.validated_rows || 0), 0)};
    recorded.push(result);
    console.log("CGV_NATIVE_BROWSER_SOURCE", JSON.stringify(result));
    await page.close();
    if (status !== 200 || !result.qualified_session_count) {
      console.log("CGV_NATIVE_BROWSER_SOURCE stopped: no valid public sessions, no future probe");
      break;
    }
  }
} catch (e) {
  console.log("CGV_NATIVE_BROWSER_SOURCE launch_error", e?.name || "unknown");
} finally {
  if (browser) await browser.close();
}
const {writeFileSync} = await import("node:fs");
writeFileSync("cgv_browser_source_result.json", JSON.stringify({
  time_utc: new Date().toISOString(), recorded, source_promotable: recorded[0]?.qualified_session_count > 0,
  production_changed: false
}, null, 2) + "\n");
