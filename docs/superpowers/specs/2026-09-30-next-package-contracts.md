# Next package shared contracts (TF-STATUS-110 / 112 / 111)

Date: 2026-09-30. Owner: Opus 5.5 manager. Workers: Sonnet 5.5 A (110→120),
B (112), C (111→105/108→119). User decisions are recorded in
`docs/current_status.md` (Recommended Execution Order, 2026-09-30).

This document fixes the interfaces that more than one lane touches. A worker that
needs a change here proposes it to the manager instead of editing another lane's
area.

## 0. Already landed in the preparation PR

- Rust `Endpoint.tls: TlsSettings` (`#[serde(default)]`), `TlsMode` =
  `disable` (default) | `verify_ca` | `verify_full`. There is intentionally no
  unverified `require` mode.
- `TlsSettings.ca_file` (extra PEM CA, in addition to the OS store) and
  `TlsSettings.server_name` (name verified instead of `host`).
- Every production PostgreSQL connection goes through
  `adapters::connect_postgres(endpoint)`; every MySQL connection through
  `adapters::mysql_opts(endpoint)`. Out-of-band PG cancel goes through
  `adapters::cancel_postgres_query(token, endpoint)`.
- Python `DbEndpoint.tls_mode / tls_ca_file / tls_server_name`; `to_payload()`
  omits `tls` when disabled so legacy payloads are unchanged.

## 1. Stable error codes

Error and result events may carry `"error_code"` (string) next to the existing
human `message`. UI logic branches on `error_code`, never on message text.

| Code | Lane | Meaning |
| --- | --- | --- |
| `tls_verification_failed` | A | Chain, expiry or host-name verification failed |
| `tls_unavailable` | A | Verified TLS requested but the server does not offer TLS |
| `ssh_host_key_unknown` | A | First contact; fingerprint must be confirmed by the user |
| `ssh_host_key_changed` | A | Stored fingerprint differs; connection blocked |
| `query_cancelled` | B | Cancelled by the user; server-side cancel was sent |
| `query_timeout` | B | `timeout_ms` elapsed; server-side cancel was sent |
| `connection_busy` | B | A query is already running on this `connection_id` |
| `multiple_result_sets_unsupported` | B | Statement produced more than one result set |
| `unsupported_objects` | C | Export scope contains objects the dump cannot preserve |

## 2. Lane A: connection trust (110), then wizard (120)

Policy (user decisions):
- Saved profiles without a TLS setting stay `disable` and show a persistent
  warning; they are not silently upgraded.
- New profiles default to `verify_full`. `disable` is allowed without warning only
  for a **direct** connection to a loopback target (`localhost`, `127.0.0.0/8`,
  `::1`). SSH-tunnel connections are never exempt.
- Through an SSH tunnel `host` is the local forwarded port, so `server_name` is set
  to the tunnel's `remote_host` automatically.
- SSH host keys use trust-on-first-use: unknown key → show SHA-256 fingerprint,
  persist only after confirmation; changed key → block with
  `ssh_host_key_changed`, replace only through an explicit "update key" action.
  Applies to both the paramiko probe and the forwarder path.
- Encrypted private keys: prompt for the passphrase, keep it in memory for the
  session only, never write it to config.

Implementation owned by A: `native-tls` (+ `postgres-native-tls`, mysql crate TLS
feature) inside `connect_postgres`, `cancel_postgres_query` and `mysql_opts`;
`tunnel_engine.py`; `config_manager.py` profile/known-host storage; tunnel and
connection dialogs; DbEndpoint construction sites, including only the
`_endpoint()` method of `src/exporters/rust_dump_exporter.py`.

## 3. Lane B: query control (112)

Protocol:
- `query.execute` payload adds `job_id` (client generated), optional
  `timeout_ms`, `max_rows`, `max_bytes`. With `stream_rows`, `columns` and row
  batch events are emitted **while rows are fetched**, not after collecting a full
  `Vec`. The final `result` adds `truncated` (bool), `truncated_by`
  (`rows`|`bytes`|null), `cancelled`, `timed_out`, and `in_transaction` when known.
- `query.cancel {job_id}` must be processed while the query is running and
  returns `cancelled` plus `server_cancel_sent`. MySQL: `KILL QUERY <thread id>` on
  a separate short connection built from `mysql_opts`. PostgreSQL:
  `cancel_postgres_query` with the token captured when the session opened.
- At most one in-flight query per `connection_id`; a second request gets
  `connection_busy`. Other commands keep their current synchronous semantics.
- After cancel/timeout the session remains usable and reports whether a manual
  transaction is still open; it must not silently commit or roll back.
- Multiple result sets: refuse with `multiple_result_sets_unsupported`.

Client:
- `DbCoreServiceClient` gets one stdout reader thread that routes events by
  `request_id`; the lock covers writes only. All existing callers (dump/import,
  migration, schema) must keep working unchanged.
- SQL editor defaults: 100,000 rows or 256 MiB displayed, whichever comes first,
  with a visible "results truncated" notice; timeout is user-configurable and off
  by default.

Owned by B: `protocol.rs`, `query.rs`, `main.rs`, `src/core/db_core_client.py`,
query methods of `src/core/db_core_facade.py`, SQL editor/execution workers and
their dialogs. B does not edit `connect_postgres`/`mysql_opts`.

## 4. Lane C: export contract (111), then faults (105/108), then backups (119)

- Publish an engine/version/object support table in
  `docs/export_import_policy.md`: preserved, refused before export, or omitted
  with warning.
- Default export refuses (`unsupported_objects`, listing each object) when the
  selected scope contains objects the format cannot preserve. The user may proceed
  only through an explicit "table data only (incomplete)" choice that keeps
  `strict_export=false` and the persistent warnings. Nothing is refused after the
  target or dump file has been partially written.
- 105/108 run against the `tf-test-` Docker environment (restricted MySQL account,
  concurrent writers, injected disconnects/ambiguous commits) and record exactly
  what was executed.

Owned by C: object detection in `schema.rs` (not its connection lines), `dump.rs`,
`dump_format.rs`, `ddl.rs`, `import.rs`, `safe_restore*.rs`,
`safe_promot*.rs` (not their connection helper), `rust_dump_exporter.py` except
`_endpoint()`, export/import dialogs, `docs/export_import_policy.md`.

## 5. Shared rules

- `docs/current_status.md` is updated only by the manager.
- Docker test resources use the `tf-test-{lane}-` prefix and are removed by the
  owner; never touch other containers or volumes.
- No real credentials, hosts or dumps in code, docs, logs or PR text.
