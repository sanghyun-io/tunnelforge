# One-Click Readiness Evidence

This directory stores machine-checkable evidence for GitHub #137.

## Current Evidence

- `oneclick-dry-run-evidence.json` was captured from a local MySQL container
  using Rust Core `oneclick.run` with `dry_run=true`.
- The report proves that Rust Core advertises the `oneclick.*` command surface,
  the hidden PyQt gate remains disabled, real One-Click execution remains
  disabled, and the dry-run workflow emitted preflight, analysis,
  recommendation, execution, validation, and final result events.

Validate it with:

```powershell
python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json
```

A clean checkout can also require this evidence through the Rust Core regression
gate:

```powershell
$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
```

## Refresh

Use a local MySQL test container only. The capture helper refuses schemas and
tables outside the `tf_oneclick_` prefix when seeding local data.

```powershell
python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json
python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json
```

Do not use production databases for this evidence.
