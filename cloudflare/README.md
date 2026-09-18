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


## Final cleanup after Cloudflare verification

After Cloudflare Cron has successfully triggered the GitHub workflow several times in a row:

1. Remove the GitHub Actions `schedule: */5 * * * *` trigger from `.github/workflows/cgv-alert.yml`.
2. Keep `workflow_dispatch` because Cloudflare uses it to start the checker.
3. Remove `.github/workflows/cgv-temp-watch.yml`.
4. Keep GitHub Actions enabled because the checker still runs there.
5. Verify there are no duplicate CGV Alert runs after cleanup.

Do not perform this cleanup before Cloudflare has been verified working.
