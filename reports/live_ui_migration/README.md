# Live UI Migration Evidence

This directory defines the final evidence shape for GitHub #136 and the
remaining #99 closure gate.

The actual completed evidence file is intentionally not checked in yet. It must
be produced from a live MySQL/PostgreSQL validation run through the PyQt worker
path.

Preferred capture command for local Docker validation:

```powershell
python scripts\capture-live-ui-migration-evidence.py `
  --seed-local-containers `
  --rows 1000000 `
  --chunk-size 10000 `
  --output reports\live_ui_migration\live-ui-migration-evidence.json `
  --stress-source-type synthetic_adapter `
  --stress-peak-rss-mb <observed_peak_rss_mb> `
  --stress-rss-limit-mb <accepted_rss_limit_mb> `
  --stress-notes "<10M memory measurement method and artifact paths>"
```

The capture script seeds the `tf-live-mysql` and `tf-live-postgres` containers
with deterministic `tf_live_*` source tables, runs both directions through
`CrossEngineMigrationWorker`, samples the Qt event-loop heartbeat while the
migration worker runs, and writes the validator-compatible JSON report.

Then run:

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

Do not commit smoke reports with fewer than 1,000,000 rows. They are useful only
for checking the capture script path and must fail the final validator.
