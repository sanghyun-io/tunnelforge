# TunnelForge Safety and Proof Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement TF-STATUS-095 through TF-STATUS-101, pass all release gates, publish a Safety and Proof release, and prepare one disposable MySQL-over-SSH observation workflow.

**Architecture:** Rust `tunnelforge-core` remains the DB operation owner. Python owns SSH trust, UI approval, process orchestration, typed failure propagation, and release tooling. Each task has its own detailed TDD plan and is reviewed before the next task starts.

**Tech Stack:** Python 3.9+, PyQt6, Paramiko/sshtunnel, Rust/Cargo JSONL service, pytest, GitHub Actions, `uv pip compile`, pip hash-checking mode.

## Global Constraints

- Unknown SSH hosts require visible SHA-256 approval; changed keys fail closed.
- Import Auto emits no timezone-changing SQL; UTC and KST are explicit only.
- One-Click non-dry-run requires approval of the exact current-target plan.
- Timed-out mutations return typed `outcome_indeterminate` and are never retried automatically.
- Mismatched request IDs and incompatible protocol versions fail explicitly.
- Resume identity covers full endpoints and immutable plan; writes are atomic.
- Release dependency installation uses hashes on Windows and both macOS architectures.
- Analysis failure is distinct from successful zero findings.
- No direct Python DB mutation path, Apple App Store work, remote telemetry, support bundle, new engine, broad accessibility project, or automated rollback is added.

---

### Task 1: TF-STATUS-095 SSH Host Trust Core

**Detailed plan:** `docs/superpowers/plans/2026-07-15-ssh-host-trust.md`

**Exit evidence:** Unknown host stops before authentication/forwarding; approval persists host+port+algorithm+SHA-256 only; changed key cannot be overwritten in the connection flow; `SSHTunnelForwarder` and Paramiko preflight receive the exact freshly probed trusted key.

- [ ] RED trust store, platform path, and tunnel engine tests
- [ ] GREEN atomic trust store and pre-authentication probe
- [ ] Task review and focused regression

### Task 2: TF-STATUS-095 SSH First-Use Approval UX

**Detailed plan:** `docs/superpowers/plans/2026-07-15-ssh-host-trust.md`

**Exit evidence:** UI shows host:port, algorithm, and SHA-256 with default-No approval; worker threads never open QMessageBox; background paths never auto-approve; changed keys show a blocking error without bypass.

- [ ] RED MainWindow, connection dialog, and worker signal tests
- [ ] GREEN UI-thread approval and retry flow
- [ ] Task review and focused regression

### Task 3: TF-STATUS-096 Neutral Import Timezone

**Detailed plan to create before implementation:** `docs/superpowers/plans/2026-07-15-neutral-import-timezone.md`

**Exit evidence:** Auto is the default, promises server/session preservation, passes `timezone_sql=None` for MySQL and PostgreSQL, never queries `mysql.time_zone_name`, and has no duplicate None option. Explicit UTC/KST SQL and translations remain covered.

- [ ] RED UI/copy/payload tests
- [ ] GREEN dialog and translation changes
- [ ] Rust omission contract and focused regression

### Task 4: TF-STATUS-097 Disable Unsafe One-Click Apply

**Detailed plan to create before implementation:** `docs/superpowers/plans/2026-07-15-oneclick-plan-approval.md`

**Exit evidence:** One-Click non-dry-run cannot reach mutation until the bounded process and exact-plan contracts are complete. Dry-run remains available and the UI explains that apply is temporarily unavailable.

- [ ] RED UI/service tests proving non-dry-run currently reaches mutation
- [ ] GREEN fail-closed feature gate while preserving dry-run
- [ ] Task review and focused regression

### Task 5: TF-STATUS-098 Bounded DB Core Process Contract

**Detailed plan to create before implementation:** `docs/superpowers/plans/2026-07-15-db-core-process-contract.md`

**Exit evidence:** Deadline can interrupt a stalled stdout read; unusable generation is terminated/reaped; caller receives `outcome_indeterminate`; no command is resent; mismatched IDs and protocol versions fail; next generation can recover.

- [ ] RED stalled/mismatched/death/recovery tests
- [ ] GREEN one-reader bounded lifecycle
- [ ] Task review and service/protocol regression

### Task 6: TF-STATUS-097 One-Click Exact-Plan Approval

**Detailed plan:** `docs/superpowers/plans/2026-07-15-oneclick-plan-approval.md`

**Exit evidence:** A plan-only command returns a canonical, secret-free target identity containing engine, route, server UUID, authenticated user, schema, snapshot hash, ordered actions, and SHA-256 plan hash. Apply replans against current state and rejects missing or stale approvals before mutation; UI defaults No. Non-dry-run is re-enabled only after these checks pass through the bounded process contract.

- [ ] RED Rust protocol and PyQt plan/approve/replan sequencing tests
- [ ] GREEN canonical plan hash and exact-current-plan apply contract
- [ ] Re-enable non-dry-run, task review, and Rust/Python focused regression

### Task 7: TF-STATUS-099 Resume Identity, Atomicity, and Cancellation

**Detailed plan to create before implementation:** `docs/superpowers/plans/2026-07-15-resume-cancellation-contract.md`

**Exit evidence:** State key changes for any endpoint/table/plan change; envelope is versioned and secret-free; write is fsync+replace atomic; stale state fails explicitly; result/cancel publishes exactly one terminal state.

- [ ] RED identity/atomic/stale/terminal-state tests
- [ ] GREEN Python envelope and Rust validation
- [ ] Task review and migration regression

### Task 8: TF-STATUS-100 Hash-Locked Release Dependencies

**Detailed plan to create before implementation:** `docs/superpowers/plans/2026-07-15-release-dependency-lock.md`

**Exit evidence:** `requirements-release.txt` is generated from all extras for Python 3.11 using `uv pip compile --universal --generate-hashes`; release jobs install it with pip `--require-hashes --force-reinstall --no-cache-dir`; editable/ranged release installation is absent.

- [ ] RED lock/workflow/build-script tests
- [ ] GREEN universal lock and release install wiring
- [ ] Windows and both macOS resolver/install verification

### Task 9: TF-STATUS-101 Typed Analysis Failure

**Detailed plan to create before implementation:** `docs/superpowers/plans/2026-07-15-analysis-failure-contract.md`

**Exit evidence:** Strict analysis query result distinguishes success-empty from failure; analyzers stop with typed failure; workers do not emit clean analysis completion; UI does not offer report/save actions for failed evidence.

- [ ] RED connector/analyzer/worker/UI tests
- [ ] GREEN typed query and analysis outcome
- [ ] Task review and migration-analysis regression

### Task 10: Safety and Proof Release and Observation Workflow

**Detailed plan to create after Tasks 1-9:** `docs/superpowers/plans/2026-07-15-safety-release-and-study.md`

**Exit evidence:** Focused and full Python, Rust, Worker, packaging, clean installer, status, and diff gates pass; TF-STATUS-095 through 101 close with fresh evidence; protected release path publishes verified assets; facilitator workflow contains no credentials and records 3-5 independent session outcomes without treating downloads as users.

- [ ] Full local and hosted release gates
- [ ] Protected version/tag/draft/publication sequence
- [ ] Disposable MySQL-over-SSH workflow and 3-5 session records

## Self-Review

- Every proposal issue TF-STATUS-095 through TF-STATUS-101 has an implementation task and exit evidence.
- Immediate safety work precedes product evidence collection.
- No task expands the Rust/Python ownership boundary or adds an explicit non-goal.
- The active detailed plan is Task 1-2 SSH trust; later detailed plans must be committed before their production edits begin.
