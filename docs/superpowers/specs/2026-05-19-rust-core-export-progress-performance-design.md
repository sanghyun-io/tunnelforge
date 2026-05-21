# Rust Core Export Progress and Performance Design

## Context

TunnelForge now routes dump export/import through the Rust DB Core. A real full-schema export of `dataflare` on May 19, 2026 showed:

- 208 tables
- 8,870,087 rows
- 323 TSV chunks
- 3.21 GB dump output
- 11 minutes 50 seconds total runtime

The slowest tables were:

| Table | Rows | Size | Chunks | Observed Duration |
| --- | ---: | ---: | ---: | ---: |
| `qe_view_factors_result` | 1,946,153 | 491.73 MB | 39 | 366s |
| `df_subs` | 387,398 | 1.30 GB | 8 | 245s |
| `df_call_logs` | 1,076,142 | 388.83 MB | 22 | 135s |
| `sub_value_change_log` | 920,204 | 269.69 MB | 19 | 118s |
| `attachment_storage` | 643,747 | 93.47 MB | 13 | 97s |

The saved UI log did not include raw `row_progress` telemetry, so detailed per-chunk timings were inferred from chunk file write times and table progress logs. The UI also showed inconsistent progress because the same `QProgressBar` is updated by both table progress and row progress.

## Goals

1. Make the export dialog's primary progress indicator mean "overall export progress".
2. Ensure the primary progress value never decreases during a run.
3. Separate overall progress, table completion, current table progress, speed, and diagnostics.
4. Improve full-schema export performance by applying PK range parallelism to large eligible tables, not only single-table exports.
5. Remove long-tail behavior where one large table monopolizes a worker after other workers finish.
6. Save enough telemetry in export logs to diagnose future slow exports without requiring raw UI screenshots.

## Non-Goals

- Do not reintroduce `mysqlsh`.
- Do not change the dump manifest format incompatibly.
- Do not change the user's selected output folder structure.
- Do not require users to understand row/chunk internals to use the dialog.
- Do not redesign the whole export wizard.

## UI Design

The export dialog will show one primary progress bar:

- Label: `전체 진행률`
- Meaning: total completed rows divided by known total rows across all exported tables.
- Rule: monotonic. If a late event reports lower progress, the displayed value remains at the previous maximum.

Separate labels will show:

- `테이블`: completed tables / total tables.
- `현재`: active table name and current table progress.
- `속도`: recent rows/sec, preferably a short moving average.
- `처리`: active worker count and active chunk/table count when available.

The primary progress bar will not be updated by table ordinal values. Table progress will only update the table label and table list.

The table list will distinguish:

- pending
- exporting
- completed
- failed

It will not label an `exporting` table as completed.

## Progress Data Model

Python UI state will track progress per table:

- `table_total_rows[table]`
- `table_done_rows[table]`
- `table_status[table]`
- `completed_tables`
- `total_tables`
- `last_overall_percent`

Rust DB Core will emit an initial export planning event before row export begins. That event will include the selected table names and known row totals. The UI will use it to initialize `table_total_rows` and `overall_total_rows`. This avoids deriving "overall progress" from only the currently active table.

On each `row_progress` event:

1. Update the table's done rows using the event's `rows` value.
2. Clamp table done rows to the table total when known.
3. Recompute overall rows as the sum of all table done rows.
4. Compute overall percent as `overall_done_rows / overall_total_rows`.
5. Display `max(previous_percent, computed_percent)`.

If total rows are unknown for one or more tables, the UI will temporarily show an indeterminate or "preparing" state until enough table totals are known. Fallback table-weighted progress may be used only when row totals are unavailable.

## Rust Core Telemetry

Rust DB Core will emit richer progress events for dump exports:

- `event`
- `request_id`
- `tables_total` on planning/table events
- `rows_total` on planning/table events
- `table`
- `rows`
- `total`
- `chunk_rows`
- `chunk_index`
- `chunks_done`
- `chunks_total`
- `stream_ms`
- `read_ms` when separately measured
- `write_ms` when separately measured
- `strategy`
- `worker_id` when applicable

The Python layer will pass these fields through to UI state and saved logs. Credentials must not appear in telemetry.

## Export Scheduling Design

The current full-schema export uses table-level parallelism. Single-table PK range parallel export exists, but it is only selected when the export contains exactly one table. This causes large PK tables to be exported sequentially inside one worker during a full-schema export.

The new scheduler will use a work queue that can contain:

- whole-table tasks for small or non-range-eligible tables
- PK range chunk tasks for large range-eligible MySQL tables

A MySQL table is range-eligible when:

- it has exactly one primary key column
- the primary key type is numeric
- min/max PK inspection succeeds
- row count is above a configurable threshold

Large eligible tables will be split into contiguous PK ranges. These range tasks will be scheduled alongside normal table tasks so that all export workers stay busy until the queue is empty.

Each table will still produce the same table directory and `chunk_*.tsv` files. Chunk indices must remain unique and deterministic within the table manifest.

## Ordering and Correctness

Dump export does not insert into a target database, so FK parent-first ordering is not needed for export performance. The manifest table order should remain dependency-ordered for import compatibility.

Range tasks must not duplicate or skip rows:

- PK ranges must be contiguous.
- Range boundaries must be inclusive in a way that partitions the min/max span exactly once.
- Each chunk output file must map to one range.
- Final manifest row counts must equal the sum of completed range rows.

Tables without numeric single-column PKs will continue to use the existing sequential keyset or offset path.

## Logging Design

Saved export logs will include:

- final summary: rows, tables, chunks, bytes, elapsed time
- top slow tables by elapsed time
- top large tables by bytes
- per-table summary: rows, chunks, bytes, duration, strategy
- slow chunk samples when telemetry is available

Raw JSONL events should not flood the visible UI, but they should be available in the saved log or a companion diagnostic file. The visible log may stay concise.

## Error Handling

If range splitting fails for an eligible table, the scheduler will fall back to the existing sequential table export and log the fallback reason.

If one range worker fails, the export fails with the table name, chunk/range identity, and sanitized error message.

If telemetry fields are missing from older Rust Core versions, the UI will degrade gracefully to table-based progress instead of crashing.

## Testing

Rust unit tests:

- range partitioning does not overlap or skip
- scheduler creates range tasks for large numeric PK tables
- scheduler leaves small/non-eligible tables as whole-table tasks
- manifest row/chunk aggregation is correct
- telemetry contains strategy and chunk timing fields

Python tests:

- primary progress bar uses only overall row progress
- primary progress never decreases
- table progress no longer changes the progress bar maximum/value
- current table progress is shown separately
- saved export log includes slow table summaries and telemetry

Integration/performance checks:

- MySQL 8.4 full-schema or synthetic multi-table export with at least one large numeric-PK table uses range tasks inside full export
- large table long-tail is reduced compared with sequential full export
- row counts and chunk counts match the manifest after export
- `scripts/rust-core-regression-gate.ps1` still passes
- `cargo test --manifest-path migration_core\Cargo.toml` passes
- `python -m pytest` passes

## Acceptance Criteria

- The export dialog has a single primary progress meaning: overall row-based progress.
- The primary progress value does not decrease during export.
- Table completion and current table progress are visually separate.
- Saved logs identify which table/chunk caused a slow export.
- Full-schema export can use PK range parallelism for large eligible MySQL tables.
- The `dataflare`-style long-tail case no longer leaves a single large eligible table running sequentially after other workers finish.
- Existing TSV dump/import compatibility is preserved.
