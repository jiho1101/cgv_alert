# Independent first-party CGV collector prototype

This experiment is an **optional producer**, not a new approved CGV endpoint
or an enabled Production source.

- `cgv_first_party_publisher.py` uses the existing Selenium browser
  implementation to capture a response initiated by the public booking page.
  It never independently replays a rejected API request, retries a 403, uses
  proxies, forges a crawler identity, or logs in.
- It converts exact movie/site/date/screen/sequence/start/seat fields to the
  already deployed `verified_feed.py` schema and runs that validator before
  writing a **local** candidate JSON file.
- Missing December sessions are **unconfirmed**, not proof there are no
  sessions. The source must also return a genuinely bookable current/next-day
  sentinel; empty pages never count as collection success.
- The earliest observation time is used for packet freshness. A sweep lasting
  over four minutes fails rather than asserting that older rows are new.
- The browser's first explicit denial or missing response stops the run. There
  is no Discord send, no state update, no production feed promotion.

## External prerequisites

A ChatGPT user's permission to access external services does **not** grant
authorization from the service operator to perform unattended automated
collection. CGV's published robots policy disallows general crawlers, and
existing GitHub-hosted official API probes returned HTTP 403. **Do not turn on
`CGV_OFFICIAL_AUTOMATION_AUTHORIZED=1` without actual applicable
authorization**. The environment flag is a safeguard, not evidence.

An authorized deployment would additionally need: a host on which the normal
booking-page response is actually available; qualified source identifier and
permission reference; a secured HTTPS publisher to expose the generated
packet; repeated live observations with <=5-minute freshness; and safe
Cloudflare/GitHub feed configuration. No such live host/publisher has been
qualified or enabled here. No CGV account, payment, or inquiry was used.

## Offline verification

```shell
python -m unittest -v test_cgv_first_party_publisher.py
```

For an **already independently authorized** operator, running one read-only
local trial would use:
```shell
CGV_OFFICIAL_AUTOMATION_AUTHORIZED=1 \
CGV_VERIFIED_FEED_SOURCE_ID=<issued-source-id> \
CGV_VERIFIED_FEED_PERMISSION=<actual-permission-reference> \
python cgv_first_party_publisher.py
```
The command writes `cgv_first_party_candidate.json` locally; it does not
create a public endpoint or deliver notifications. Keep candidate packet and
publisher credentials private.

**Production status remains NOT RECOVERED** until an authorized unattended
collector produces repeatable actual first-party data and end-to-end timing
is confirmed. Do not loosen checks or imply that green unit tests prove it.
