# MySQL Shell Grade Export Import Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bring TunnelForge Export/Import to a MySQL Shell grade trust model that can honestly classify dumps, prevent unsafe imports, preserve MySQL metadata and objects, resume interrupted imports, and verify the final result with evidence.

**Architecture:** Rust core remains the owner of DB semantics, artifact contracts, state files, verification, and reports. Python/PyQt only forwards user intent and renders Rust-classified compatibility, progress, and verdicts. The work is phased so every commit leaves the product more accurate even before the full mysqlsh-grade target is complete.

**Tech Stack:** Rust `migration_core/src/lib.rs`, Python `src/exporters/rust_dump_exporter.py`, PyQt `src/ui/dialogs/db_dialogs.py`, translations in `src/core/i18n.py`, Rust tests through Cargo, pytest tests under `tests/`.

---

## Scope And Execution Rules

This plan implements `docs/superpowers/specs/2026-06-01-mysqlsh-grade-export-import-design.md`.

The accepted reference point is MySQL Shell reliability behavior, not mysqlsh file-format compatibility. The target behavior is evidence, resumability, guardrails, and accurate user-facing verdicts.

The work is one program with six phases:

1. Honest grading and UI.
2. Strict export consistency evidence.
3. Schema and object fidelity.
4. Persistent import state and resume/reset.
5. Strong verification.
6. MySQL Shell parity reporting.

Before every task, run:

```powershell
git status --short
git branch --show-current
```

Expected:

- Branch is `recovery/export-import-root-cause` or another explicit feature branch.
- Worktree is clean unless the previous task is intentionally in progress.

Do not add Python DB-driver dump/import paths. Rust core owns all database semantics.

---

## File Map

- Modify `migration_core/src/lib.rs`
  - Manifest model, grading, preflight gates, snapshot evidence, schema metadata, object metadata, object restore order, progress state, verification, report payload, and Rust tests.
- Modify `src/exporters/rust_dump_exporter.py`
  - Read manifest compatibility metadata, forward import policy, expose grading/report fields, and reject unsafe UI-driven imports before mutation.
- Modify `src/ui/dialogs/db_dialogs.py`
  - Show strict/limited/not-restorable compatibility, session policy, progress-state actions, and final verdicts.
- Modify `src/core/i18n.py`
  - Add Korean and English strings for the new operator-facing states.
- Modify `tests/test_rust_dump_exporter.py`
  - Wrapper payload, manifest metadata, strict gate, and report propagation tests.
- Modify `tests/test_db_dialogs.py`
  - Dialog wording and disabled-action tests for limited and blocked dumps.
- Add `tests/fixtures/mysqlsh_grade/`
  - Golden manifest JSON files for strict, limited legacy, unsupported-feature, checksum-missing, checksum-mismatch, object-scope, and resumable import cases.
- Modify `reports/export_import_flow_review_20260601.html`
  - Add final MySQL Shell parity matrix and implementation verdict.

---

### Task 1: Add Dump Artifact Grading Model

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for strict, limited, and blocked grading**

Add these tests inside the existing `#[cfg(test)] mod tests`:

```rust
#[test]
fn artifact_grading_requires_snapshot_for_strict_restore() {
    let manifest = mysql_grade_manifest("not_enforced", true, Vec::new());

    let grade = grade_dump_artifact(&manifest);

    assert_eq!(grade.restorability, RestorabilityGrade::LimitedRestorable);
    assert!(grade
        .warnings
        .contains(&"snapshot consistency is not proven".to_string()));
    assert!(grade.blockers.is_empty());
}

#[test]
fn artifact_grading_blocks_unsupported_features() {
    let manifest = mysql_grade_manifest(
        "transaction_snapshot",
        true,
        vec!["mysql.unknown_feature".to_string()],
    );

    let grade = grade_dump_artifact(&manifest);

    assert_eq!(grade.restorability, RestorabilityGrade::NotRestorable);
    assert!(grade
        .blockers
        .contains(&"unsupported feature mysql.unknown_feature".to_string()));
}

#[test]
fn artifact_grading_allows_strict_when_required_evidence_exists() {
    let manifest = mysql_grade_manifest("transaction_snapshot", true, Vec::new());

    let grade = grade_dump_artifact(&manifest);

    assert_eq!(grade.restorability, RestorabilityGrade::StrictRestorable);
    assert!(grade.warnings.is_empty());
    assert!(grade.blockers.is_empty());
}
```

- [ ] **Step 2: Run focused tests and confirm they fail**

```powershell
cargo test --manifest-path migration_core\Cargo.toml artifact_grading --lib
```

Expected: FAIL with missing `RestorabilityGrade`, `grade_dump_artifact`, or `mysql_grade_manifest`.

- [ ] **Step 3: Add grading structs near `DumpManifest`**

```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum RestorabilityGrade {
    StrictRestorable,
    LimitedRestorable,
    NotRestorable,
}

fn default_restorability_grade() -> RestorabilityGrade {
    RestorabilityGrade::LimitedRestorable
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct DumpFeatureSet {
    #[serde(default)]
    pub snapshot: bool,
    #[serde(default)]
    pub chunking: bool,
    #[serde(default)]
    pub partitioning: bool,
    #[serde(default)]
    pub routines: bool,
    #[serde(default)]
    pub events: bool,
    #[serde(default)]
    pub triggers: bool,
    #[serde(default)]
    pub users: bool,
    #[serde(default)]
    pub grants: bool,
    #[serde(default)]
    pub checksum: bool,
    #[serde(default)]
    pub row_digest: bool,
    #[serde(default)]
    pub timezone: bool,
    #[serde(default)]
    pub unsupported: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ArtifactGrade {
    pub restorability: RestorabilityGrade,
    pub warnings: Vec<String>,
    pub blockers: Vec<String>,
}
```

- [ ] **Step 4: Extend `DumpManifest` with default-safe fields**

Add fields to `DumpManifest`:

```rust
#[serde(default)]
pub source_version: Option<String>,
#[serde(default = "default_dump_scope")]
pub dump_scope: String,
#[serde(default)]
pub features: DumpFeatureSet,
#[serde(default = "default_restorability_grade")]
pub restorability: RestorabilityGrade,
#[serde(default)]
pub blockers: Vec<String>,
```

Add helper:

```rust
fn default_dump_scope() -> String {
    "schema".to_string()
}
```

- [ ] **Step 5: Add the grading function**

```rust
fn grade_dump_artifact(manifest: &DumpManifest) -> ArtifactGrade {
    let mut warnings = manifest.manifest_warnings.clone();
    let mut blockers = manifest.blockers.clone();

    for feature in &manifest.features.unsupported {
        blockers.push(format!("unsupported feature {feature}"));
    }
    if manifest.snapshot_policy == "not_enforced" || manifest.snapshot_policy == "unknown" {
        warnings.push("snapshot consistency is not proven".to_string());
    }
    if !manifest.features.checksum {
        warnings.push("checksum coverage is incomplete".to_string());
    }

    warnings.sort();
    warnings.dedup();
    blockers.sort();
    blockers.dedup();

    let restorability = if !blockers.is_empty() {
        RestorabilityGrade::NotRestorable
    } else if warnings.is_empty()
        && manifest.features.snapshot
        && manifest.features.checksum
        && manifest.snapshot_policy != "not_enforced"
        && manifest.snapshot_policy != "unknown"
    {
        RestorabilityGrade::StrictRestorable
    } else {
        RestorabilityGrade::LimitedRestorable
    };

    ArtifactGrade {
        restorability,
        warnings,
        blockers,
    }
}
```

- [ ] **Step 6: Add the test manifest helper**

Inside `mod tests`, add:

```rust
fn mysql_grade_manifest(
    snapshot_policy: &str,
    checksum: bool,
    unsupported: Vec<String>,
) -> DumpManifest {
    DumpManifest {
        format: "tunnelforge-dump".to_string(),
        format_version: 3,
        data_format: "tsv".to_string(),
        compression: "zstd".to_string(),
        source_engine: "mysql".to_string(),
        source_version: Some("8.0.36".to_string()),
        database: "app".to_string(),
        schema: NormalizedSchema::default(),
        chunk_size: 1000,
        created_unix_seconds: 1,
        snapshot_policy: snapshot_policy.to_string(),
        strict_export: false,
        manifest_warnings: Vec::new(),
        dump_scope: "schema".to_string(),
        features: DumpFeatureSet {
            snapshot: snapshot_policy != "not_enforced",
            chunking: true,
            checksum,
            timezone: true,
            unsupported,
            ..DumpFeatureSet::default()
        },
        restorability: RestorabilityGrade::LimitedRestorable,
        blockers: Vec::new(),
        tables: Vec::new(),
    }
}
```

- [ ] **Step 7: Run focused tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml artifact_grading --lib
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: add dump artifact grading model"
```

---

### Task 2: Persist Grading In Exported Manifests

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing tests for manifest finalization**

Add:

```rust
#[test]
fn dump_manifest_finalizer_records_limited_grade_when_snapshot_is_missing() {
    let mut manifest = mysql_grade_manifest("not_enforced", true, Vec::new());

    finalize_dump_manifest_grade(&mut manifest);

    assert_eq!(manifest.restorability, RestorabilityGrade::LimitedRestorable);
    assert!(!manifest.strict_export);
    assert!(manifest
        .manifest_warnings
        .contains(&"snapshot consistency is not proven".to_string()));
}

#[test]
fn dump_manifest_finalizer_records_strict_grade_when_evidence_is_complete() {
    let mut manifest = mysql_grade_manifest("transaction_snapshot", true, Vec::new());

    finalize_dump_manifest_grade(&mut manifest);

    assert_eq!(manifest.restorability, RestorabilityGrade::StrictRestorable);
    assert!(manifest.strict_export);
    assert!(manifest.blockers.is_empty());
}
```

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_manifest_finalizer --lib
```

Expected: FAIL because `finalize_dump_manifest_grade` is missing.

- [ ] **Step 3: Implement finalizer**

```rust
fn finalize_dump_manifest_grade(manifest: &mut DumpManifest) {
    let grade = grade_dump_artifact(manifest);
    manifest.restorability = grade.restorability;
    manifest.manifest_warnings = grade.warnings;
    manifest.blockers = grade.blockers;
    manifest.strict_export = manifest.restorability == RestorabilityGrade::StrictRestorable;
}
```

- [ ] **Step 4: Wire finalizer into `dump_run_streaming`**

In the `DumpManifest { ... }` construction inside `dump_run_streaming`, make the value mutable and populate new fields:

```rust
let mut manifest = DumpManifest {
    format: "tunnelforge-dump".to_string(),
    format_version: 3,
    data_format: "tsv".to_string(),
    compression: compression.clone(),
    source_engine: source.engine.clone(),
    source_version: source_version.clone(),
    database: source.database.clone(),
    schema,
    chunk_size,
    created_unix_seconds,
    snapshot_policy,
    strict_export: false,
    manifest_warnings,
    dump_scope: "schema".to_string(),
    features: DumpFeatureSet {
        snapshot: snapshot_policy != "not_enforced",
        chunking: true,
        checksum: true,
        timezone: false,
        ..DumpFeatureSet::default()
    },
    restorability: RestorabilityGrade::LimitedRestorable,
    blockers: Vec::new(),
    tables: table_manifests,
};
finalize_dump_manifest_grade(&mut manifest);
```

Add `source_version` before manifest construction:

```rust
let source_version = detect_server_version(&mut conn).ok();
```

- [ ] **Step 5: Add server version helper**

```rust
fn detect_server_version(conn: &mut mysql::PooledConn) -> Result<String, String> {
    conn.query_first::<String, _>("SELECT VERSION()")
        .map_err(|err| format!("failed to inspect MySQL version: {err}"))?
        .ok_or_else(|| "MySQL version query returned no rows".to_string())
}
```

- [ ] **Step 6: Include grade in `dump.run` result JSON**

Add fields to the final `json!` result:

```rust
"restorability": manifest.restorability,
"warnings": manifest.manifest_warnings,
"blockers": manifest.blockers,
```

- [ ] **Step 7: Run focused and manifest roundtrip tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_manifest --lib
cargo test --manifest-path migration_core\Cargo.toml dump_run --lib
```

Expected: PASS. Existing legacy manifest tests must still deserialize through serde defaults.

- [ ] **Step 8: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: persist dump restorability grade"
```

---

### Task 3: Gate Import Before Target Mutation

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for import compatibility preflight**

Add:

```rust
#[test]
fn import_preflight_blocks_not_restorable_manifest() {
    let mut manifest = mysql_grade_manifest("transaction_snapshot", true, vec!["x".to_string()]);
    finalize_dump_manifest_grade(&mut manifest);

    let err = validate_dump_import_compatibility(&manifest, true).unwrap_err();

    assert!(err.contains("dump artifact is not restorable"));
    assert!(err.contains("unsupported feature x"));
}

#[test]
fn import_preflight_blocks_strict_import_for_limited_manifest() {
    let mut manifest = mysql_grade_manifest("not_enforced", true, Vec::new());
    finalize_dump_manifest_grade(&mut manifest);

    let err = validate_dump_import_compatibility(&manifest, true).unwrap_err();

    assert!(err.contains("strict import requires a strict restorable dump"));
}

#[test]
fn import_preflight_allows_limited_manifest_when_strict_is_false() {
    let mut manifest = mysql_grade_manifest("not_enforced", true, Vec::new());
    finalize_dump_manifest_grade(&mut manifest);

    validate_dump_import_compatibility(&manifest, false).unwrap();
}
```

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_preflight --lib
```

Expected: FAIL because `validate_dump_import_compatibility` is missing.

- [ ] **Step 3: Implement compatibility validation**

```rust
fn validate_dump_import_compatibility(
    manifest: &DumpManifest,
    strict_import: bool,
) -> Result<(), String> {
    let grade = grade_dump_artifact(manifest);
    if grade.restorability == RestorabilityGrade::NotRestorable {
        return Err(format!(
            "dump artifact is not restorable: {}",
            grade.blockers.join("; ")
        ));
    }
    if strict_import && grade.restorability != RestorabilityGrade::StrictRestorable {
        return Err(format!(
            "strict import requires a strict restorable dump: {}",
            grade.warnings.join("; ")
        ));
    }
    Ok(())
}
```

- [ ] **Step 4: Call validation in `dump_import_streaming` before target preparation**

Immediately after reading manifest and `strict_manifest`:

```rust
validate_dump_import_compatibility(&manifest, strict_manifest)?;
```

This must happen before any call that creates, drops, truncates, switches, or loads target objects.

- [ ] **Step 5: Emit compatibility event before blocking return**

Wrap the validation call:

```rust
let grade = grade_dump_artifact(&manifest);
emit(&json!({
    "event": "progress",
    "phase": "dump_import_preflight",
    "message": "dump compatibility preflight completed",
    "restorability": grade.restorability,
    "warnings": grade.warnings,
    "blockers": grade.blockers
}))?;
validate_dump_import_compatibility(&manifest, strict_manifest)?;
```

- [ ] **Step 6: Run import-focused tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_preflight --lib
cargo test --manifest-path migration_core\Cargo.toml strict_manifest --lib
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "fix: gate dump import by artifact grade"
```

---

### Task 4: Surface Compatibility In Python And UI

**Files:**
- Modify: `src/exporters/rust_dump_exporter.py`
- Modify: `src/ui/dialogs/db_dialogs.py`
- Modify: `src/core/i18n.py`
- Modify: `tests/test_rust_dump_exporter.py`
- Modify: `tests/test_db_dialogs.py`

- [ ] **Step 1: Add failing wrapper tests for manifest metadata**

In `tests/test_rust_dump_exporter.py`, add:

```python
def test_import_metadata_exposes_restorability(tmp_path):
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps(
            {
                "format": "tunnelforge-dump",
                "format_version": 3,
                "source_engine": "mysql",
                "database": "app",
                "restorability": "limited_restorable",
                "manifest_warnings": ["snapshot consistency is not proven"],
                "blockers": [],
                "tables": [],
            }
        ),
        encoding="utf-8",
    )

    exporter = RustDumpExporter(core_service=FakeDumpCoreService())
    metadata = exporter.analyze_import_metadata(str(dump_dir))[2]

    assert metadata["restorability"] == "limited_restorable"
    assert metadata["warnings"] == ["snapshot consistency is not proven"]
    assert metadata["blockers"] == []
```

- [ ] **Step 2: Add failing dialog test for strict action gating**

In `tests/test_db_dialogs.py`, add:

```python
def test_import_dialog_disables_recommended_import_for_limited_dump(qtbot, monkeypatch):
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.check_rust_dump", lambda: (True, "installed"))
    dialog = RustDumpImportDialog(connector=None)
    qtbot.addWidget(dialog)
    dialog._set_dump_compatibility(
        {
            "restorability": "limited_restorable",
            "warnings": ["snapshot consistency is not proven"],
            "blockers": [],
        }
    )

    assert not dialog.btn_import.isEnabled()
    assert "제한적 복원" in dialog.lbl_dump_compatibility.text()
```

- [ ] **Step 3: Run tests and confirm failure**

```powershell
pytest tests/test_rust_dump_exporter.py::test_import_metadata_exposes_restorability -q
pytest tests/test_db_dialogs.py::test_import_dialog_disables_recommended_import_for_limited_dump -q
```

Expected: FAIL because metadata fields or `_set_dump_compatibility` are missing.

- [ ] **Step 4: Return compatibility fields from `analyze_import_metadata`**

In `src/exporters/rust_dump_exporter.py`, extend the metadata dict:

```python
"restorability": str(manifest.get("restorability") or "limited_restorable"),
"warnings": list(manifest.get("manifest_warnings") or manifest.get("warnings") or []),
"blockers": list(manifest.get("blockers") or []),
"snapshot_policy": str(manifest.get("snapshot_policy") or "unknown"),
"features": dict(manifest.get("features") or {}),
```

- [ ] **Step 5: Add dialog compatibility renderer**

In `src/ui/dialogs/db_dialogs.py`, add a method on `RustDumpImportDialog`:

```python
def _set_dump_compatibility(self, metadata: dict) -> None:
    grade = str(metadata.get("restorability") or "limited_restorable")
    warnings = list(metadata.get("warnings") or [])
    blockers = list(metadata.get("blockers") or [])

    if grade == "strict_restorable":
        text = self.tr("Strict restorable dump")
        enable_recommended = True
    elif grade == "not_restorable":
        text = self.tr("복원 불가 Dump")
        enable_recommended = False
    else:
        text = self.tr("제한적 복원 Dump")
        enable_recommended = False

    details = blockers or warnings
    if details:
        text = f"{text}: {'; '.join(details)}"

    self.lbl_dump_compatibility.setText(text)
    self.btn_import.setEnabled(enable_recommended and self.rust_dump_installed)
```

- [ ] **Step 6: Add missing widgets only if the dialog lacks them**

If the dialog does not have `lbl_dump_compatibility`, add it next to the existing import metadata summary:

```python
self.lbl_dump_compatibility = QLabel(self.tr("Dump compatibility is not checked"))
self.lbl_dump_compatibility.setWordWrap(True)
layout.addWidget(self.lbl_dump_compatibility)
```

- [ ] **Step 7: Add translations**

In `src/core/i18n.py`, add exact strings:

```python
"Strict restorable dump": "엄격 복원 가능 Dump",
"Dump compatibility is not checked": "Dump 호환성을 아직 확인하지 않았습니다",
```

- [ ] **Step 8: Run focused tests**

```powershell
pytest tests/test_rust_dump_exporter.py::test_import_metadata_exposes_restorability -q
pytest tests/test_db_dialogs.py::test_import_dialog_disables_recommended_import_for_limited_dump -q
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add src\exporters\rust_dump_exporter.py src\ui\dialogs\db_dialogs.py src\core\i18n.py tests\test_rust_dump_exporter.py tests\test_db_dialogs.py
git commit -m "feat: surface dump compatibility grade in import UI"
```

---

### Task 5: Add Truthful Snapshot Evidence For Export

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for snapshot evidence classification**

Add:

```rust
#[test]
fn mysql_snapshot_evidence_marks_transaction_snapshot_strict_for_innodb_only() {
    let tables = vec![MysqlTableEngine {
        table: "orders".to_string(),
        engine: "InnoDB".to_string(),
    }];

    let evidence = classify_mysql_snapshot_strategy(&tables, true, false, 1);

    assert_eq!(evidence.policy, "transaction_snapshot");
    assert!(evidence.strict_candidate);
    assert!(evidence.warnings.is_empty());
}

#[test]
fn mysql_snapshot_evidence_marks_mixed_engines_limited_without_locks() {
    let tables = vec![
        MysqlTableEngine {
            table: "orders".to_string(),
            engine: "InnoDB".to_string(),
        },
        MysqlTableEngine {
            table: "audit_log".to_string(),
            engine: "MyISAM".to_string(),
        },
    ];

    let evidence = classify_mysql_snapshot_strategy(&tables, true, false, 8);

    assert_eq!(evidence.policy, "not_enforced");
    assert!(!evidence.strict_candidate);
    assert!(evidence
        .warnings
        .contains(&"non-transactional tables require lock-based export".to_string()));
}
```

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml mysql_snapshot_evidence --lib
```

Expected: FAIL because snapshot evidence types are missing.

- [ ] **Step 3: Add evidence types and classifier**

```rust
#[derive(Debug, Clone, PartialEq, Eq)]
struct MysqlTableEngine {
    table: String,
    engine: String,
}

#[derive(Debug, Clone, PartialEq, Eq)]
struct MysqlSnapshotEvidence {
    policy: String,
    strict_candidate: bool,
    warnings: Vec<String>,
}

fn classify_mysql_snapshot_strategy(
    tables: &[MysqlTableEngine],
    transaction_snapshot_available: bool,
    lock_based_snapshot_available: bool,
    dump_threads: usize,
) -> MysqlSnapshotEvidence {
    let has_non_transactional = tables
        .iter()
        .any(|table| !table.engine.eq_ignore_ascii_case("InnoDB"));

    if has_non_transactional && !lock_based_snapshot_available {
        return MysqlSnapshotEvidence {
            policy: "not_enforced".to_string(),
            strict_candidate: false,
            warnings: vec!["non-transactional tables require lock-based export".to_string()],
        };
    }
    if lock_based_snapshot_available {
        return MysqlSnapshotEvidence {
            policy: "lock_based".to_string(),
            strict_candidate: true,
            warnings: Vec::new(),
        };
    }
    if dump_threads > 1 {
        return MysqlSnapshotEvidence {
            policy: "not_enforced".to_string(),
            strict_candidate: false,
            warnings: vec![
                "parallel dump connections cannot share a transaction snapshot".to_string(),
            ],
        };
    }
    if transaction_snapshot_available {
        return MysqlSnapshotEvidence {
            policy: "transaction_snapshot".to_string(),
            strict_candidate: true,
            warnings: Vec::new(),
        };
    }
    MysqlSnapshotEvidence {
        policy: "not_enforced".to_string(),
        strict_candidate: false,
        warnings: vec!["snapshot consistency is not proven".to_string()],
    }
}
```

- [ ] **Step 4: Inspect table engines before dump**

Add:

```rust
fn inspect_mysql_table_engines(
    conn: &mut mysql::PooledConn,
    database: &str,
) -> Result<Vec<MysqlTableEngine>, String> {
    conn.exec_map(
        r#"
        SELECT TABLE_NAME, COALESCE(ENGINE, '')
        FROM information_schema.TABLES
        WHERE TABLE_SCHEMA = ?
          AND TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_NAME
        "#,
        (database,),
        |(table, engine): (String, String)| MysqlTableEngine { table, engine },
    )
    .map_err(|err| format!("failed to inspect MySQL table engines: {err}"))
}
```

- [ ] **Step 5: Begin transaction snapshot for strict candidate**

When `snapshot_evidence.policy == "transaction_snapshot"`, the dump must use a single connection for all table reads. Before data dumping on that connection, call:

```rust
conn.query_drop("SET SESSION TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    .map_err(|err| format!("failed to set export isolation level: {err}"))?;
conn.query_drop("START TRANSACTION WITH CONSISTENT SNAPSHOT")
    .map_err(|err| format!("failed to start consistent export snapshot: {err}"))?;
```

After manifest write and before returning result on that same connection:

```rust
let _ = conn.query_drop("COMMIT");
```

- [ ] **Step 6: Use evidence in manifest fields**

```rust
let table_engines = inspect_mysql_table_engines(&mut conn, &source.database)?;
let snapshot_evidence = classify_mysql_snapshot_strategy(&table_engines, true, false, threads);
let snapshot_policy = snapshot_evidence.policy.clone();
let manifest_warnings = snapshot_evidence.warnings.clone();
```

If `snapshot_evidence.policy == "transaction_snapshot"`, set the effective dump thread count to 1 before table dumping:

```rust
let effective_threads = if snapshot_evidence.policy == "transaction_snapshot" {
    1
} else {
    threads
};
```

Pass `effective_threads` to the table dump planner instead of the user-requested `threads`.

Set:

```rust
features: DumpFeatureSet {
    snapshot: snapshot_evidence.strict_candidate,
    chunking: true,
    checksum: true,
    timezone: false,
    ..DumpFeatureSet::default()
},
```

- [ ] **Step 7: Run tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml mysql_snapshot_evidence --lib
cargo test --manifest-path migration_core\Cargo.toml dump_manifest --lib
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: record mysql export snapshot evidence"
```

---

### Task 6: Preserve MySQL Table Metadata

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing Rust tests for table metadata serialization**

Add:

```rust
#[test]
fn normalized_table_preserves_mysql_table_options() {
    let table = NormalizedTable {
        name: "orders".to_string(),
        engine: Some("InnoDB".to_string()),
        default_charset: Some("utf8mb4".to_string()),
        default_collation: Some("utf8mb4_0900_ai_ci".to_string()),
        row_format: Some("Dynamic".to_string()),
        partitions: vec![NormalizedPartition {
            name: "p2026".to_string(),
            method: Some("RANGE".to_string()),
            expression: Some("YEAR(created_at)".to_string()),
        }],
        columns: Vec::new(),
        indexes: Vec::new(),
        foreign_keys: Vec::new(),
    };

    let json = serde_json::to_string(&table).unwrap();
    assert!(json.contains("\"engine\":\"InnoDB\""));
    assert!(json.contains("\"default_charset\":\"utf8mb4\""));
    assert!(json.contains("\"partitions\""));
}
```

- [ ] **Step 2: Run focused test and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml normalized_table_preserves_mysql_table_options --lib
```

Expected: FAIL because the metadata fields are missing.

- [ ] **Step 3: Extend table structs**

```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct NormalizedPartition {
    pub name: String,
    #[serde(default)]
    pub method: Option<String>,
    #[serde(default)]
    pub expression: Option<String>,
}
```

Add to `NormalizedTable`:

```rust
#[serde(default)]
pub engine: Option<String>,
#[serde(default)]
pub default_charset: Option<String>,
#[serde(default)]
pub default_collation: Option<String>,
#[serde(default)]
pub row_format: Option<String>,
#[serde(default)]
pub partitions: Vec<NormalizedPartition>,
```

- [ ] **Step 4: Populate table metadata in MySQL inspection**

Where MySQL schema inspection creates `NormalizedTable`, query:

```rust
SELECT
    TABLE_NAME,
    ENGINE,
    TABLE_COLLATION,
    ROW_FORMAT
FROM information_schema.TABLES
WHERE TABLE_SCHEMA = ?
  AND TABLE_TYPE = 'BASE TABLE'
ORDER BY TABLE_NAME
```

Resolve charset from collation:

```rust
fn mysql_charset_from_collation(collation: &Option<String>) -> Option<String> {
    collation
        .as_ref()
        .and_then(|value| value.split('_').next())
        .filter(|value| !value.is_empty())
        .map(str::to_string)
}
```

Assign:

```rust
engine,
default_charset: mysql_charset_from_collation(&table_collation),
default_collation: table_collation,
row_format,
partitions: inspect_mysql_partitions(conn, database, &table_name)?,
```

- [ ] **Step 5: Add partition inspector**

```rust
fn inspect_mysql_partitions(
    conn: &mut mysql::PooledConn,
    database: &str,
    table: &str,
) -> Result<Vec<NormalizedPartition>, String> {
    conn.exec_map(
        r#"
        SELECT
            PARTITION_NAME,
            PARTITION_METHOD,
            PARTITION_EXPRESSION
        FROM information_schema.PARTITIONS
        WHERE TABLE_SCHEMA = ?
          AND TABLE_NAME = ?
          AND PARTITION_NAME IS NOT NULL
        ORDER BY PARTITION_ORDINAL_POSITION
        "#,
        (database, table),
        |(name, method, expression): (String, Option<String>, Option<String>)| {
            NormalizedPartition {
                name,
                method,
                expression,
            }
        },
    )
    .map_err(|err| format!("failed to inspect MySQL partitions for {table}: {err}"))
}
```

- [ ] **Step 6: Use metadata when generating MySQL create table SQL**

When building `CREATE TABLE`, append options:

```rust
if let Some(engine) = &table.engine {
    sql.push_str(&format!(" ENGINE={}", engine));
}
if let Some(charset) = &table.default_charset {
    sql.push_str(&format!(" DEFAULT CHARSET={}", charset));
}
if let Some(collation) = &table.default_collation {
    sql.push_str(&format!(" COLLATE={}", collation));
}
if let Some(row_format) = &table.row_format {
    sql.push_str(&format!(" ROW_FORMAT={}", row_format));
}
```

Do not quote charset, collation, or row format with string quotes.

- [ ] **Step 7: Run tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml normalized_table_preserves_mysql_table_options --lib
cargo test --manifest-path migration_core\Cargo.toml create_table --lib
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: preserve mysql table options in dumps"
```

---

### Task 7: Capture And Restore MySQL Objects

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing tests for object metadata**

Add:

```rust
#[test]
fn dump_object_manifest_serializes_view_and_trigger_entries() {
    let objects = DumpObjectManifest {
        objects: vec![
            DumpObjectEntry {
                object_type: "view".to_string(),
                schema: "app".to_string(),
                name: "v_orders".to_string(),
                path: "objects/views/v_orders.sql".to_string(),
                restore_order: 10,
            },
            DumpObjectEntry {
                object_type: "trigger".to_string(),
                schema: "app".to_string(),
                name: "trg_orders_ai".to_string(),
                path: "objects/triggers/trg_orders_ai.sql".to_string(),
                restore_order: 40,
            },
        ],
    };

    let json = serde_json::to_string(&objects).unwrap();
    assert!(json.contains("\"object_type\":\"view\""));
    assert!(json.contains("\"restore_order\":40"));
}
```

- [ ] **Step 2: Run focused test and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_object_manifest --lib
```

Expected: FAIL because object manifest types are missing.

- [ ] **Step 3: Add object manifest types**

```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq, Default)]
pub struct DumpObjectManifest {
    #[serde(default)]
    pub objects: Vec<DumpObjectEntry>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct DumpObjectEntry {
    pub object_type: String,
    pub schema: String,
    pub name: String,
    pub path: String,
    pub restore_order: u32,
}
```

Add to `DumpManifest`:

```rust
#[serde(default)]
pub objects: DumpObjectManifest,
```

- [ ] **Step 4: Capture object DDL during export**

Add:

```rust
fn capture_mysql_objects(
    conn: &mut mysql::PooledConn,
    database: &str,
    output_path: &Path,
) -> Result<DumpObjectManifest, String> {
    let mut objects = DumpObjectManifest::default();
    fs::create_dir_all(output_path.join("objects/views"))
        .map_err(|err| format!("failed to create view object directory: {err}"))?;
    fs::create_dir_all(output_path.join("objects/triggers"))
        .map_err(|err| format!("failed to create trigger object directory: {err}"))?;
    fs::create_dir_all(output_path.join("objects/routines"))
        .map_err(|err| format!("failed to create routine object directory: {err}"))?;
    fs::create_dir_all(output_path.join("objects/events"))
        .map_err(|err| format!("failed to create event object directory: {err}"))?;

    let views: Vec<String> = conn
        .exec_map(
            "SELECT TABLE_NAME FROM information_schema.VIEWS WHERE TABLE_SCHEMA = ? ORDER BY TABLE_NAME",
            (database,),
            |name: String| name,
        )
        .map_err(|err| format!("failed to inspect MySQL views: {err}"))?;
    for view in views {
        let ddl = show_create_object(conn, "VIEW", database, &view)?;
        let path = format!("objects/views/{view}.sql");
        fs::write(output_path.join(&path), ddl)
            .map_err(|err| format!("failed to write view DDL for {view}: {err}"))?;
        objects.objects.push(DumpObjectEntry {
            object_type: "view".to_string(),
            schema: database.to_string(),
            name: view,
            path,
            restore_order: 10,
        });
    }
    let triggers: Vec<String> = conn
        .exec_map(
            "SELECT TRIGGER_NAME FROM information_schema.TRIGGERS WHERE TRIGGER_SCHEMA = ? ORDER BY TRIGGER_NAME",
            (database,),
            |name: String| name,
        )
        .map_err(|err| format!("failed to inspect MySQL triggers: {err}"))?;
    for trigger in triggers {
        let ddl = show_create_object(conn, "TRIGGER", database, &trigger)?;
        let path = format!("objects/triggers/{trigger}.sql");
        fs::write(output_path.join(&path), ddl)
            .map_err(|err| format!("failed to write trigger DDL for {trigger}: {err}"))?;
        objects.objects.push(DumpObjectEntry {
            object_type: "trigger".to_string(),
            schema: database.to_string(),
            name: trigger,
            path,
            restore_order: 40,
        });
    }
    let routines: Vec<(String, String)> = conn
        .exec_map(
            "SELECT ROUTINE_NAME, ROUTINE_TYPE FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = ? ORDER BY ROUTINE_TYPE, ROUTINE_NAME",
            (database,),
            |(name, routine_type): (String, String)| (name, routine_type),
        )
        .map_err(|err| format!("failed to inspect MySQL routines: {err}"))?;
    for (routine, routine_type) in routines {
        let ddl = show_create_object(conn, &routine_type, database, &routine)?;
        let path = format!("objects/routines/{routine_type}_{routine}.sql");
        fs::write(output_path.join(&path), ddl)
            .map_err(|err| format!("failed to write routine DDL for {routine}: {err}"))?;
        objects.objects.push(DumpObjectEntry {
            object_type: "routine".to_string(),
            schema: database.to_string(),
            name: routine,
            path,
            restore_order: 30,
        });
    }
    let events: Vec<String> = conn
        .exec_map(
            "SELECT EVENT_NAME FROM information_schema.EVENTS WHERE EVENT_SCHEMA = ? ORDER BY EVENT_NAME",
            (database,),
            |name: String| name,
        )
        .map_err(|err| format!("failed to inspect MySQL events: {err}"))?;
    for event in events {
        let ddl = show_create_object(conn, "EVENT", database, &event)?;
        let path = format!("objects/events/{event}.sql");
        fs::write(output_path.join(&path), ddl)
            .map_err(|err| format!("failed to write event DDL for {event}: {err}"))?;
        objects.objects.push(DumpObjectEntry {
            object_type: "event".to_string(),
            schema: database.to_string(),
            name: event,
            path,
            restore_order: 20,
        });
    }
    Ok(objects)
}
```

Add helper:

```rust
fn show_create_object(
    conn: &mut mysql::PooledConn,
    object_type: &str,
    database: &str,
    name: &str,
) -> Result<String, String> {
    let sql = format!(
        "SHOW CREATE {} {}.{}",
        object_type,
        quote_mysql_identifier(database),
        quote_mysql_identifier(name)
    );
    let row: Option<mysql::Row> = conn
        .query_first(sql)
        .map_err(|err| format!("failed to read DDL for {object_type} {name}: {err}"))?;
    let row = row.ok_or_else(|| format!("SHOW CREATE returned no row for {object_type} {name}"))?;
    row.get::<String, _>(1)
        .ok_or_else(|| format!("SHOW CREATE returned no DDL for {object_type} {name}"))
}
```

- [ ] **Step 5: Wire object capture into manifest**

In `dump_run_streaming`, call:

```rust
let objects = capture_mysql_objects(&mut conn, &source.database, output_path)?;
```

Set features:

```rust
features.routines = objects.objects.iter().any(|item| item.object_type == "routine");
features.events = objects.objects.iter().any(|item| item.object_type == "event");
features.triggers = objects.objects.iter().any(|item| item.object_type == "trigger");
```

Set manifest field:

```rust
objects,
```

- [ ] **Step 6: Restore object DDL after data load**

Add:

```rust
fn restore_dump_objects(
    conn: &mut mysql::PooledConn,
    input_path: &Path,
    objects: &DumpObjectManifest,
) -> Result<u64, String> {
    let mut entries = objects.objects.clone();
    entries.sort_by_key(|entry| entry.restore_order);
    let mut restored = 0;
    for entry in entries {
        let ddl_path = safe_dump_relative_path(input_path, &entry.path)?;
        let ddl = fs::read_to_string(&ddl_path)
            .map_err(|err| format!("failed to read object DDL {}: {err}", entry.path))?;
        conn.query_drop(ddl)
            .map_err(|err| format!("failed to restore {} {}: {err}", entry.object_type, entry.name))?;
        restored += 1;
    }
    Ok(restored)
}
```

Call it in `dump_import_streaming` after table data and before final verification:

```rust
let objects_restored = restore_dump_objects(&mut target_conn, input_path, &manifest.objects)?;
```

- [ ] **Step 7: Add object count to final report payload**

```rust
"objects_restored": objects_restored,
```

- [ ] **Step 8: Run tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_object_manifest --lib
cargo test --manifest-path migration_core\Cargo.toml dump_manifest --lib
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: capture and restore mysql dump objects"
```

---

### Task 8: Add Persistent Import Progress State

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing tests for progress state persistence**

Add:

```rust
#[test]
fn import_progress_state_roundtrips_with_manifest_hash() {
    let dir = tempfile::tempdir().unwrap();
    let path = dir.path().join("_tunnelforge_import_progress.json");
    let state = ImportProgressState {
        manifest_hash: "abc123".to_string(),
        target_identity: "mysql://localhost/app".to_string(),
        import_mode: "replace".to_string(),
        completed_steps: vec!["load_schema".to_string()],
        failed_steps: Vec::new(),
        completed_chunks: BTreeSet::from(["orders:1".to_string()]),
        verification_complete: false,
        switched: false,
        cleanup_complete: false,
    };

    write_import_progress_state(&path, &state).unwrap();
    let loaded = read_import_progress_state(&path).unwrap();

    assert_eq!(loaded, state);
}

#[test]
fn import_resume_rejects_mismatched_manifest_hash() {
    let state = ImportProgressState {
        manifest_hash: "old".to_string(),
        target_identity: "mysql://localhost/app".to_string(),
        import_mode: "replace".to_string(),
        completed_steps: Vec::new(),
        failed_steps: Vec::new(),
        completed_chunks: BTreeSet::new(),
        verification_complete: false,
        switched: false,
        cleanup_complete: false,
    };

    let err = validate_import_resume_state(&state, "new", "mysql://localhost/app", "replace")
        .unwrap_err();

    assert!(err.contains("progress file belongs to a different dump manifest"));
}
```

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_progress_state --lib
```

Expected: FAIL because progress state functions are missing.

- [ ] **Step 3: Add progress state struct and helpers**

```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ImportProgressState {
    pub manifest_hash: String,
    pub target_identity: String,
    pub import_mode: String,
    #[serde(default)]
    pub completed_steps: Vec<String>,
    #[serde(default)]
    pub failed_steps: Vec<String>,
    #[serde(default)]
    pub completed_chunks: BTreeSet<String>,
    #[serde(default)]
    pub verification_complete: bool,
    #[serde(default)]
    pub switched: bool,
    #[serde(default)]
    pub cleanup_complete: bool,
}

fn write_import_progress_state(path: &Path, state: &ImportProgressState) -> Result<(), String> {
    let tmp_path = path.with_extension("json.tmp");
    let file = File::create(&tmp_path)
        .map_err(|err| format!("failed to create import progress state: {err}"))?;
    serde_json::to_writer_pretty(file, state)
        .map_err(|err| format!("failed to write import progress state: {err}"))?;
    fs::rename(&tmp_path, path)
        .map_err(|err| format!("failed to replace import progress state: {err}"))
}

fn read_import_progress_state(path: &Path) -> Result<ImportProgressState, String> {
    let file = File::open(path)
        .map_err(|err| format!("failed to open import progress state: {err}"))?;
    serde_json::from_reader(file)
        .map_err(|err| format!("failed to parse import progress state: {err}"))
}

fn validate_import_resume_state(
    state: &ImportProgressState,
    manifest_hash: &str,
    target_identity: &str,
    import_mode: &str,
) -> Result<(), String> {
    if state.manifest_hash != manifest_hash {
        return Err("progress file belongs to a different dump manifest".to_string());
    }
    if state.target_identity != target_identity {
        return Err("progress file belongs to a different target database".to_string());
    }
    if state.import_mode != import_mode {
        return Err("progress file belongs to a different import mode".to_string());
    }
    Ok(())
}
```

- [ ] **Step 4: Add manifest hash helper**

```rust
fn dump_manifest_hash(input_path: &Path) -> Result<String, String> {
    let manifest_path = input_path.join("_tunnelforge_dump.json");
    let bytes = fs::read(&manifest_path)
        .map_err(|err| format!("failed to read dump manifest for hashing: {err}"))?;
    Ok(format!("{:x}", Sha256::digest(&bytes)))
}
```

- [ ] **Step 5: Initialize progress file in `dump_import_streaming`**

After manifest preflight and before target preparation:

```rust
let manifest_hash = dump_manifest_hash(input_path)?;
let target_identity = format!("{}://{}:{}/{}", target.engine, target.host, target.port, target.database);
let progress_path = input_path.join("_tunnelforge_import_progress.json");
let mut progress_state = ImportProgressState {
    manifest_hash: manifest_hash.clone(),
    target_identity: target_identity.clone(),
    import_mode: mode.clone(),
    completed_steps: Vec::new(),
    failed_steps: Vec::new(),
    completed_chunks: BTreeSet::new(),
    verification_complete: false,
    switched: false,
    cleanup_complete: false,
};
write_import_progress_state(&progress_path, &progress_state)?;
```

- [ ] **Step 6: Mark major steps complete**

After schema creation:

```rust
progress_state.completed_steps.push("load_schema".to_string());
write_import_progress_state(&progress_path, &progress_state)?;
```

After data load:

```rust
progress_state.completed_steps.push("load_data".to_string());
write_import_progress_state(&progress_path, &progress_state)?;
```

After object restore:

```rust
progress_state.completed_steps.push("load_objects".to_string());
write_import_progress_state(&progress_path, &progress_state)?;
```

- [ ] **Step 7: Run focused tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_progress_state --lib
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: persist dump import progress state"
```

---

### Task 9: Implement Resume And Reset Semantics

**Files:**
- Modify: `migration_core/src/lib.rs`
- Modify: `src/exporters/rust_dump_exporter.py`
- Modify: `tests/test_rust_dump_exporter.py`

- [ ] **Step 1: Add failing wrapper test for resume and reset policy payload**

In `tests/test_rust_dump_exporter.py`, add:

```python
def test_import_dump_forwards_progress_policy(tmp_path):
    dump_dir = tmp_path / "dump"
    dump_dir.mkdir()
    (dump_dir / "_tunnelforge_dump.json").write_text(
        json.dumps(
            {
                "format": "tunnelforge-dump",
                "format_version": 3,
                "data_format": "tsv",
                "compression": "none",
                "source_engine": "mysql",
                "database": "app",
                "restorability": "strict_restorable",
                "features": {"checksum": True, "snapshot": True},
                "tables": [],
            }
        ),
        encoding="utf-8",
    )
    facade = RecordingDumpCoreService()
    exporter = RustDumpExporter(core_service=facade)

    exporter.import_dump(str(dump_dir), {"database": "app"}, progress_policy="resume")

    assert facade.payload["progress_policy"] == "resume"
```

- [ ] **Step 2: Run wrapper test and confirm failure**

```powershell
pytest tests/test_rust_dump_exporter.py::test_import_dump_forwards_progress_policy -q
```

Expected: FAIL because `progress_policy` is not accepted or forwarded.

- [ ] **Step 3: Add Rust progress policy handling**

In `dump_import_streaming`, read:

```rust
let progress_policy = request
    .payload
    .get("progress_policy")
    .and_then(Value::as_str)
    .unwrap_or("fresh");
if !matches!(progress_policy, "fresh" | "resume" | "reset") {
    return Err(format!("unsupported import progress_policy {progress_policy}"));
}
```

Handle existing state:

```rust
if progress_path.exists() {
    match progress_policy {
        "resume" => {
            progress_state = read_import_progress_state(&progress_path)?;
            validate_import_resume_state(&progress_state, &manifest_hash, &target_identity, &mode)?;
        }
        "reset" => {
            fs::remove_file(&progress_path)
                .map_err(|err| format!("failed to reset import progress state: {err}"))?;
        }
        _ => {
            return Err("existing import progress state requires resume or reset".to_string());
        }
    }
}
```

- [ ] **Step 4: Skip completed chunks on resume**

Before loading a chunk, build key:

```rust
let chunk_key = format!("{}:{chunk_index}", table_manifest.name);
if progress_state.completed_chunks.contains(&chunk_key) {
    continue;
}
```

After successful chunk load:

```rust
progress_state.completed_chunks.insert(chunk_key);
write_import_progress_state(&progress_path, &progress_state)?;
```

- [ ] **Step 5: Forward policy from Python wrapper**

In `RustDumpExporter.import_dump`, add parameter:

```python
progress_policy: str = "fresh",
```

Add to payload:

```python
"progress_policy": progress_policy,
```

- [ ] **Step 6: Run focused tests**

```powershell
pytest tests/test_rust_dump_exporter.py::test_import_dump_forwards_progress_policy -q
cargo test --manifest-path migration_core\Cargo.toml import_progress_state --lib
```

Expected: PASS.

- [ ] **Step 7: Commit**

```powershell
git add migration_core\src\lib.rs src\exporters\rust_dump_exporter.py tests\test_rust_dump_exporter.py
git commit -m "feat: support dump import resume and reset"
```

---

### Task 10: Add Strong Verification And Session Policy Evidence

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Write failing tests for final verification verdict**

Add:

```rust
#[test]
fn import_verdict_fails_on_row_count_mismatch() {
    let evidence = ImportVerificationEvidence {
        tables: vec![TableVerificationEvidence {
            table: "orders".to_string(),
            expected_rows: 10,
            actual_rows: 9,
            checksum_match: Some(true),
            digest_match: None,
        }],
        load_data_warnings: 0,
        sql_mode_before: Some("STRICT_TRANS_TABLES".to_string()),
        sql_mode_after: Some("STRICT_TRANS_TABLES".to_string()),
        local_infile_enabled: true,
        session_timezone: Some("+00:00".to_string()),
    };

    let verdict = classify_import_verdict(&evidence);

    assert_eq!(verdict, "failed");
}

#[test]
fn import_verdict_is_success_when_rows_and_checksums_match() {
    let evidence = ImportVerificationEvidence {
        tables: vec![TableVerificationEvidence {
            table: "orders".to_string(),
            expected_rows: 10,
            actual_rows: 10,
            checksum_match: Some(true),
            digest_match: Some(true),
        }],
        load_data_warnings: 0,
        sql_mode_before: Some("STRICT_TRANS_TABLES".to_string()),
        sql_mode_after: Some("STRICT_TRANS_TABLES".to_string()),
        local_infile_enabled: true,
        session_timezone: Some("+00:00".to_string()),
    };

    let verdict = classify_import_verdict(&evidence);

    assert_eq!(verdict, "success");
}
```

- [ ] **Step 2: Run focused tests and confirm failure**

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_verdict --lib
```

Expected: FAIL because verification evidence types are missing.

- [ ] **Step 3: Add verification evidence structs**

```rust
#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct TableVerificationEvidence {
    pub table: String,
    pub expected_rows: u64,
    pub actual_rows: u64,
    #[serde(default)]
    pub checksum_match: Option<bool>,
    #[serde(default)]
    pub digest_match: Option<bool>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct ImportVerificationEvidence {
    pub tables: Vec<TableVerificationEvidence>,
    pub load_data_warnings: u64,
    #[serde(default)]
    pub sql_mode_before: Option<String>,
    #[serde(default)]
    pub sql_mode_after: Option<String>,
    pub local_infile_enabled: bool,
    #[serde(default)]
    pub session_timezone: Option<String>,
}
```

- [ ] **Step 4: Add verdict classifier**

```rust
fn classify_import_verdict(evidence: &ImportVerificationEvidence) -> &'static str {
    let table_failed = evidence.tables.iter().any(|table| {
        table.expected_rows != table.actual_rows
            || table.checksum_match == Some(false)
            || table.digest_match == Some(false)
    });
    if table_failed {
        return "failed";
    }
    if evidence.load_data_warnings > 0 {
        return "limited_success";
    }
    "success"
}
```

- [ ] **Step 5: Capture and restore SQL mode**

Before import session changes:

```rust
let sql_mode_before: Option<String> = target_conn
    .query_first("SELECT @@SESSION.sql_mode")
    .map_err(|err| format!("failed to read session sql_mode: {err}"))?;
```

Before return:

```rust
if let Some(sql_mode) = &sql_mode_before {
    target_conn
        .exec_drop("SET SESSION sql_mode = ?", (sql_mode,))
        .map_err(|err| format!("failed to restore session sql_mode: {err}"))?;
}
let sql_mode_after: Option<String> = target_conn
    .query_first("SELECT @@SESSION.sql_mode")
    .map_err(|err| format!("failed to read restored session sql_mode: {err}"))?;
```

- [ ] **Step 6: Build verification evidence after import**

Use existing row-count verification results and add:

```rust
let verification = ImportVerificationEvidence {
    tables: table_verification_evidence,
    load_data_warnings,
    sql_mode_before,
    sql_mode_after,
    local_infile_enabled: true,
    session_timezone: request
        .payload
        .get("timezone")
        .and_then(Value::as_str)
        .map(str::to_string),
};
let verdict = classify_import_verdict(&verification);
```

If any `SHOW WARNINGS` or `warning_count` helper exists after `LOAD DATA`, feed its count into `load_data_warnings`. If there is no helper, add:

```rust
let warning_count: Option<u64> = target_conn
    .query_first("SELECT @@warning_count")
    .map_err(|err| format!("failed to read LOAD DATA warning count: {err}"))?;
let load_data_warnings = warning_count.unwrap_or(0);
```

- [ ] **Step 7: Write verification evidence to import report**

Extend `_tunnelforge_import_report.json` with:

```rust
"verification": verification,
"verdict": verdict,
```

- [ ] **Step 8: Run tests**

```powershell
cargo test --manifest-path migration_core\Cargo.toml import_verdict --lib
cargo test --manifest-path migration_core\Cargo.toml dump_import --lib
```

Expected: PASS.

- [ ] **Step 9: Commit**

```powershell
git add migration_core\src\lib.rs
git commit -m "feat: record strong dump import verification"
```

---

### Task 11: Add Golden Fixtures And Regression Tests

**Files:**
- Add: `tests/fixtures/mysqlsh_grade/strict_manifest.json`
- Add: `tests/fixtures/mysqlsh_grade/limited_legacy_manifest.json`
- Add: `tests/fixtures/mysqlsh_grade/not_restorable_manifest.json`
- Add: `tests/fixtures/mysqlsh_grade/progress_state.json`
- Modify: `tests/test_rust_dump_exporter.py`
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Add strict manifest fixture**

Create `tests/fixtures/mysqlsh_grade/strict_manifest.json`:

```json
{
  "format": "tunnelforge-dump",
  "format_version": 3,
  "data_format": "tsv",
  "compression": "none",
  "source_engine": "mysql",
  "source_version": "8.0.36",
  "database": "app",
  "schema": { "tables": [] },
  "chunk_size": 1000,
  "created_unix_seconds": 1790000000,
  "snapshot_policy": "transaction_snapshot",
  "strict_export": true,
  "manifest_warnings": [],
  "dump_scope": "schema",
  "features": { "snapshot": true, "chunking": true, "checksum": true, "timezone": true },
  "restorability": "strict_restorable",
  "blockers": [],
  "objects": { "objects": [] },
  "tables": []
}
```

- [ ] **Step 2: Add limited legacy fixture**

Create `tests/fixtures/mysqlsh_grade/limited_legacy_manifest.json`:

```json
{
  "format": "tunnelforge-dump",
  "format_version": 2,
  "data_format": "tsv",
  "compression": "zstd",
  "source_engine": "mysql",
  "database": "dataflare",
  "schema": { "tables": [] },
  "chunk_size": 1000,
  "created_unix_seconds": 1780000000,
  "snapshot_policy": "not_enforced",
  "strict_export": false,
  "manifest_warnings": ["snapshot consistency is not proven"],
  "dump_scope": "schema",
  "features": { "snapshot": false, "chunking": true, "checksum": false, "timezone": false },
  "restorability": "limited_restorable",
  "blockers": [],
  "objects": { "objects": [] },
  "tables": []
}
```

- [ ] **Step 3: Add not-restorable fixture**

Create `tests/fixtures/mysqlsh_grade/not_restorable_manifest.json`:

```json
{
  "format": "tunnelforge-dump",
  "format_version": 3,
  "data_format": "tsv",
  "compression": "none",
  "source_engine": "mysql",
  "database": "app",
  "schema": { "tables": [] },
  "chunk_size": 1000,
  "created_unix_seconds": 1790000001,
  "snapshot_policy": "transaction_snapshot",
  "strict_export": false,
  "manifest_warnings": [],
  "dump_scope": "schema",
  "features": { "snapshot": true, "chunking": true, "checksum": true, "unsupported": ["mysql.unknown_feature"] },
  "restorability": "not_restorable",
  "blockers": ["unsupported feature mysql.unknown_feature"],
  "objects": { "objects": [] },
  "tables": []
}
```

- [ ] **Step 4: Add progress state fixture**

Create `tests/fixtures/mysqlsh_grade/progress_state.json`:

```json
{
  "manifest_hash": "abc123",
  "target_identity": "mysql://localhost:3306/app",
  "import_mode": "replace",
  "completed_steps": ["load_schema"],
  "failed_steps": [],
  "completed_chunks": ["orders:1"],
  "verification_complete": false,
  "switched": false,
  "cleanup_complete": false
}
```

- [ ] **Step 5: Add Python fixture regression test**

In `tests/test_rust_dump_exporter.py`, add:

```python
def test_mysqlsh_grade_manifest_fixtures_are_classified():
    fixtures = Path("tests/fixtures/mysqlsh_grade")
    strict = json.loads((fixtures / "strict_manifest.json").read_text(encoding="utf-8"))
    limited = json.loads((fixtures / "limited_legacy_manifest.json").read_text(encoding="utf-8"))
    blocked = json.loads((fixtures / "not_restorable_manifest.json").read_text(encoding="utf-8"))

    assert strict["restorability"] == "strict_restorable"
    assert limited["restorability"] == "limited_restorable"
    assert blocked["restorability"] == "not_restorable"
    assert blocked["blockers"] == ["unsupported feature mysql.unknown_feature"]
```

- [ ] **Step 6: Add Rust fixture parse test**

In Rust tests, add:

```rust
#[test]
fn mysqlsh_grade_fixtures_parse_as_dump_manifests() {
    for path in [
        "../tests/fixtures/mysqlsh_grade/strict_manifest.json",
        "../tests/fixtures/mysqlsh_grade/limited_legacy_manifest.json",
        "../tests/fixtures/mysqlsh_grade/not_restorable_manifest.json",
    ] {
        let json = fs::read_to_string(path).unwrap();
        let manifest: DumpManifest = serde_json::from_str(&json).unwrap();
        let grade = grade_dump_artifact(&manifest);
        assert_eq!(manifest.restorability, grade.restorability);
    }
}
```

- [ ] **Step 7: Run fixture tests**

```powershell
pytest tests/test_rust_dump_exporter.py::test_mysqlsh_grade_manifest_fixtures_are_classified -q
cargo test --manifest-path migration_core\Cargo.toml mysqlsh_grade_fixtures_parse_as_dump_manifests --lib
```

Expected: PASS.

- [ ] **Step 8: Commit**

```powershell
git add tests\fixtures\mysqlsh_grade tests\test_rust_dump_exporter.py migration_core\src\lib.rs
git commit -m "test: add mysqlsh grade dump fixtures"
```

---

### Task 12: Update Operator Report And Run Full Verification

**Files:**
- Modify: `reports/export_import_flow_review_20260601.html`

- [ ] **Step 1: Update report with parity matrix**

Add a section titled `MySQL Shell Grade Implementation Verdict` with this table:

```html
<table>
  <thead>
    <tr>
      <th>Dimension</th>
      <th>Target</th>
      <th>TunnelForge Status</th>
      <th>Evidence</th>
    </tr>
  </thead>
  <tbody>
    <tr><td>Metadata and feature contract</td><td>Manifest declares supported features</td><td>Implemented</td><td>format_version 3, features, restorability, blockers</td></tr>
    <tr><td>Consistent snapshot</td><td>Strict dumps prove snapshot strategy</td><td>Implemented with explicit limits</td><td>snapshot_policy and manifest warnings</td></tr>
    <tr><td>DDL and data separation</td><td>Schema/data/object files are separated</td><td>Implemented</td><td>tables plus objects manifest</td></tr>
    <tr><td>Object support</td><td>Views, triggers, routines, and events are visible</td><td>Implemented for captured object classes</td><td>objects manifest and object restore count</td></tr>
    <tr><td>Progress and resume</td><td>Interrupted imports can resume or reset</td><td>Implemented</td><td>_tunnelforge_import_progress.json</td></tr>
    <tr><td>Verification</td><td>Final result is evidence-backed</td><td>Implemented</td><td>_tunnelforge_import_report.json verdict and verification</td></tr>
    <tr><td>Legacy dump honesty</td><td>Legacy artifacts are not presented as strict success</td><td>Implemented</td><td>limited_restorable and UI disabled recommended import</td></tr>
  </tbody>
</table>
```

- [ ] **Step 2: Run Rust verification**

```powershell
cargo test --manifest-path migration_core\Cargo.toml
```

Expected: PASS with all Rust tests passing.

- [ ] **Step 3: Build Rust release binary**

```powershell
cargo build --manifest-path migration_core\Cargo.toml --release
```

Expected: PASS and binary exists at `migration_core\target\release\tunnelforge-core.exe`.

- [ ] **Step 4: Run Python tests**

```powershell
pytest
```

Expected: PASS with the known warning count documented in final response.

- [ ] **Step 5: Run service smoke test**

```powershell
@'
import json
import subprocess
from pathlib import Path

binary = Path("migration_core/target/release/tunnelforge-core.exe")
proc = subprocess.run(
    [str(binary)],
    input=json.dumps({"command": "service.hello"}) + "\n",
    text=True,
    capture_output=True,
    check=True,
)
print(proc.stdout)
'@ | python -
```

Expected: output contains `"event":"result"` and `"service":"tunnelforge-core"`.

- [ ] **Step 6: Run whitespace check**

```powershell
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 7: Commit report and final verification changes**

```powershell
git add reports\export_import_flow_review_20260601.html
git commit -m "docs: report mysqlsh grade export import status"
```

---

## Final Acceptance Criteria

- Current PROD dump `C:\Users\QESG\Desktop\PROD_root_dataflare_20260601_120617` is classified as `limited_restorable`, not strict, because its legacy manifest lacks full snapshot and compatibility metadata.
- A new MySQL export with complete snapshot/checksum evidence is classified as `strict_restorable`.
- Import rejects `not_restorable` artifacts before mutating the target.
- Recommended full-replace import is disabled for limited legacy dumps unless the user explicitly chooses a limited/manual path.
- Full replace import uses shadow routing, FK compatibility validation, row-count verification, object restore, final report writing, and verdict classification.
- Interrupted imports write `_tunnelforge_import_progress.json` and require explicit `resume` or `reset`.
- `_tunnelforge_import_report.json` includes manifest hash, target identity, import mode, restored rows, restored objects, verification evidence, session policy, and final verdict.
- Rust tests, Python tests, Rust release build, service smoke test, and `git diff --check` pass.
