import assert from "node:assert/strict";
import { extractBrowserRunTimetableText } from "./worker.js";

const page = {
  success: true,
  result: "<!doctype html><html><body><section>울산삼산</section>" +
    "<div><span>14:20</span> <span>잔여석 70</span></div>" +
    "<script>window.fake='12:00'</script></body></html>",
};
const text = extractBrowserRunTimetableText(page, "울산삼산");
assert.match(text, /14:20/);
assert.match(text, /잔여석 70/);
assert.doesNotMatch(text, /12:00/);
for (const bad of [
  { success: false, result: page.result },
  { success: true, result: null },
  { success: true, result: "<html>울산삼산</html>" },
  { success: true, result: "<html>울산삼산 14:20</html>" },
  { success: true, result: "<html>울산삼산 잔여석 70</html>" },
  { success: true, result: "<html>울산삼산 14:20 좌석 CAPTCHA</html>" },
  { success: true, result: "<html>다른 극장 14:20 잔여석 70</html>" },
]) {
  assert.throws(() => extractBrowserRunTimetableText(bad, "울산삼산"));
}
assert.throws(() => extractBrowserRunTimetableText('{"success":true,"result":"<html>.."}', "울산삼산"),
  /response/);
console.log("BROWSER_CONTENT_ENVELOPE_PASS: typed JSON result, rendered text, fail-closed shell/challenge/mismatch");
