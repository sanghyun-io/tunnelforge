# MySQL Shell Grade Export/Import Design

Date: 2026-06-01

## Objective

Raise TunnelForge Export/Import to a level users can trust by using MySQL
Shell dump/load behavior as the reliability baseline. The goal is not file
format compatibility with `mysqlsh`; the goal is comparable evidence,
guardrails, resumability, and honesty about what is and is not restored.

This design is intentionally long-horizon. It covers strict artifact grading,
consistent export, schema and object fidelity, persistent import progress,
stronger verification, UI/operator reporting, and acceptance tests.

## Reference Baseline

The baseline is derived from official MySQL documentation:

- MySQL Shell dump utilities:
  <https://dev.mysql.com/doc/mysql-shell/8.0/en/mysql-shell-utilities-dump-instance-schema.html>
- MySQL Shell load utility:
  <https://dev.mysql.com/doc/mysql-shell/8.4/en/mysql-shell-utilities-load-dump.html>
- MySQL `LOAD DATA` behavior:
  <https://dev.mysql.com/doc/mysql/8.0/en/load-data.html>

The comparison dimensions are:

- Metadata, format version, features, GTID/binlog metadata.
- Consistent snapshot semantics.
- DDL and data separation.
- Timezone policy.
- Chunking, parallelism, and partition awareness.
- Persistent progress state, resume, reset.
- Object support: users, roles, grants, views, routines, triggers, events.
- `LOAD DATA LOCAL INFILE` caveats.
- Checksum and verification.
- Include/exclude filter conflicts and explicit failure.

## Current Assessment

Recent remediation has moved Import in the right direction:

- MySQL text column charset/collation is preserved in inspected type metadata.
- Full MySQL shadow restore routes loader workers to the shadow database.
- Strict import rejects chunked manifests without checksum metadata.
- Merge import no longer blindly reapplies recreate-style post-load DDL.
- Import success is gated by row counts.
- FK column compatibility is validated before post-load FK DDL.
- Import writes `_tunnelforge_import_report.json`.
- Python forwards `strict_manifest` and validated timezone intent to Rust.
- UI no longer claims views/routines/triggers/events are automatically restored.

Important gaps remain:

- `dump.run` records `snapshot_policy: not_enforced`; strict consistent export
  is not implemented.
- Table-level MySQL metadata is incomplete: engine, default charset/collation,
  row format, and partition metadata are not structurally preserved.
- Unsupported objects are detected in some paths but not captured/restored as
  first-class dump objects.
- Import has row-count verification but not row digest/content verification.
- Persistent progress state and mysqlsh-style resume/reset are not implemented.
- UI does not yet expose a full compatibility profile for legacy/incomplete
  dumps, local infile policy, SQL mode policy, and session timezone.

## Target Standard

TunnelForge artifacts and imports are classified into three grades.

### Strict Restorable

This is the only default recommended operating path.

Required properties:

- Snapshot consistency is proven and recorded.
- Manifest version and feature metadata are supported by the target.
- Schema, table, column, index, FK, object, and data metadata are sufficient for
  verification.
- Chunk checksums or row digests cover dumped data.
- Unsupported objects are absent, explicitly excluded, or captured with restore
  instructions.
- Import completes only after verification and report writing.

### Limited Restorable

The artifact can be restored with explicit limitations, but it is not a trusted
full restore.

Examples:

- Snapshot consistency is not proven.
- Routines, events, triggers, views, users, or grants are not included.
- Table engine or default collation metadata is incomplete.
- Some checksums or digests are missing.
- The artifact is a legacy dump.

Limited restore must never be displayed as verified success.

### Not Restorable

The artifact or requested import plan is unsafe or impossible.

Examples:

- Unsupported manifest feature.
- Checksum mismatch.
- Requested object/table filter conflict.
- Missing FK dependencies.
- Strict import requested for a non-consistent artifact.
- Unsupported object must be restored but was not captured.

## Architecture

The design has four durable artifacts and one Rust-owned state machine.

### Dump Artifact

The artifact contains:

- Schema metadata and/or DDL.
- Table data chunks.
- Object DDL for views, routines, triggers, and events.
- Optional users, roles, and grants for instance-scope exports.
- Checksum or digest files.
- Feature metadata.

The artifact must make included and excluded scopes visible without opening the
application.

### Manifest

The manifest is the import contract.

Required fields:

- `format_version`
- `source_engine`, `source_version`
- `target_requirements`
- `dump_scope`: instance, schema, or table
- `features`: snapshot, chunking, partitioning, routines, events, triggers,
  users, grants, checksum, timezone, GTID/binlog
- `snapshot_policy`: transaction_snapshot, lock_based, backup_lock,
  not_enforced
- `restorability`: strict_restorable, limited_restorable, not_restorable
- `warnings` and `blockers`
- Table metadata: engine, default charset, default collation, row format,
  columns, indexes, foreign keys, generated columns, invisible columns, check
  constraints, partitions
- Object metadata: views, routines, triggers, events, users/roles/grants when
  applicable
- Data metadata: chunk paths, row counts, byte sizes, checksums, row digest
  policy

### Progress State File

This is TunnelForge's equivalent of mysqlsh `progressFile`.

It records:

- Manifest hash.
- Target identity.
- Import mode.
- Completed and failed schema/object/table/chunk steps.
- Verification steps.
- Switch and cleanup status.

Resume requires a valid progress file. Reset discards progress but must report
that existing target objects may require cleanup.

### Import Report

The report is the final verdict evidence.

It includes:

- Source dump id and manifest hash.
- Target identity.
- Import mode.
- Loaded tables, chunks, and objects.
- Row count, checksum, digest, schema, FK, and object verification results.
- Unsupported or skipped objects.
- Session policy: timezone, local_infile, SQL mode.
- LOAD DATA warning counts and samples when available.
- Final verdict: success, limited_success, failed, manual_action_required.

### Rust Core State Machine

Python/PyQt does not own DB semantics. Rust owns:

- `inspect`
- `export_plan`
- `snapshot_begin`
- `schema_capture`
- `object_capture`
- `data_dump`
- `manifest_finalize`
- `import_preflight`
- `target_prepare`
- `load_schema`
- `load_data`
- `load_objects`
- `verify`
- `switch`
- `cleanup`
- `report`

Python forwards user intent and displays classified events and reports.

## Export Design

### Preflight

Export preflight inspects:

- MySQL version and capabilities.
- Storage engine distribution.
- Required privileges for lock/snapshot strategy.
- Tables, views, routines, triggers, events, users, roles, grants.
- FK dependencies.
- Generated columns, invisible columns, auto increment.
- Partitions and subpartitions.
- Timezone policy.
- Unsupported features.

Preflight produces a provisional artifact grade before data is dumped.

### Snapshot Policy

Strict export requires a proven consistent snapshot.

Preferred strategies:

- Transaction snapshot using `START TRANSACTION WITH CONSISTENT SNAPSHOT` where
  valid.
- Lock-based snapshot using table/global locks where required.
- Backup lock strategy when privileges and server version allow it.

Rules:

- Non-InnoDB tables cannot receive the same consistency claim unless the lock
  strategy actually covers them.
- If consistency cannot be proven, the manifest records
  `snapshot_policy: not_enforced`.
- `not_enforced` artifacts cannot be strict restorable.

### Schema Fidelity

MySQL same-engine restore must preserve:

- Table engine.
- Table default charset and collation.
- Row format.
- Column type, charset, collation, default, nullability.
- Generated expressions.
- Invisible column state.
- Auto increment state and sequence behavior.
- Primary, unique, secondary, and fulltext/spatial indexes where supported.
- Foreign keys.
- Check constraints.
- Partition/subpartition definitions.

Cross-engine migration remains a separate transformation path.

### Object Capture

Supported object scopes are explicit:

- Views.
- Triggers.
- Routines: procedures and functions.
- Events.
- Users, roles, and grants only for instance-scope exports.

Schema/table export must not imply users or global grants are included.

### Data Dump

Each chunk records:

- Table name.
- Partition/subpartition origin when applicable.
- Chunk ordering key.
- Row count.
- Byte size.
- SHA-256 checksum.
- Optional row digest.
- Data format and version.

### Timezone

Timezone policy is explicit:

- `utc_normalized`
- `source_preserved`
- `manual`
- `unknown`

`unknown` prevents strict restorable grading.

### Finalization

Export ends by grading the artifact. File creation is not enough.

Final statuses:

- `strict_restorable_export`
- `limited_export`
- `not_restorable_export`

## Import Design

### Preflight

Import preflight validates before mutation:

- Manifest version and feature compatibility.
- Source/target MySQL version compatibility.
- Dump scope and target scope.
- Selected table/object filters.
- FK dependency completeness.
- Required checksums/digests.
- Snapshot policy compatibility with requested mode.
- Unsupported objects and manual requirements.
- Existing target object policy.

### Modes

User-facing modes should be semantic:

- `strict_full_restore`
- `strict_schema_restore`
- `strict_data_restore`
- `limited_restore`
- `resume_from_progress`
- `reset_progress_and_restore`

Existing internal aliases may remain temporarily, but UI should not present
ambiguous terms as the product contract.

### Progress State

The progress file records:

- Schema created.
- Object DDL applied.
- Table created.
- Chunk loaded.
- Index applied.
- FK applied.
- Table verified.
- Switch completed.
- Cleanup completed.

Resume rules:

- Manifest hash must match.
- Target identity must match.
- Completed chunks/objects are skipped.
- Failed step restarts from a known safe boundary.
- Reset warns about cleanup and object deduplication limits.

### Load Order

Strict full restore order:

1. Prepare shadow target.
2. Create schemas/tables without secondary indexes and FKs.
3. Load table data chunks.
4. Apply secondary indexes.
5. Apply FKs.
6. Apply views, routines, triggers, and events in dependency-aware order.
7. Verify.
8. Switch.
9. Cleanup.
10. Write report.

Triggers default to post-data-load application to avoid load-time side effects.

### Verification

Required:

- Row count per table.
- Chunk checksum.
- Schema compatibility.
- FK compatibility.
- Object DDL application result.

Recommended:

- Row digest by primary key.
- Sampled row comparison.
- Table checksum when supported.
- LOAD DATA warning policy check.

### Session Policy

Session-sensitive behavior is reportable:

- `local_infile`: enabled, temporary_enabled, fallback, blocked.
- `sql_mode`: original captured, relaxed, restored.
- `timezone`: utc, source, manual.
- LOAD DATA warning count and samples.

SQL mode restoration must restore the original value, not blindly set
`DEFAULT`.

### Failure Policy

- Preflight failure: no mutation.
- Load failure: progress state is written, switch is forbidden.
- Verification failure: switch is forbidden.
- Switch failure: repair instructions are written.
- Cleanup failure: data verdict and cleanup warning are separate.

## UI And Operator Experience

UI should show guarantees, not just status.

### Export Result

Show:

- Restorability grade.
- Snapshot policy.
- Included objects.
- Excluded/unsupported objects.
- Checksum/digest coverage.
- Timezone policy.
- Warnings and blockers.
- Artifact path.

### Import Preflight

Show:

- Dump grade.
- Target compatibility.
- Mode compatibility.
- Selected tables/objects.
- Destructive operations.
- Shadow target usage.
- Progress state availability.
- Resume/reset availability.
- Blockers and warnings.

Strict restore is enabled only for strict artifacts. Limited restore requires
explicit acknowledgement.

### Import Progress

Show stage states:

- preflight
- prepare target
- create schema
- load data chunks
- apply indexes
- apply FKs
- apply objects
- verify
- switch
- cleanup
- report

Each stage is pending, running, done, failed, or skipped.

### Decision Events

Raw events are converted into operator decisions:

- local_infile disabled, fallback used.
- SQL mode relaxed, warnings observed.
- Legacy dump, missing checksums.
- Views detected but not included.
- Resume blocked, manifest hash mismatch.

### Final Report

UI displays:

- `success`
- `limited_success`
- `failed`
- `manual_action_required`

The report, manifest, and progress state are openable artifacts.

## Testing And Acceptance Criteria

### Artifact Grading

Tests cover:

- Consistent snapshot plus full metadata produces strict restorable.
- Missing snapshot produces limited restorable.
- Missing checksum blocks strict import.
- Unsupported objects produce limited or blocked status.
- Feature mismatch produces not restorable.

### Snapshot

Tests cover:

- Strict export starts snapshot before schema/count/data reads.
- Non-InnoDB tables limit strict grading unless locked.
- Missing privileges produce limited or blocked grading.
- Manifest never lies about consistency.

### Schema Fidelity

Tests cover:

- Engine.
- Table default charset/collation.
- Row format.
- Generated columns.
- Invisible columns.
- Indexes, FKs, check constraints.
- Partition metadata.
- View/routine/trigger/event DDL.

### Import State Machine

Tests cover:

- Chunk load interruption and resume.
- Manifest hash mismatch blocks resume.
- Target identity mismatch blocks resume.
- Reset records cleanup requirements.
- Pre-switch failure leaves target unswitched.

### Verification

Tests cover:

- Row count mismatch.
- Checksum mismatch.
- Schema mismatch.
- FK compatibility mismatch.
- Object DDL failure.
- LOAD DATA warnings under strict and limited policy.

### UI

Tests cover:

- Strict-disabled UI for limited dumps.
- Limited restore warnings.
- No unsupported full-restore claims.
- Resume/reset wording tied to progress state.
- Final verdict display.

### Golden Fixtures

Fixtures include:

- Simple InnoDB schema.
- Collation-sensitive FK schema.
- Generated and invisible columns.
- Views, routines, triggers, events.
- Partitioned table.
- Non-InnoDB table.
- Legacy/incomplete dump.
- Checksum mismatch dump.
- Interrupted import state.

## Phasing

### Phase 1: Honest Grading And UI

- Artifact grading model.
- Manifest feature map.
- Strict/limited/not-restorable UI.
- Compatibility profile in reports.

### Phase 2: Strict Export Consistency

- MySQL snapshot/lock strategies.
- Engine-aware consistency grading.
- Snapshot evidence in manifest.

### Phase 3: Schema And Object Fidelity

- Table-level metadata.
- Object DDL capture/restore.
- Object support matrix.

### Phase 4: Persistent Import State

- Progress state file.
- Resume/reset semantics.
- Partial failure recovery boundaries.

### Phase 5: Strong Verification

- Row digest/checksum verification.
- LOAD DATA warning policy.
- SQL mode original-value restoration.

### Phase 6: mysqlsh Parity Review

- Re-run the 10-point baseline checklist.
- Update final report with pass/partial/fail status.
- Keep explicitly unsupported features documented.

## Completion Criteria

The long-horizon program is complete when:

- A strict artifact cannot be produced without proven snapshot consistency.
- A strict import cannot run against a limited or not-restorable artifact.
- Metadata and object support are explicit and test-covered.
- Resume requires and honors persistent progress state.
- Verification covers row counts, checksums/digests, schema, FK, objects, and
  session warning policy.
- UI and reports show what is guaranteed, what is skipped, and what requires
  manual action.
- The 10-point mysqlsh baseline is reflected in tests and final documentation.

