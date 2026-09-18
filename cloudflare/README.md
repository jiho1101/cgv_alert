# CGV Alert Cloudflare Trigger

This Worker is only a timer/trigger. The actual CGV Selenium checker continues to run in GitHub Actions.

## Required secret

Create a Cloudflare Worker secret named:

`GITHUB_TOKEN`

Use a GitHub fine-grained personal access token restricted to the `jiho1101/cgv_alert` repository with **Actions: Read and write** permission.

Do not commit the token into this repository.

## Schedule

The Worker is configured for:

`*/5 * * * *`

That asks Cloudflare Cron to invoke the Worker every five minutes.

## Flow

Cloudflare Cron -> Worker scheduled() -> GitHub workflow_dispatch -> CGV Alert workflow -> checker.py -> Discord
