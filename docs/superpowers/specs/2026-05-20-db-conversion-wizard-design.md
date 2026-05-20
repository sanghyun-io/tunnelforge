# DB Conversion Guided Wizard Design

## Goal

Redesign the MySQL <-> PostgreSQL DB conversion dialog so a general user can understand the workflow without knowing Rust Core command names. The existing Rust Core commands remain the execution backend; the PyQt UI becomes a guided wizard that explains what is happening, blocks unsafe execution by default, and requires explicit approval before changing the target database.

## Current Problem

The current `DB 전환` dialog exposes internal workflow commands directly: `inspect`, `readiness`, `preflight`, `guide`, `plan`, `migrate`, `resume`, and `verify`. It also shows normalized schema JSON in the main flow. This is technically powerful but unclear for normal users because they need to infer which button to press, why "양방향 점검" exists, and what the raw schema JSON means.

## UX Direction

Use a guided, step-by-step wizard similar in spirit to the MySQL 8.0 -> 8.4 migration flow, but with a stricter final approval gate.

The default flow is:

1. 연결 선택
2. Source 구조 분석
3. 전환 가능 여부 점검
4. 실행 계획 확인
5. 승인 및 전환 실행
6. 검증 및 결과 저장

The UI should not expose a default `전체 실행` button. Users proceed with `이전` and `다음`, and actual DB changes require an explicit approval screen.

## Screen Structure

The dialog title becomes `DB 전환 마법사`.

The top of the dialog shows the selected one-way direction, for example:

`MySQL tf_source84 -> PostgreSQL public`

The left side contains a vertical step list:

- `1. 연결 선택`
- `2. Source 구조 분석`
- `3. 전환 가능 여부 점검`
- `4. 실행 계획 확인`
- `5. 승인 및 전환 실행`
- `6. 검증 및 결과 저장`

The right side shows only the current step contents. The bottom action row shows default controls such as `이전`, `다음`, and `취소`. Advanced controls are available only inside contextual `고급 설정` expanders in each step.

## Direction And Readiness Policy

The default workflow validates only the selected source-to-target direction. If the user chooses MySQL as Source and PostgreSQL as Target, the UI should only present MySQL -> PostgreSQL readiness.

The current "양방향 점검" concept should not appear as a primary user-facing step. If reverse-direction diagnostic information remains useful for development, it belongs in advanced diagnostics, not the default wizard.

The user-facing check is named `전환 가능 여부 점검` or `실행 전 안전 점검`.

This step verifies:

- The selected A engine can be converted to the selected B engine.
- Source and Target connection payloads are valid.
- Source schema inspection succeeded.
- Target database/schema is absent or empty by default.
- Blocking issues are absent.

## Target Schema Safety

By default, DB conversion can proceed only when the target schema does not exist or exists but contains no tables/data relevant to the conversion.

If the target schema already contains tables or data, the wizard blocks progress and shows actions:

- `새 schema 선택`
- `Target 비우고 다시 점검`
- `고급 설정 열기`

Advanced options may allow riskier modes:

- Delete/recreate target schema or tables.
- Append/merge into existing target data.
- Resume a previously interrupted run.

These options are not part of the default path and must require explicit user intent.

## Step Behavior

### 1. 연결 선택

The user selects Source and Target connections. The UI clearly states the one-way conversion direction. The wizard does not run reverse-direction checks in the default flow.

### 2. Source 구조 분석

Rust Core `inspect` runs automatically when the user proceeds.

The default screen hides normalized schema JSON and shows a readable summary:

- Table count.
- Estimated rows, when available.
- Primary key, foreign key, and index summary.
- Unsupported objects such as views, triggers, functions, or procedures.
- Important conversion notes.

Advanced settings expose:

- Schema JSON view.
- Schema JSON import/export.
- Raw Rust Core inspect result.
- Table selection, if supported by the backend flow.

### 3. 전환 가능 여부 점검

This step combines the practical parts of readiness and preflight into one user-facing safety screen.

It shows:

- Current direction support status.
- Target empty/non-empty status.
- Blocking issues.
- Warnings.
- Unsupported objects that may require manual handling.

The user can proceed only when there are no blocking issues, or when an advanced explicitly approved mode allows continuation.

### 4. 실행 계획 확인

The plan screen is summary-first, but any conversion that changes data shape or semantics must be visible.

Priority order:

1. Conversion content.
2. Safety status.
3. Workload/performance information.

The main plan highlights meaningful transformations, including:

- Unsigned numeric widening, such as `int unsigned` -> `bigint`.
- `tinyint(1)` boolean handling.
- `datetime` / `timestamp` handling.
- `decimal(p,s)` precision handling.
- `auto_increment` -> identity behavior.
- `enum` / `set` conversion strategy.
- `json` -> `jsonb`.
- `blob` / binary -> `bytea`.
- MySQL index prefix length handling.
- FK/index creation order, especially post-data creation.

The screen provides `계획 저장`.

Advanced settings expose:

- Full type mapping list.
- Generated DDL preview.
- Raw plan JSON.
- FK/index creation order details.

### 5. 승인 및 전환 실행

Actual DB changes are always gated.

The user must type the target schema name exactly before the execute button becomes enabled. For example, if the target schema is `public`, the user must type `public`.

During execution, the progress view prioritizes:

1. Current table and chunk details.
2. Human-readable Rust Core log.
3. Overall progress.

It should show:

- Current table.
- Current chunk / total chunks when available.
- Current table rows.
- Current table speed.
- Failed table/retry state.
- Overall completed tables and rows.

Raw Rust event JSON remains hidden behind advanced details.

If execution fails or is interrupted, the wizard shows `재개` only when a resume state exists.

### 6. 검증 및 결과 저장

Verification happens after conversion execution. It checks whether Source and Target data actually match.

The default verification mode is strict:

- Table existence.
- Row count.
- Primary-key based row matching.
- Canonical value comparison.
- Missing, duplicate, and mismatched rows.
- Structural confirmation for keys and indexes where relevant.

Fast row-count-only or sample verification may exist as advanced options, but strict verification is the default.

If verification fails, the first visible information should be actual mismatch examples:

- Table.
- Key.
- Column.
- Source value.
- Target value.
- Difference type.

After examples, show summary counts and suggested next actions.

## Advanced Controls

Advanced features are contextual and hidden inside each step's `고급 설정` expander. The wizard should not use a global "advanced mode" as the primary way to understand the flow.

Examples:

- Schema JSON view/import/export in Source analysis.
- Non-empty target handling in safety check.
- Full DDL and raw plan JSON in plan review.
- Resume state and raw Rust events in execution.
- Verification mode and mismatch example limit in verification.

## Error Handling

Target non-empty:

- Block by default.
- Explain that the target schema contains existing tables/data.
- Offer safe next actions and advanced options.

Unsupported objects:

- Show them during safety check.
- Separate blocking issues from manual follow-up objects.

Conversion notes:

- Show meaningful type/shape changes as "확인 필요", not necessarily as errors.

Execution failure:

- Show current table, chunk, and error.
- Show failed table list.
- Show resume when available.
- Keep raw logs in advanced details.

Verification failure:

- Show concrete mismatch examples first.
- Then show failed table and row counts.
- Then show likely cause and next-step guidance.

## Testing Plan

Add or update focused tests for:

- Step navigation and state transitions.
- No default full-run path.
- Source schema JSON hidden by default and available in advanced details.
- Source analysis summary rendering.
- Current-direction-only readiness display.
- Target non-empty default block.
- Advanced target handling visibility.
- Execute button disabled until exact target schema name is entered.
- Execution progress prioritizing current table/chunk before overall progress.
- Resume button visible only when resume state exists.
- Strict verification default.
- Verification mismatch examples rendered before aggregate summaries.
- Existing Rust Core payload compatibility for inspect, preflight, plan, migrate, resume, and verify.

## Out Of Scope

This design does not change Rust Core migration semantics. It reorganizes the PyQt workflow around the existing Rust Core commands and may add UI-level helpers for summaries, gating, and presentation. Performance tuning for data movement is outside this UX redesign.
