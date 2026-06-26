# Live UI Migration Evidence

This directory defines the final evidence shape for GitHub #136 and the
remaining #99 closure gate.

The actual completed evidence file is intentionally not checked in yet. It must
be produced from a live MySQL/PostgreSQL validation run through the PyQt
worker/dialog path.

Create the evidence by copying `live-ui-migration-evidence.template.json` to
`live-ui-migration-evidence.json`, filling in the observed values, and running:

```powershell
python scripts\validate-live-ui-migration-evidence.py reports\live_ui_migration\live-ui-migration-evidence.json
```

The validator requires:

- MySQL -> PostgreSQL 1M row migrate+verify success.
- PostgreSQL -> MySQL 1M row migrate+verify success.
- Worker progress events for both directions.
- UI heartbeat samples with `max_gap_ms <= max_allowed_gap_ms`.
- 10M stress/resume/verify evidence with source type and RSS bound.

Use `source_type` values such as `local_containers`, `remote_real_databases`,
or `synthetic_adapter` so #99 can distinguish live DB evidence from Rust-only
synthetic evidence.
