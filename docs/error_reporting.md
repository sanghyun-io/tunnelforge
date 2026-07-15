# Anonymous Error Reporting

TunnelForge error reporting is an explicit, optional opt-in. The application
does not send a report when the consent prompt is shown, when settings are
opened, or when a local preview is generated. Existing legacy settings do not
count as affirmative consent.

## What Is Sent

After consent, the client builds schema v1 data from a strict allowlist. The
allowlist contains bounded application and runtime information, operation
metadata, and a sanitized error summary. Unknown fields are rejected rather
than collected and filtered later. Reports are best-effort and never change
the result of an Export, Import, migration, shutdown, or cleanup operation.

The client does not send database credentials, SQL or arbitrary context, source
files, user names, raw paths, access tokens, private keys, PEM data, or a
GitHub installation identity. The complete schema and sanitizer behavior are
implemented in the client reporting modules and tested against the shared
contract fixtures.

## Local Preview and Health

The Settings preview is read-only: it builds and displays the local sanitized
JSON without writing configuration or making a network request. A relay health
check is a separate retained background operation. It does not capture consent,
enable reporting, or update the last-submission record. When no relay is
configured, preview remains available while opt-in and health actions remain
disabled.

## Relay Behavior

With current consent, the desktop sends the allowlisted report over HTTPS to
the relay configured for that build. The relay is the only reporting transport;
the desktop does not authenticate directly to GitHub or create issues itself.
TLS verification remains enabled, requests are bounded, and there is no local
report queue or inbound listener. A transport failure is recorded as a bounded
last-attempt status and is otherwise ignored so the original application
operation is unaffected.

No client credential is required. The desktop package contains no GitHub App
private key, installation token, JWT, shared API secret, or user token.

The relay has four server-side modes. `off` rejects reports before GitHub
access, `shadow` validates without repository mutation, `canary` accepts
authenticated operator requests, and `active` handles opted-in client reports.
The canary rollout procedure limits its own submissions to the designated
synthetic fixture and one recurrence; the Worker does not enforce that fixture
identity. Mode changes do not require a desktop release. The complete creation,
migration, secret upload, canary, rollback, and credential-deletion procedure
is the [relay operator runbook](../services/issue-relay/README.md).

## Privacy

Reporting is disabled until the user explicitly enables it and can be disabled
again from Settings. The relay must independently validate the schema, apply
its privacy and abuse controls, and avoid logging or storing raw report bodies,
source IP addresses, anonymous identifiers, or credentials. Do not paste
secrets into error messages or diagnostic fields.

## Troubleshooting

- If preview works but opt-in and health are disabled, the build has no relay
  configuration. This is expected and does not require a GitHub key.
- If a health check fails, verify network access and the system's TLS trust
  configuration, then retry from Settings.
- If a submission fails, use the last-attempt status in Settings for the
  bounded result. The report is not queued for later delivery.
- To stop reporting, disable it in Settings. Disabling consent does not affect
  tunnels, database operations, or local diagnostics.
- Operators responding to a relay incident should deploy `off` first. They
  should not request a private key, canary token, HMAC value, or GitHub token
  from a user; the desktop has none of those credentials.

For implementation and release history, see the repository's historical status
and incident documents. Those records are retained separately from this
client-facing behavior guide.
