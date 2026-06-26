# One-Click Readiness Evidence

This directory stores machine-checkable One-Click readiness evidence.

## Current Evidence

- `oneclick-dry-run-evidence.json` was captured from a local MySQL container
  using Rust Core `oneclick.run` with `dry_run=true`.
- The report proves that Rust Core advertises the `oneclick.*` command surface,
  the PyQt entry point is exposed, and the dry-run workflow emitted preflight,
  analysis, recommendation, execution, validation, and final result events.
- `oneclick-real-execution-evidence.json` was captured from a local MySQL
  container using Rust Core `oneclick.apply_fixes` with `dry_run=false` against
  `tf_oneclick_real_execution.tf_oneclick_legacy_engine_table`.
- The real-execution report proves the first allowed automatic fix,
  `deprecated_engine -> engine_innodb`, changed that local test table from
  `MyISAM` to `InnoDB`. It was captured while the app-level real-execution
  feature flag remained disabled; the UI gate was opened only after this
  evidence and the UI-facing `oneclick.run dry_run=false` sequencing were
  proven.
- `oneclick-real-execution-evidence.template.json` documents the required
  GitHub #138 evidence shape for a future controlled local non-dry-run
  `deprecated_engine -> engine_innodb` run. It is a template only, not
  completed evidence.
- `oneclick-charset-evidence.template.json` documents the GitHub #139 evidence
  shape for future controlled local `charset_issue -> charset_collation_fk_safe`
  runs. It is a template only, not completed evidence.

Validate it with:

```powershell
python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json
```

Validate the real-execution evidence with:

```powershell
python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json
```

Validate future charset/collation evidence with:

```powershell
python scripts\validate-oneclick-charset-evidence.py reports\oneclick_readiness\oneclick-charset-evidence.json
```

A clean checkout can also require this evidence through the Rust Core regression
gate:

```powershell
$env:RUST_CORE_REQUIRE_ONECLICK_DRY_RUN_EVIDENCE='1'
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
```

The real-execution evidence gate can be required with:

```powershell
$env:RUST_CORE_REQUIRE_ONECLICK_REAL_EXECUTION_EVIDENCE='1'
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
```

The charset/collation evidence gate can be required after completed evidence is
captured:

```powershell
$env:RUST_CORE_REQUIRE_ONECLICK_CHARSET_EVIDENCE='1'
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
```

## Refresh

Use a local MySQL test container only. The capture helper refuses schemas and
tables outside the `tf_oneclick_` prefix when seeding local data.

```powershell
python scripts\capture-oneclick-dry-run-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-dry-run-evidence.json
python scripts\validate-oneclick-dry-run-evidence.py reports\oneclick_readiness\oneclick-dry-run-evidence.json
```

Refresh the real-execution evidence with:

```powershell
cargo build --manifest-path migration_core\Cargo.toml --release
python scripts\capture-oneclick-real-execution-evidence.py --seed-local-container --output reports\oneclick_readiness\oneclick-real-execution-evidence.json
python scripts\validate-oneclick-real-execution-evidence.py reports\oneclick_readiness\oneclick-real-execution-evidence.json
```

Do not use production databases for this evidence.
