# CGV Alert Cloudflare Trigger + Discord Commands

This Worker is the control layer for the CGV alert system.

Current flow:

```
Cloudflare Cron (5 min)
  -> GitHub workflow_dispatch
  -> GitHub Actions
  -> checker.py
  -> CGV
  -> Discord booking alert

Discord slash commands
  -> Cloudflare Worker
  -> D1 status snapshot / GitHub workflow dispatch
  -> Discord response
```

## Existing required secret

Create a Cloudflare Worker secret named:

`GITHUB_TOKEN`

Use a GitHub fine-grained personal access token restricted to the
`jiho1101/cgv_alert` repository with **Actions: Read and write** permission.

Do not commit the token into this repository.

## Discord command setup

The Worker supports:

- `/상태`: overall service status, last Cloudflare Cron, last GitHub run,
  last successful CGV read, recent error, and active target count.
- `/감시목록`: movie, target ID, theater, date, current interval,
  health status, and last successful check time.
- `/도움말`: show command usage.

Monitoring targets are intentionally managed through GitHub/ChatGPT rather
than Discord write commands.

### Cloudflare bindings/secrets

Create a D1 database and bind it to the Worker with variable name:

`DB`

The Worker creates its small `app_state` table automatically.

Add these Worker secrets/variables:

- `DISCORD_PUBLIC_KEY` — Discord application Public Key
- `DISCORD_APPLICATION_ID` — Discord application ID
- `DISCORD_BOT_TOKEN` — Discord bot token
- `STATUS_API_TOKEN` — a random shared secret used only for GitHub -> Worker status updates

Keep the existing `GITHUB_TOKEN`.

### GitHub secret

Create one repository secret with the exact same value as the Cloudflare
`STATUS_API_TOKEN`:

`STATUS_API_TOKEN`

The GitHub workflow posts `runtime_status.json` to:

`https://cgv-alert-trigger.choi1101jh.workers.dev/api/status`

The runtime status file is ignored by Git and is not committed.

### Discord Interactions URL

Set the Discord application's Interactions Endpoint URL to:

`https://cgv-alert-trigger.choi1101jh.workers.dev/discord/interactions`

Discord verifies this endpoint using the application's Public Key.

The Worker registers the global slash commands automatically after
`DISCORD_APPLICATION_ID`, `DISCORD_BOT_TOKEN`, and the `DB` binding are present.
Registration is versioned so it is not repeated every five minutes.

## Schedule

The Worker uses:

`*/5 * * * *`

Cloudflare invokes the Worker every five minutes.

## Security

Never commit:

- GitHub PATs
- Discord bot tokens
- Discord webhook URLs
- STATUS_API_TOKEN

Store them only in GitHub Secrets or Cloudflare Worker Secrets.

## Completed migration

The old GitHub-native scheduled trigger and temporary five-minute watcher
have already been removed. `workflow_dispatch` remains because Cloudflare
uses it to start the checker.
