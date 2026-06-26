# Export Import Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make TunnelForge Export/Import recoverable and verifiable end to end, without temporary DB-specific patches or UI claims that the Rust core cannot prove.

**Architecture:** Rust `tunnelforge-core` owns dump/import semantics, import plans, validation, and success classification. Python/PyQt forwards user intent, displays classified events, and keeps UI text aligned with actual Rust behavior.

**2026-06-26 architecture update:** shadow full replacement is retired as a
current guarantee. Historical steps below that mention shadow schemas, shadow
workers, or schema switching are superseded unless a new product decision
reintroduces atomic shadow replacement with DB-specific switch, rollback,
cleanup, and worker endpoint semantics. The supported current architecture is
direct `replace`/`recreate`/`merge` import against the selected target database.

**Tech Stack:** Rust core in `migration_core/src/lib.rs`, Python wrapper in `src/exporters/rust_dump_exporter.py`, PyQt dialog code in `src/ui/dialogs/db_dialogs.py`, pytest tests under `tests/`, Rust tests through Cargo.

---

## Scope And Current-State Rules

Before editing code, inspect the current worktree. There are existing modified
files in this repo. Do not revert unrelated user changes.

Run:

```powershell
git status --short
git diff -- migration_core/src/lib.rs
git diff -- src/exporters/rust_dump_exporter.py src/ui/dialogs/db_dialogs.py tests/test_db_dialogs.py
```

Expected:

- `docs/superpowers/specs/2026-06-01-export-import-recovery-design.md` exists and is committed.
- Other files may already be modified; preserve unrelated edits.

## File Map

- Modify `migration_core/src/lib.rs`
  - Add import validation, import target context, classified errors, import report helpers, post-load policy helpers, and Rust tests.
  - Keep DB operations inside Rust core.
- Modify `src/exporters/rust_dump_exporter.py`
  - Forward missing import payload fields such as `timezone_sql`.
  - Preserve Rust classified error details in returned metadata.
- Modify `src/ui/dialogs/db_dialogs.py`
  - Align labels and warnings with real Rust support.
  - Surface limited/legacy dump status and classified failures.
- Modify `tests/test_db_dialogs.py`
  - Add UI wording and payload propagation coverage where dialog-level behavior belongs.
- Add or modify `tests/test_rust_dump_exporter.py`
  - Add wrapper-level tests for payload forwarding and classified error propagation.
- Modify `reports/export_import_flow_review_20260601.html`
  - Convert review-only report into a remediation report after implementation and verification.

Do not add Python DB-driver dump/import paths. Do not route DB semantics through
the UI layer.

---

### Task 1: Add Import Failure Classification And Manifest Strictness Helpers

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for manifest strictness and classified errors**

Add these tests inside the existing `#[cfg(test)] mod tests` in
`migration_core/src/lib.rs`, near the dump import tests.

```rust
#[test]
fn strict_manifest_validation_rejects_missing_chunk_checksums() {
    let table = DumpTableManifest {
        name: "users".to_string(),
        path: "0001_users".to_string(),
        rows: 10,
        chunks: 1,
        chunk_sha256: BTreeMap::new(),
    };

    let err = validate_dump_import_manifest_strictness(&[table], true).unwrap_err();

    assert!(err.contains("export_invalid"));
    assert!(err.contains("users"));
    assert!(err.contains("missing chunk_sha256"));
}

#[test]
fn legacy_manifest_validation_allows_missing_checksums_when_not_strict() {
    let table = DumpTableManifest {
        name: "users".to_string(),
        path: "0001_users".to_string(),
        rows: 10,
        chunks: 1,
        chunk_sha256: BTreeMap::new(),
    };

    let warnings = validate_dump_import_manifest_strictness(&[table], false).unwrap();

    assert_eq!(warnings, vec![
        "legacy dump: table users has chunks but no chunk_sha256 metadata".to_string()
    ]);
}

#[test]
fn classified_import_error_formats_code_scope_and_message() {
    let err = classified_import_error(
        "import_plan_invalid",
        "full replacement worker target is unresolved",
        Some("users"),
    );

    assert_eq!(
        err,
        "import_plan_invalid: users: full replacement worker target is unresolved"
    );
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml strict_manifest_validation_rejects_missing_chunk_checksums classified_import_error_formats_code_scope_and_message --lib
```

Expected: FAIL because `validate_dump_import_manifest_strictness` and
`classified_import_error` do not exist.

- [ ] **Step 3: Add minimal helpers**

Add this code near the manifest validation helpers, before
`validate_dump_manifest_chunks`.

```rust
fn classified_import_error(code: &str, message: &str, scope: Option<&str>) -> String {
    match scope.filter(|value| !value.trim().is_empty()) {
        Some(scope) => format!("{code}: {scope}: {message}"),
        None => format!("{code}: {message}"),
    }
}

fn validate_dump_import_manifest_strictness(
    tables: &[DumpTableManifest],
    strict: bool,
) -> Result<Vec<String>, String> {
    let mut warnings = Vec::new();
    for table in tables {
        if table.chunks > 0 && table.chunk_sha256.is_empty() {
            let message = format!(
                "table {} has chunks but no chunk_sha256 metadata",
                table.name
            );
            if strict {
                return Err(classified_import_error(
                    "export_invalid",
                    &format!("missing chunk_sha256; {message}"),
                    Some(&table.name),
                ));
            }
            warnings.push(format!("legacy dump: {message}"));
        }
    }
    Ok(warnings)
}
```

- [ ] **Step 4: Wire strictness into `dump_import` before target mutation**

In `dump_import`, after selected `tables` are computed and before
`LiveAdapter::connect(&endpoint)?`, add:

```rust
let strict_manifest = request
    .payload
    .get("strict_manifest")
    .and_then(Value::as_bool)
    .unwrap_or(true);
let manifest_warnings = validate_dump_import_manifest_strictness(&tables, strict_manifest)?;
```

After `local_infile_restore` setup and before import work starts, emit warnings:

```rust
for warning in &manifest_warnings {
    emit(json!({
        "event": "warning",
        "request_id": request.request_id,
        "phase": "dump_import_manifest",
        "classification": "legacy_dump",
        "message": warning
    }));
}
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml strict_manifest_validation_rejects_missing_chunk_checksums legacy_manifest_validation_allows_missing_checksums_when_not_strict classified_import_error_formats_code_scope_and_message --lib
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: classify strict dump import manifests"
```

---

### Task 2: Retired Draft - Shadow Worker Target Context

**Files:**
- Modify: `migration_core/src/lib.rs`

Decision update, 2026-06-26: this task is superseded. The current supported
architecture is direct `replace`/`recreate`/`merge` import against the selected
target database. Do not implement the shadow worker helper below unless a new
product decision reintroduces atomic shadow replacement with DB-specific
switch/cleanup semantics.

- [ ] **Step 1: Write failing Rust tests for shadow endpoint resolution**

Add these tests near the existing `mysql_shadow_schema_name_is_safe_and_bounded`
test.

```rust
#[test]
fn mysql_shadow_import_endpoint_targets_shadow_database() {
    let endpoint = Endpoint {
        engine: "mysql".to_string(),
        host: "127.0.0.1".to_string(),
        port: 3306,
        user: "root".to_string(),
        password: "secret".to_string(),
        database: "dataflare".to_string(),
        schema: None,
    };

    let shadow = mysql_import_worker_endpoint(&endpoint, Some("_tf_restore_dataflare_1"));

    assert_eq!(shadow.database, "_tf_restore_dataflare_1");
    assert_eq!(shadow.host, endpoint.host);
    assert_eq!(shadow.user, endpoint.user);
}

#[test]
fn direct_import_worker_endpoint_keeps_target_database() {
    let endpoint = Endpoint {
        engine: "mysql".to_string(),
        host: "127.0.0.1".to_string(),
        port: 3306,
        user: "root".to_string(),
        password: "secret".to_string(),
        database: "dataflare".to_string(),
        schema: None,
    };

    let direct = mysql_import_worker_endpoint(&endpoint, None);

    assert_eq!(direct.database, "dataflare");
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml mysql_shadow_import_endpoint_targets_shadow_database direct_import_worker_endpoint_keeps_target_database --lib
```

Expected: FAIL because `mysql_import_worker_endpoint` does not exist.

- [ ] **Step 3: Add endpoint helper**

Add this near the MySQL shadow helpers.

```rust
fn mysql_import_worker_endpoint(endpoint: &Endpoint, database: Option<&str>) -> Endpoint {
    let mut worker_endpoint = endpoint.clone();
    if let Some(database) = database.filter(|value| !value.trim().is_empty()) {
        worker_endpoint.database = database.to_string();
        worker_endpoint.schema = Some(database.to_string());
    }
    worker_endpoint
}
```

- [ ] **Step 4: Change `import_dump_table_data` to accept the effective worker endpoint**

Update the call in the full replacement shadow branch:

```rust
let worker_endpoint = mysql_import_worker_endpoint(&endpoint, Some(&shadow_database));
import_dump_table_data(
    &mut adapter,
    &worker_endpoint,
    input_path,
    &tables,
    &import_schema,
    &data_format,
    &compression,
    threads,
    request.request_id.clone(),
    table_total,
    &mut rows_imported,
    &mut chunks_imported,
    &mut emit,
)?;
```

Keep direct replace/merge calls using:

```rust
let worker_endpoint = mysql_import_worker_endpoint(&endpoint, None);
import_dump_table_data(
    &mut adapter,
    &worker_endpoint,
    input_path,
    &tables,
    &import_schema,
    &data_format,
    &compression,
    threads,
    request.request_id.clone(),
    table_total,
    &mut rows_imported,
    &mut chunks_imported,
    &mut emit,
)?;
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml mysql_shadow_import_endpoint_targets_shadow_database direct_import_worker_endpoint_keeps_target_database --lib
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: route shadow import workers to shadow database"
```

---

### Task 3: Split Post-Load DDL Policy By Import Mode

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for post-load policy**

Add:

```rust
#[test]
fn post_load_ddl_policy_applies_for_recreated_targets_only() {
    assert!(should_apply_post_load_ddl("recreate", true));
    assert!(should_apply_post_load_ddl("replace", true));
    assert!(!should_apply_post_load_ddl("merge", false));
    assert!(!should_apply_post_load_ddl("merge", true));
}

#[test]
fn merge_import_does_not_claim_post_load_ddl_phase() {
    assert_eq!(
        post_load_ddl_skip_message("merge"),
        "skipping post-load DDL for merge import; existing objects must already match"
    );
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_policy_applies_for_recreated_targets_only merge_import_does_not_claim_post_load_ddl_phase --lib
```

Expected: FAIL because helpers do not exist.

- [ ] **Step 3: Add policy helpers**

Add near `apply_post_load_ddl`.

```rust
fn should_apply_post_load_ddl(mode: &str, recreated_target: bool) -> bool {
    recreated_target && matches!(mode, "replace" | "recreate")
}

fn post_load_ddl_skip_message(mode: &str) -> String {
    format!(
        "skipping post-load DDL for {mode} import; existing objects must already match"
    )
}
```

- [ ] **Step 4: Use the policy in `dump_import`**

Track whether target tables were recreated:

```rust
let mut recreated_target = false;
```

Set it to true in the shadow branch after `create_dump_import_tables` succeeds:

```rust
recreated_target = true;
```

Set it to true in the direct replace/recreate branch after `create_dump_import_tables` succeeds:

```rust
recreated_target = true;
```

Replace the unconditional non-shadow post-load block:

```rust
emit(json!({
    "event": "phase",
    "request_id": request.request_id,
    "phase": "dump_import_post_load",
    "message": "creating indexes and foreign keys"
}));
apply_post_load_ddl(&mut adapter, &import_schema, &target_engine)?;
```

with:

```rust
if should_apply_post_load_ddl(mode, recreated_target) {
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump_import_post_load",
        "message": "creating indexes and foreign keys"
    }));
    apply_post_load_ddl(&mut adapter, &import_schema, &target_engine)?;
} else {
    emit(json!({
        "event": "phase",
        "request_id": request.request_id,
        "phase": "dump_import_post_load",
        "message": post_load_ddl_skip_message(mode),
        "strategy": "existing_schema"
    }));
}
```

Do not change the shadow branch post-load DDL; shadow recreate must still apply
DDL before switching.

- [ ] **Step 5: Run focused tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml post_load_ddl_policy_applies_for_recreated_targets_only merge_import_does_not_claim_post_load_ddl_phase --lib
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: separate merge import post load ddl policy"
```

---

### Task 4: Add Import Verification Summary And Success Gate

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for row-count verification**

Add:

```rust
#[test]
fn import_row_count_verification_rejects_missing_rows() {
    let tables = vec![DumpTableManifest {
        name: "users".to_string(),
        path: "0001_users".to_string(),
        rows: 3,
        chunks: 1,
        chunk_sha256: BTreeMap::new(),
    }];
    let mut imported = BTreeMap::new();
    imported.insert("users".to_string(), 2_u64);

    let err = verify_imported_row_counts(&tables, &imported).unwrap_err();

    assert!(err.contains("post_load_validation_failed"));
    assert!(err.contains("users"));
    assert!(err.contains("expected 3 rows, imported 2"));
}

#[test]
fn import_row_count_verification_accepts_matching_counts() {
    let tables = vec![DumpTableManifest {
        name: "users".to_string(),
        path: "0001_users".to_string(),
        rows: 3,
        chunks: 1,
        chunk_sha256: BTreeMap::new(),
    }];
    let mut imported = BTreeMap::new();
    imported.insert("users".to_string(), 3_u64);

    verify_imported_row_counts(&tables, &imported).unwrap();
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_row_count_verification_rejects_missing_rows import_row_count_verification_accepts_matching_counts --lib
```

Expected: FAIL because `verify_imported_row_counts` does not exist.

- [ ] **Step 3: Add row-count verification helper**

Add near import helper functions:

```rust
fn verify_imported_row_counts(
    tables: &[DumpTableManifest],
    imported_rows_by_table: &BTreeMap<String, u64>,
) -> Result<(), String> {
    for table in tables {
        let imported = imported_rows_by_table.get(&table.name).copied().unwrap_or(0);
        if imported != table.rows {
            return Err(classified_import_error(
                "post_load_validation_failed",
                &format!("expected {} rows, imported {}", table.rows, imported),
                Some(&table.name),
            ));
        }
    }
    Ok(())
}
```

- [ ] **Step 4: Track imported rows by table**

Change `import_dump_table_data` signature to accept:

```rust
imported_rows_by_table: &mut BTreeMap<String, u64>,
```

Inside each table loop, track per-table rows:

```rust
let before_rows = *rows_imported;
```

Before each table completion event, insert:

```rust
let table_rows_imported = rows_imported.saturating_sub(before_rows);
imported_rows_by_table.insert(table.name.clone(), table_rows_imported);
```

Update every call site to pass:

```rust
&mut imported_rows_by_table,
```

Define the map in `dump_import` beside row counters:

```rust
let mut imported_rows_by_table: BTreeMap<String, u64> = BTreeMap::new();
```

- [ ] **Step 5: Gate success on row-count verification**

After `import_result?`, before returning success:

```rust
verify_imported_row_counts(&tables, &imported_rows_by_table)?;
```

Add result payload detail:

```rust
"verification": {
    "row_counts": "passed",
    "strict_manifest": strict_manifest,
    "warnings": manifest_warnings
},
```

- [ ] **Step 6: Run focused tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_row_count_verification_rejects_missing_rows import_row_count_verification_accepts_matching_counts --lib
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: gate dump import success on row counts"
```

---

### Task 5: Validate FK Schema Fidelity Before Post-Load DDL

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for FK column compatibility**

Add:

```rust
#[test]
fn fk_schema_fidelity_rejects_incompatible_text_collations() {
    let schema = NormalizedSchema {
        tables: vec![
            NormalizedTable {
                name: "audit_category".to_string(),
                columns: vec![NormalizedColumn {
                    name: "code".to_string(),
                    type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                        .to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            },
            NormalizedTable {
                name: "df_evaluation_results".to_string(),
                columns: vec![NormalizedColumn {
                    name: "audit_category_code".to_string(),
                    type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
                        .to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: vec![NormalizedForeignKey {
                    name: "df_evaluation_results_ibfk_3".to_string(),
                    columns: vec!["audit_category_code".to_string()],
                    referenced_table: "audit_category".to_string(),
                    referenced_columns: vec!["code".to_string()],
                }],
            },
        ],
    };

    let err = validate_foreign_key_column_compatibility(&schema).unwrap_err();

    assert!(err.contains("post_load_validation_failed"));
    assert!(err.contains("df_evaluation_results_ibfk_3"));
    assert!(err.contains("audit_category_code"));
    assert!(err.contains("code"));
}

#[test]
fn fk_schema_fidelity_accepts_matching_text_collations() {
    let schema = NormalizedSchema {
        tables: vec![
            NormalizedTable {
                name: "audit_category".to_string(),
                columns: vec![NormalizedColumn {
                    name: "code".to_string(),
                    type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                        .to_string(),
                    default_value: None,
                    nullable: false,
                    primary_key: true,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: Vec::new(),
            },
            NormalizedTable {
                name: "df_evaluation_results".to_string(),
                columns: vec![NormalizedColumn {
                    name: "audit_category_code".to_string(),
                    type_name: "varchar(45) CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                        .to_string(),
                    default_value: None,
                    nullable: true,
                    primary_key: false,
                    unique: false,
                }],
                indexes: Vec::new(),
                foreign_keys: vec![NormalizedForeignKey {
                    name: "df_evaluation_results_ibfk_3".to_string(),
                    columns: vec!["audit_category_code".to_string()],
                    referenced_table: "audit_category".to_string(),
                    referenced_columns: vec!["code".to_string()],
                }],
            },
        ],
    };

    validate_foreign_key_column_compatibility(&schema).unwrap();
}
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml fk_schema_fidelity_rejects_incompatible_text_collations fk_schema_fidelity_accepts_matching_text_collations --lib
```

Expected: FAIL because `validate_foreign_key_column_compatibility` does not
exist.

- [ ] **Step 3: Add FK compatibility helper**

Add near `apply_post_load_ddl`.

```rust
fn validate_foreign_key_column_compatibility(schema: &NormalizedSchema) -> Result<(), String> {
    for table in &schema.tables {
        for fk in &table.foreign_keys {
            let referenced_table = schema
                .tables
                .iter()
                .find(|candidate| candidate.name == fk.referenced_table)
                .ok_or_else(|| {
                    classified_import_error(
                        "post_load_validation_failed",
                        &format!("foreign key {} references missing table {}", fk.name, fk.referenced_table),
                        Some(&table.name),
                    )
                })?;
            for (column_name, referenced_column_name) in fk
                .columns
                .iter()
                .zip(fk.referenced_columns.iter())
            {
                let column = table
                    .columns
                    .iter()
                    .find(|candidate| candidate.name == *column_name)
                    .ok_or_else(|| {
                        classified_import_error(
                            "post_load_validation_failed",
                            &format!("foreign key {} references missing column {}", fk.name, column_name),
                            Some(&table.name),
                        )
                    })?;
                let referenced_column = referenced_table
                    .columns
                    .iter()
                    .find(|candidate| candidate.name == *referenced_column_name)
                    .ok_or_else(|| {
                        classified_import_error(
                            "post_load_validation_failed",
                            &format!(
                                "foreign key {} references missing column {}.{}",
                                fk.name, referenced_table.name, referenced_column_name
                            ),
                            Some(&table.name),
                        )
                    })?;
                if normalize_fk_type_signature(&column.type_name)
                    != normalize_fk_type_signature(&referenced_column.type_name)
                {
                    return Err(classified_import_error(
                        "post_load_validation_failed",
                        &format!(
                            "foreign key {} incompatible columns {}.{} ({}) and {}.{} ({})",
                            fk.name,
                            table.name,
                            column.name,
                            column.type_name,
                            referenced_table.name,
                            referenced_column.name,
                            referenced_column.type_name
                        ),
                        Some(&table.name),
                    ));
                }
            }
        }
    }
    Ok(())
}

fn normalize_fk_type_signature(type_name: &str) -> String {
    type_name
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .to_ascii_lowercase()
}
```

- [ ] **Step 4: Gate post-load FK creation on compatibility**

At the top of `apply_post_load_ddl`, before creating indexes or FKs, add:

```rust
validate_foreign_key_column_compatibility(schema)?;
```

- [ ] **Step 5: Run focused tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml fk_schema_fidelity_rejects_incompatible_text_collations fk_schema_fidelity_accepts_matching_text_collations --lib
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: validate foreign key column compatibility"
```

---

### Task 6: Preserve Table-Level MySQL Metadata And Strict Export Markers

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for table metadata fields**

Add:

```rust
#[test]
fn dump_manifest_table_options_default_to_empty_for_legacy_json() {
    let json = r#"{
        "format": "tunnelforge-dump",
        "format_version": 2,
        "source_engine": "mysql",
        "database": "dataflare",
        "schema": {"tables": []},
        "chunk_size": 1000,
        "created_unix_seconds": 1,
        "tables": []
    }"#;

    let manifest: DumpManifest = serde_json::from_str(json).unwrap();

    assert_eq!(manifest.snapshot_policy, "unknown");
    assert_eq!(manifest.schema.tables.len(), 0);
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_manifest_table_options_default_to_empty_for_legacy_json --lib
```

Expected: FAIL because `snapshot_policy` does not exist.

- [ ] **Step 3: Add default manifest fields**

Modify `DumpManifest`:

```rust
#[serde(default = "default_snapshot_policy")]
pub snapshot_policy: String,
#[serde(default)]
pub strict_export: bool,
#[serde(default)]
pub manifest_warnings: Vec<String>,
```

Add helper:

```rust
fn default_snapshot_policy() -> String {
    "unknown".to_string()
}
```

When constructing manifests in export code, set:

```rust
snapshot_policy: "connection_consistent".to_string(),
strict_export: true,
manifest_warnings: Vec::new(),
```

If the export path uses parallel reads without a shared snapshot, set:

```rust
snapshot_policy: "non_consistent_parallel".to_string(),
strict_export: false,
manifest_warnings: vec![
    "parallel export did not prove a shared consistent snapshot".to_string()
],
```

- [ ] **Step 4: Run focused manifest serialization tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_manifest_table_options_default_to_empty_for_legacy_json --lib
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: record dump snapshot strictness"
```

---

### Task 7: Forward Import Intent From Python Wrapper To Rust

**Files:**
- Modify: `src/exporters/rust_dump_exporter.py`
- Add or modify: `tests/test_rust_dump_exporter.py`

- [ ] **Step 1: Write failing pytest for `timezone_sql` and strict policy forwarding**

Create `tests/test_rust_dump_exporter.py` if it does not exist, or append to it.

```python
import json
from pathlib import Path

from src.exporters.rust_dump_exporter import RustDumpImporter


class FakeFacade:
    def __init__(self):
        self.payload = None

    def import_dump(self, payload, on_event=None):
        self.payload = payload
        return {"rows_imported": 0}


def test_import_dump_forwards_timezone_and_strict_manifest(tmp_path):
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps({
            "format": "tunnelforge-dump",
            "format_version": 2,
            "database": "dataflare",
            "tables": [],
        }),
        encoding="utf-8",
    )
    config = {
        "host": "127.0.0.1",
        "port": 3306,
        "username": "root",
        "password": "secret",
        "database": "dataflare",
        "db_type": "mysql",
    }
    importer = RustDumpImporter(config)
    importer.facade = FakeFacade()

    ok, message, _ = importer.import_dump(
        str(dump_dir),
        target_schema="dataflare",
        timezone_sql="SET SESSION time_zone = '+09:00'",
        import_mode="recreate",
    )

    assert ok is True
    assert "완료" in message
    assert importer.facade.payload["timezone_sql"] == "SET SESSION time_zone = '+09:00'"
    assert importer.facade.payload["strict_manifest"] is True
    assert importer.facade.payload["mode"] == "recreate"
```

- [ ] **Step 2: Run pytest to verify it fails**

Run:

```powershell
pytest tests/test_rust_dump_exporter.py::test_import_dump_forwards_timezone_and_strict_manifest -q
```

Expected: FAIL because `timezone_sql` and `strict_manifest` are not forwarded.

- [ ] **Step 3: Forward fields in `RustDumpImporter.import_dump`**

In `src/exporters/rust_dump_exporter.py`, update payload construction:

```python
payload = {
    "target": self._endpoint(final_target_schema).to_payload(),
    "input_dir": input_dir,
    "mode": import_mode,
    "threads": max(1, int(threads)),
    "strict_manifest": True,
}
if timezone_sql:
    payload["timezone_sql"] = timezone_sql
```

- [ ] **Step 4: Run focused pytest**

Run:

```powershell
pytest tests/test_rust_dump_exporter.py::test_import_dump_forwards_timezone_and_strict_manifest -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src\exporters\rust_dump_exporter.py tests\test_rust_dump_exporter.py
git commit -m "fix: forward rust dump import intent"
```

---

### Task 8: Apply `timezone_sql` In Rust Import Sessions

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust test for timezone SQL validation**

Add:

```rust
#[test]
fn import_timezone_sql_accepts_session_time_zone_only() {
    assert_eq!(
        validated_timezone_sql(Some("SET SESSION time_zone = '+09:00'")).unwrap(),
        Some("SET SESSION time_zone = '+09:00'".to_string())
    );
    assert!(validated_timezone_sql(Some("DROP DATABASE prod")).is_err());
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib
```

Expected: FAIL because `validated_timezone_sql` does not exist.

- [ ] **Step 3: Add validation helper**

Add near import payload helpers:

```rust
fn validated_timezone_sql(value: Option<&str>) -> Result<Option<String>, String> {
    let Some(sql) = value.map(str::trim).filter(|value| !value.is_empty()) else {
        return Ok(None);
    };
    let normalized = sql.to_ascii_lowercase();
    if normalized.starts_with("set session time_zone")
        && !normalized.contains(';')
        && !normalized.contains("--")
        && !normalized.contains("/*")
    {
        Ok(Some(sql.to_string()))
    } else {
        Err(classified_import_error(
            "import_plan_invalid",
            "unsupported timezone_sql; only SET SESSION time_zone is allowed",
            None,
        ))
    }
}
```

- [ ] **Step 4: Apply timezone SQL after adapter connection**

In `dump_import`, after `LiveAdapter::connect(&endpoint)?`:

```rust
let timezone_sql = validated_timezone_sql(
    request
        .payload
        .get("timezone_sql")
        .and_then(Value::as_str),
)?;
if let Some(sql) = timezone_sql.as_deref() {
    adapter.execute_sql(sql)?;
}
```

When a shadow branch calls `use_mysql_database`, apply timezone again on that
same session only if needed:

```rust
if let Some(sql) = timezone_sql.as_deref() {
    adapter.execute_sql(sql)?;
}
```

Do not apply arbitrary SQL in worker threads. If worker session timezone becomes
required for `LOAD DATA LOCAL`, add a typed field later instead of passing raw
SQL to workers.

- [ ] **Step 5: Run focused test**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_session_time_zone_only --lib
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: apply validated dump import timezone"
```

---

### Task 9: Align UI Wording With Supported Object Behavior

**Files:**
- Modify: `src/ui/dialogs/db_dialogs.py`
- Modify: `tests/test_db_dialogs.py`

- [ ] **Step 1: Write failing UI text test**

Add to `tests/test_db_dialogs.py`:

```python
def test_import_dialog_does_not_claim_all_objects_are_recreated(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )

    dialog = RustDumpImportDialog()
    texts = []
    for label in dialog.findChildren(type(dialog.label_core_status)):
        if hasattr(label, "text"):
            texts.append(label.text())
    combined = "\n".join(texts)

    assert "모든 객체" not in combined
    assert "테이블" in combined
    dialog.close()
```

- [ ] **Step 2: Run test to verify it fails if overpromising text remains**

Run:

```powershell
pytest tests/test_db_dialogs.py::test_import_dialog_does_not_claim_all_objects_are_recreated -q
```

Expected: FAIL if the dialog still contains "모든 객체".

- [ ] **Step 3: Replace overpromising text**

In `src/ui/dialogs/db_dialogs.py`, replace strings that imply every object is
restored. Use direct table-scoped wording:

```python
"테이블 구조와 데이터를 Rust DB Core로 복원합니다. "
"뷰/프로시저/트리거/이벤트는 현재 자동 복원 대상이 아니며, "
"덤프 검사 결과에서 별도 경고로 표시됩니다."
```

For full replacement wording, use:

```python
"전체 교체 Import는 임시 스키마 복원과 검증을 통과한 뒤 대상 테이블로 전환합니다."
```

- [ ] **Step 4: Run focused UI test**

Run:

```powershell
pytest tests/test_db_dialogs.py::test_import_dialog_does_not_claim_all_objects_are_recreated -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src\ui\dialogs\db_dialogs.py tests\test_db_dialogs.py
git commit -m "fix: align dump import ui wording"
```

---

### Task 10: Write Import Report Artifact

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust test for report path**

Add:

```rust
#[test]
fn import_report_path_lives_inside_dump_directory() {
    let path = dump_import_report_path(Path::new("C:/tmp/dump")).unwrap();

    assert!(path.ends_with("_tunnelforge_import_report.json"));
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_report_path_lives_inside_dump_directory --lib
```

Expected: FAIL because `dump_import_report_path` does not exist.

- [ ] **Step 3: Add report helper**

Add:

```rust
fn dump_import_report_path(input_path: &Path) -> Result<PathBuf, String> {
    let canonical = input_path
        .canonicalize()
        .map_err(|err| format!("cannot resolve import report directory: {err}"))?;
    Ok(canonical.join("_tunnelforge_import_report.json"))
}

fn write_dump_import_report(input_path: &Path, report: &Value) -> Result<(), String> {
    let report_path = dump_import_report_path(input_path)?;
    let bytes = serde_json::to_vec_pretty(report)
        .map_err(|err| format!("cannot serialize import report: {err}"))?;
    fs::write(&report_path, bytes)
        .map_err(|err| format!("cannot write import report {}: {err}", report_path.display()))
}
```

Ensure `PathBuf` is imported if not already available:

```rust
use std::path::{Path, PathBuf};
```

- [ ] **Step 4: Write report on success**

Before returning the success JSON from `dump_import`, build and write:

```rust
let report = json!({
    "success": true,
    "mode": mode,
    "tables": table_total,
    "rows_imported": rows_imported,
    "chunks_imported": chunks_imported,
    "verification": {
        "row_counts": "passed",
        "strict_manifest": strict_manifest,
        "warnings": manifest_warnings
    }
});
write_dump_import_report(input_path, &report)?;
```

Include `"import_report": dump_import_report_path(input_path)?.display().to_string()` in the
result payload.

- [ ] **Step 5: Run focused test**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_report_path_lives_inside_dump_directory --lib
```

Expected: PASS.

- [ ] **Step 6: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: write dump import verification report"
```

---

### Task 11: Classify The Provided PROD Dump Before Using It

**Files:**
- No source edits required unless classification exposes a missing helper.
- Read-only target: `C:\Users\QESG\Desktop\PROD_root_dataflare_20260601_120617`

- [ ] **Step 1: Inspect manifest metadata**

Run:

```powershell
$dump = 'C:\Users\QESG\Desktop\PROD_root_dataflare_20260601_120617'
Get-Content "$dump\_tunnelforge_dump.json" -TotalCount 80
```

Expected: Manifest is readable.

- [ ] **Step 2: Check for required metadata**

Run:

```powershell
$dump = 'C:\Users\QESG\Desktop\PROD_root_dataflare_20260601_120617'
$manifest = Get-Content "$dump\_tunnelforge_dump.json" -Raw | ConvertFrom-Json
$tables = @($manifest.tables)
$missingChecksums = @($tables | Where-Object { -not $_.chunk_sha256 -or $_.chunk_sha256.PSObject.Properties.Count -eq 0 })
[PSCustomObject]@{
  FormatVersion = $manifest.format_version
  DataFormat = $manifest.data_format
  Compression = $manifest.compression
  SnapshotPolicy = $manifest.snapshot_policy
  StrictExport = $manifest.strict_export
  TableCount = $tables.Count
  MissingChecksumTables = $missingChecksums.Count
}
```

Expected for the current reported dump: likely missing strict metadata. Record
the exact output for the final report.

- [ ] **Step 3: Do not import this dump as strict if metadata is incomplete**

If `MissingChecksumTables` is nonzero or `SnapshotPolicy` is empty/unknown, do
not run production import as strict. Record:

```text
Provided PROD dump is legacy/incomplete for strict restore. Standard recovery path is to re-export with the fixed core, then import the new artifact.
```

- [ ] **Step 4: Commit only if a source helper was needed**

If no source edits were needed, do not commit.

---

### Task 12: Full Verification Matrix

**Files:**
- Source files modified by previous tasks.

- [ ] **Step 1: Run Rust unit and integration tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml
```

Expected: PASS.

- [ ] **Step 2: Run Rust release build**

Run:

```powershell
cargo build --manifest-path migration_core\Cargo.toml --release
```

Expected: PASS and `migration_core\target\release\tunnelforge-core.exe` exists.

- [ ] **Step 3: Run Python tests**

Run:

```powershell
pytest
```

Expected: PASS. If environment-specific UI tests fail, record exact failing
tests and reason. Do not claim Python verification passed unless it did.

- [ ] **Step 4: Smoke-test Rust core capability**

Run:

```powershell
$req = '{"command":"service.hello","request_id":"verify-hello","payload":{}}'
$req | migration_core\target\release\tunnelforge-core.exe
```

Expected output contains:

```json
"command":"service.hello"
"success":true
"dump.import"
```

- [ ] **Step 5: Run diff hygiene**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors. Status should show only intended files.

---

### Task 13: Update Final HTML Remediation Report

**Files:**
- Modify: `reports/export_import_flow_review_20260601.html`

- [ ] **Step 1: Update report sections**

Edit the HTML report to include:

```html
<h2>Remediation Summary</h2>
<ul>
  <li>Full replacement is documented as direct replacement, not an atomic shadow switch.</li>
  <li>Strict import rejects incomplete dump manifests before target mutation.</li>
  <li>Merge import no longer blindly reapplies post-load DDL.</li>
  <li>Import success is gated by verification and emits an import report.</li>
  <li>Python forwards import intent fields such as timezone SQL to Rust.</li>
  <li>UI wording no longer promises unsupported object restoration.</li>
</ul>
```

Add a verification table:

```html
<h2>Verification Evidence</h2>
<table>
  <tr><th>Command</th><th>Result</th><th>Notes</th></tr>
  <tr><td>cargo test --manifest-path migration_core\Cargo.toml</td><td>PASS</td><td>Rust core tests</td></tr>
  <tr><td>cargo build --manifest-path migration_core\Cargo.toml --release</td><td>PASS</td><td>Release core built</td></tr>
  <tr><td>pytest</td><td>PASS</td><td>Python/UI tests</td></tr>
</table>
```

If any command did not pass, replace `PASS` with the exact result and explain it.

- [ ] **Step 2: Verify report file exists**

Run:

```powershell
Get-Item reports\export_import_flow_review_20260601.html | Select-Object FullName,Length,LastWriteTime
```

Expected: file exists and length is greater than before the update.

- [ ] **Step 3: Commit report**

```powershell
git add reports\export_import_flow_review_20260601.html
git commit -m "docs: update export import remediation report"
```

---

## Final Completion Audit

Before marking the goal complete, verify each requirement from
`docs/superpowers/specs/2026-06-01-export-import-recovery-design.md`.

Run:

```powershell
git status --short
git log --oneline -10
cargo test --manifest-path migration_core\Cargo.toml
cargo build --manifest-path migration_core\Cargo.toml --release
pytest
git diff --check
```

Completion can be claimed only if:

- `ERROR 3780` class mismatch is covered by schema fidelity or import-plan validation tests.
- Shadow full replacement is not claimed by docs or UI; direct replacement is the documented supported mode.
- Import success is emitted after verification.
- Strict import rejects incomplete manifests or routes them to a limited legacy path.
- UI wording matches actual object support.
- Final report is available at:

```text
file:///C:/Users/QESG/sh-project/tunnelforge/reports/export_import_flow_review_20260601.html
```
