# Rust Core Performance Evidence

This directory preserves the large-scale Rust DB Core evidence required by
GitHub #99 and #135.

Archived evidence:

- `perf_pg_mysql_1m_migrate.jsonl` - 1M row migration result with row progress.
- `perf_pg_mysql_1m_verify.jsonl` - 1M row verification result.
- `perf_stress_10m_resume.jsonl` - 10M row resume/stress migration result.
- `perf_stress_10m_verify.jsonl` - 10M row verification result.

Validate the archived evidence from a clean checkout:

```powershell
python scripts\validate-rust-core-performance-evidence.py
```

The validator checks that all four files exist, contain successful Rust Core
`result` events, prove the required row counts for migration/resume evidence,
and do not report verification mismatches.

To refresh the evidence, regenerate the corresponding JSONL files under
`migration_core\target`, rerun the Rust Core performance gate with
`RUST_CORE_REQUIRE_PERF_EVIDENCE=1`, copy the refreshed four files into this
directory, and rerun the validator above before updating #99/#135.
