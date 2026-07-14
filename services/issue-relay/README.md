# TunnelForge Issue Relay

This Worker receives explicitly consented schema-v1 error reports, rebuilds
public issue content from an allowlist, and creates or updates issues through a
dedicated GitHub App. The desktop application never receives a GitHub
credential. Treat this document as an operator runbook, not as a client setup
guide.

Do not deploy from this runbook during Task 11. Live account and credential
work belongs to the owner-mediated rollout tasks. Keep private keys, canary
tokens, HMAC keys, OAuth tokens, and secret values out of chat, tickets, shell
history, command arguments, Git, logs, and screenshots.

## Prerequisites

- GitHub organization-owner or GitHub App manager access for `sanghyun-io`.
- A Cloudflare account allowed to create Workers and D1 databases.
- Node.js and npm versions from `package.json`, Wrangler from this package, and
  OpenSSL available locally.
- A trusted local machine. Do not perform key conversion in a synchronized
  folder, repository directory, or shared temp directory.

Run all Wrangler commands below from `services/issue-relay` so Wrangler uses
`wrangler.jsonc`.

## Create the dedicated GitHub App

1. In GitHub, open the `sanghyun-io` organization, then **Settings** >
   **Developer settings** > **GitHub Apps** > **New GitHub App**.
2. Set **GitHub App name** to `TunnelForge Issue Relay`. Set **Homepage URL**
   to `https://github.com/sanghyun-io/tunnelforge`.
3. Leave user authorization disabled and leave callback/setup URLs empty.
   Clear **Active** under **Webhook**; this Worker does not receive webhooks.
4. Under **Repository permissions**, select exactly:
   - `Metadata: Read-only` (GitHub requires metadata access).
   - `Issues: Read and write`.
5. Leave every other repository, organization, and account permission at
   **No access**, and subscribe to no events.
6. Select **Only on this account**, create the App, and record its numeric App
   ID. The ID is operational metadata, not the private key.
7. On the App page, select **Install App**, choose `sanghyun-io`, choose
   **Only select repositories**, and select only `tunnelforge`. Record the
   numeric installation ID from the installation URL. Do not broaden the
   installation to all repositories.
8. Return to the App settings. Under **Private keys**, select **Generate a
   private key** once. GitHub downloads a PKCS#1 PEM file and retains only the
   public portion.

## Convert PKCS#1 to PKCS#8 locally

The Worker imports only unencrypted PKCS#8 `PRIVATE KEY` PEM. Create a private
local directory and move the downloaded file there using Explorer. Do not put
either PEM in this repository.

```powershell
$privateDir = Join-Path $env:USERPROFILE ".tunnelforge-relay-secrets"
New-Item -ItemType Directory -Force -Path $privateDir | Out-Null
icacls $privateDir /inheritance:r /grant:r "$($env:USERNAME):(OI)(CI)F"
```

Rename the downloaded file to `github-app-private-key.pem` inside that private
directory, then convert it locally:

```powershell
openssl pkcs8 -topk8 -nocrypt -in "$privateDir\github-app-private-key.pem" -out "$privateDir\github-app-private-key.pkcs8.pem"
Get-Content -LiteralPath "$privateDir\github-app-private-key.pkcs8.pem" -TotalCount 1
```

The one displayed line must be `-----BEGIN PRIVATE KEY-----`, not
`-----BEGIN RSA PRIVATE KEY-----`. Do not print the rest of the file.

## Authenticate Wrangler

Use browser OAuth rather than an API token in an environment variable or
command argument:

```powershell
npx wrangler login --use-keyring
npx wrangler whoami
```

Complete the browser flow yourself. `whoami` may be recorded by account name
and account ID only; do not record Wrangler's stored OAuth material. The login
must fail rather than fall back to a plaintext credential file if an OS keyring
is unavailable. Use `npx wrangler logout` when this machine should no longer
retain the login.

## Create and migrate D1

Create the database once and copy only the returned database UUID into
`d1_databases[0].database_id` in `wrangler.jsonc`:

```powershell
npx wrangler d1 create tunnelforge-issue-relay
npx wrangler d1 migrations list tunnelforge-issue-relay --remote
npx wrangler d1 migrations apply tunnelforge-issue-relay --remote
```

For local verification, use Wrangler's local D1 state. This does not touch the
remote database:

```powershell
npx wrangler d1 migrations apply tunnelforge-issue-relay --local
```

Review `migrations/` before every apply. A Worker rollback does not roll back D1
state.

## Deploy off before adding credentials

Confirm `vars.RELAY_MODE` is `off` in `wrangler.jsonc`, then run the local gates
and create the first deployment:

```powershell
npm ci
npm test -- --run
npm run typecheck
npm audit --omit=dev
npx wrangler deploy --dry-run
npx wrangler deploy
```

The mode inventory is:

```text
RELAY_MODE=off
RELAY_MODE=shadow
RELAY_MODE=canary
RELAY_MODE=active
```

`off` rejects reports before GitHub access. It is the initial and emergency
mode.

## Upload secrets without command-line values

Wrangler's interactive prompt is suitable only for the four one-line values
below. Run each command exactly as shown and enter the value only at Wrangler's
private local prompt. Do not append a value to any command and do not pipe a
file or environment variable into Wrangler.

```powershell
npx wrangler secret put GITHUB_APP_ID
npx wrangler secret put GITHUB_APP_INSTALLATION_ID
npx wrangler secret put INSTALLATION_ID_HMAC_KEY
npx wrangler secret put CANARY_ADMIN_TOKEN
```

Upload the multiline PEM through the Cloudflare Dashboard, not Wrangler's
single-line prompt:

1. Open **Workers & Pages** > **tunnelforge-issue-relay** > **Settings** >
   **Variables and Secrets**.
2. Add a secret named `GITHUB_APP_PRIVATE_KEY` and choose the encrypted-secret
   type.
3. Enter the complete local PKCS#8 PEM as a multiline encrypted secret in the
   Dashboard value field, then save and deploy that secret version. Do not put
   the PEM in a command argument, stdin pipeline, documentation example, test
   fixture, chat, ticket, log, or screenshot.
4. Confirm only that the secret name is present. Do not read the value back or
   capture it as evidence.

Create independent, high-entropy values for the HMAC and canary secrets using
an approved local password manager or cryptographic generator. Never reuse the
GitHub key, Releaser credential, installation token, or a personal token.

After the Cloudflare Dashboard confirms the private-key upload, delete both
local PEM copies and the private directory immediately:

```powershell
Remove-Item -LiteralPath "$privateDir\github-app-private-key.pem" -Force
Remove-Item -LiteralPath "$privateDir\github-app-private-key.pkcs8.pem" -Force
Remove-Item -LiteralPath $privateDir -Force
$privateDir = $null
```

Also empty the recycle bin if Explorer moved either file there. Verify `git
status --short --ignored` does not show a PEM or `.dev.vars` staged for commit.

## Mode rollout

For each transition, edit only `vars.RELAY_MODE` in `wrangler.jsonc`, review the
diff, run `npx wrangler deploy --dry-run`, then run `npx wrangler deploy`.
Check `GET /health` after deployment and require its exact mode to match.

1. **off**: verify a synthetic report gets `service_unavailable` and no GitHub
   subrequest occurs.
2. **shadow**: verify valid/invalid/oversized/rate-limit probes. Shadow may
   validate and rate-limit but must not persist raw report data or access
   GitHub.
3. **canary**: canary accepts authenticated operator requests; normal
   unauthenticated reports must get `unauthorized`. The Worker authenticates
   the operator token but does not enforce one fixture or fingerprint. The
   operator rollout deliberately submits only the designated synthetic fixture
   and one recurrence. Inspect the public title, body, labels, author, and
   comment before proceeding, then close that issue.
4. **active**: promote only after the canary evidence is clean. Re-run health
   and abuse probes, and monitor Worker errors and D1/global write budgets.

The smoke command accepts only the endpoint origin and expected mode. It never
accepts a private key, token, HMAC value, or other credential:

```powershell
$env:RELAY_ENDPOINT = "https://tunnelforge-issue-relay.<account>.workers.dev"
$env:RELAY_MODE = "shadow"
node scripts/smoke.mjs
Remove-Item Env:RELAY_ENDPOINT, Env:RELAY_MODE
```

For remote `active`, the smoke runner checks health only to avoid creating a
public issue. Active report-contract smoke is enabled only for loopback mocks.
In `canary`, the runner intentionally proves that an unauthenticated synthetic
fixture is denied; the authorized canary is a separate owner-mediated step.

## Canary request without command-line secrets

Set `RELAY_ENDPOINT` in the current PowerShell session. Load the shared
synthetic fixture and calculate its canonical fingerprint locally:

```powershell
$report = Get-Content ..\..\contracts\error-reporting\v1\valid-minimal.json -Raw | ConvertFrom-Json
$report.error.sanitized_message = "Synthetic canary fixture."
$canonical = [ordered]@{
    app_frame_signature = @()
    db_engine = $report.operation.db_engine
    error_code = ""
    exception_class = $report.error.exception_class
    operation_kind = $report.operation.kind
} | ConvertTo-Json -Compress
$sha = [Security.Cryptography.SHA256]::Create()
$digest = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($canonical))
$report.report.error_fingerprint = ([BitConverter]::ToString($digest)).Replace("-", "").ToLowerInvariant()
```

Read the canary token into memory through a hidden local prompt, use it for one
request, and clear the variables in `finally`. The value is never a process
argument:

```powershell
$secureToken = Read-Host "Canary token" -AsSecureString
try {
    $token = [Net.NetworkCredential]::new("", $secureToken).Password
    $headers = @{ Authorization = "Bearer $token" }
    Invoke-RestMethod -Uri "$env:RELAY_ENDPOINT/v1/reports" -Method Post -Headers $headers -ContentType "application/json" -Body ($report | ConvertTo-Json -Depth 8 -Compress)
} finally {
    $headers = $null
    $token = $null
    $secureToken.Dispose()
    $secureToken = $null
    $report = $null
}
```

Do not use this authenticated request in `active`; it is exclusively a
designated canary operation.

## Rollback

The fastest abuse stop is a new `off` deployment: set `vars.RELAY_MODE` to
`off`, dry-run, deploy, and verify health plus a rejected synthetic report.
This is safer than assuming the prior version was off.

For a code regression after the service is already stopped, inspect deployment
history and use Wrangler's interactive rollback:

```powershell
npx wrangler deployments list
npx wrangler rollback
```

Confirm the selected version and verify `/health` immediately. Rollback creates
a new deployment and does not revert or delete D1 data. If bindings or D1
schema changed, validate compatibility before selecting an old version.

## Delete credentials

Stop traffic with an `off` deployment before deleting a credential. Delete
Worker secrets interactively by name; each deletion creates a new deployment:

```powershell
npx wrangler secret delete CANARY_ADMIN_TOKEN
npx wrangler secret delete INSTALLATION_ID_HMAC_KEY
npx wrangler secret delete GITHUB_APP_PRIVATE_KEY
npx wrangler secret delete GITHUB_APP_INSTALLATION_ID
npx wrangler secret delete GITHUB_APP_ID
```

Then open GitHub organization **Settings** > **Developer settings** > **GitHub
Apps** > **TunnelForge Issue Relay** > **Private keys** and delete the matching
key. GitHub requires a replacement before deleting an App's only key; if the
service is being retired, uninstall or delete the App after the Worker is off.
For rotation, upload and verify an overlapping replacement first, wait out the
one-hour installation-token lifetime, then delete the old key.

Removing this dedicated reporter credential does not authorize deletion of the
separate `RELEASER_APP_PRIVATE_KEY`. The exposed legacy App ID `2735888` and
the repository secret `GH_APP_PRIVATE_KEY` follow the Task 12 inventory and
coupling gate before deletion.

## References

- [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app)
- [GitHub App permissions](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/choosing-permissions-for-a-github-app)
- [GitHub App private-key management](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/managing-private-keys-for-github-apps)
- [Wrangler login](https://developers.cloudflare.com/workers/wrangler/commands/general/)
- [Cloudflare Worker secrets](https://developers.cloudflare.com/workers/configuration/secrets/)
- [D1 Wrangler commands](https://developers.cloudflare.com/d1/wrangler-commands/)
- [D1 migrations](https://developers.cloudflare.com/d1/reference/migrations/)
- [Worker rollbacks](https://developers.cloudflare.com/workers/versions-and-deployments/rollbacks/)
