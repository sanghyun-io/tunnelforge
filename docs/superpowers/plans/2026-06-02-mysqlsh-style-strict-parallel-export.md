# MySQL Shell Style Strict Parallel Export Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a MySQL strict parallel export path that aligns worker snapshots before dumping and updates Import UI policy for strict, limited, and blocked dumps.

**Architecture:** Rust Core owns the consistency model. Python forwards manifest strictness and presents user choices, but does not decide whether an artifact is strict.

**Tech Stack:** Rust `mysql` crate, Rust unit tests, Python PyQt6 dialog tests, existing JSONL Rust Core facade.

---

### Task 1: Rust Snapshot Strategy and Manifest Semantics

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] Add a parallel lock-synchronized strategy name, for example `lock_synchronized_transaction_snapshot`.
- [ ] Update classification tests so parallel InnoDB exports can be strict when lock synchronization is available.
- [ ] Keep current `not_enforced` behavior when no synchronization is available.
- [ ] Remove the transaction-snapshot manifest warning once schema, row count, object capture, and data reads are covered by the chosen strategy.

### Task 2: Rust Lock/Snapshot Setup Helpers

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] Add helpers to acquire and release `FLUSH TABLES WITH READ LOCK`.
- [ ] Add helpers to start repeatable-read consistent snapshot transactions on a set of MySQL connections.
- [ ] Add best-effort `LOCK INSTANCE FOR BACKUP` and `UNLOCK INSTANCE` helpers.
- [ ] Add unit tests for SQL order and cleanup behavior where logic can be tested without a live MySQL server.

### Task 3: Rust Parallel Worker Connection Ownership

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] Add a strict MySQL global dump path that pre-opens worker connections.
- [ ] Start snapshot transactions on every worker connection while the initial read lock is held.
- [ ] Dispatch global work items to worker threads using those pre-snapshotted connections.
- [ ] Ensure every worker commits or rolls back and every lock is released on success or error.

### Task 4: Python Import Policy

**Files:**
- Modify: `src/exporters/rust_dump_exporter.py`
- Modify: `src/ui/workers/rust_dump_worker.py`
- Modify: `src/ui/dialogs/db_dialogs.py`
- Modify: `src/core/i18n.py`
- Modify: `tests/test_rust_dump_exporter.py`
- Modify: `tests/test_db_dialogs.py`
- Modify: `tests/test_i18n.py`

- [ ] Add `strict_manifest` parameter forwarding to `RustDumpImporter.import_dump`.
- [ ] Enable limited dumps only through an explicit confirmation dialog.
- [ ] Pass `strict_manifest=false` for limited Import and `true` for strict Import.
- [ ] Keep `not_restorable` blocked.
- [ ] Add plain Korean UI text for strict, limited, and blocked states.

### Task 5: Verification and Report

**Files:**
- Modify or create: `reports/export_import_flow_review_20260602.html`

- [ ] Run focused Rust tests for snapshot strategy and dump manifest behavior.
- [ ] Run focused Python tests for Import UI and Rust Dump facade forwarding.
- [ ] Run full Rust tests and Python tests if focused tests pass.
- [ ] Create an HTML report with file URL summarizing root cause, fix, remaining operational prerequisites, and test evidence.
