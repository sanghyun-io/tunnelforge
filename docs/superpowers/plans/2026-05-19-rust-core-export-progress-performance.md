# Rust Core Export Progress Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Rust Core full-schema export faster for large MySQL tables and make the export dialog's progress/logs accurate and diagnosable.

**Architecture:** Rust Core will emit an initial dump plan with table row totals, then use PK range parallel export for large eligible MySQL tables even inside full-schema exports. Python will aggregate row progress per table into one monotonic overall progress bar and save telemetry summaries in export logs.

**Tech Stack:** Rust `migration_core`, Python PyQt6 dialog/workers, pytest, cargo test.

---

## Files

- Modify: `migration_core/src/lib.rs`
  - Add dump planning telemetry.
  - Reuse PK range parallel export for eligible MySQL tables inside full-schema export.
  - Add strategy/chunk timing metadata consistently.
- Modify: `src/exporters/rust_dump_exporter.py`
  - Preserve richer raw event fields.
  - Forward dump plan and row telemetry to UI detail callbacks.
  - Stop treating table `dumping` events as completed table progress.
- Modify: `src/ui/workers/rust_dump_worker.py`
  - Continue forwarding detail/raw/table status events.
- Modify: `src/ui/dialogs/db_dialogs.py`
  - Add export progress state.
  - Make the main progress bar overall row based and monotonic.
  - Separate completed table count from current table status.
  - Save telemetry and slow table summaries in export logs.
- Modify: `tests/test_rust_dump_exporter.py`
  - Add contract tests for dump plan and row telemetry forwarding.
- Create or modify Rust tests in `migration_core/src/lib.rs`
  - Add unit coverage for PK range full export eligibility and dump plan event shape.

---

### Task 1: Rust Dump Plan Event

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Add a unit test for dump plan event shape**

Add a test near existing dump tests:

```rust
#[test]
fn dump_plan_event_reports_table_and_row_totals() {
    let schema = NormalizedSchema {
        tables: vec![
            NormalizedTable {
                name: "users".to_string(),
                columns: vec![NormalizedColumn {
                    name: "id".to_string(),
                    data_type: "int".to_string(),
                    nullable: false,
                    default: None,
                    primary_key: true,
                    unique: false,
                    auto_increment: false,
                }],
                indexes: vec![],
                foreign_keys: vec![],
            },
        ],
        unsupported_objects: vec![],
    };
    let mut counts = BTreeMap::new();
    counts.insert("users".to_string(), 42_u64);

    let event = dump_plan_event(Some("req-1".to_string()), &schema.tables, &counts);

    assert_eq!(event["event"], "dump_plan");
    assert_eq!(event["request_id"], "req-1");
    assert_eq!(event["tables_total"], 1);
    assert_eq!(event["rows_total"], 42);
    assert_eq!(event["tables"][0]["name"], "users");
    assert_eq!(event["tables"][0]["rows"], 42);
}
```

- [ ] **Step 2: Run the focused Rust test and verify it fails**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_plan_event_reports_table_and_row_totals
```

Expected: compile failure because `dump_plan_event` is not defined.

- [ ] **Step 3: Implement `dump_plan_event` and row count collection**

Add helper functions near dump helpers:

```rust
fn dump_table_row_counts(endpoint: &Endpoint, tables: &[NormalizedTable]) -> BTreeMap<String, u64> {
    let mut counts = BTreeMap::new();
    if let Ok(mut adapter) = LiveAdapter::connect(endpoint) {
        for table in tables {
            let count = adapter.row_count(&table.name).unwrap_or(0).max(0) as u64;
            counts.insert(table.name.clone(), count);
        }
    }
    counts
}

fn dump_plan_event(
    request_id: Option<String>,
    tables: &[NormalizedTable],
    row_counts: &BTreeMap<String, u64>,
) -> Value {
    let rows_total = tables
        .iter()
        .map(|table| row_counts.get(&table.name).copied().unwrap_or(0))
        .sum::<u64>();
    json!({
        "event": "dump_plan",
        "request_id": request_id,
        "tables_total": tables.len(),
        "rows_total": rows_total,
        "tables": tables.iter().map(|table| {
            json!({
                "name": table.name,
                "rows": row_counts.get(&table.name).copied().unwrap_or(0)
            })
        }).collect::<Vec<_>>()
    })
}
```

In `dump_run`, after the final table list is known and before export workers start:

```rust
let row_counts = dump_table_row_counts(&endpoint, &schema.tables);
emit(dump_plan_event(
    request.request_id.clone(),
    &schema.tables,
    &row_counts,
));
```

- [ ] **Step 4: Run the focused Rust test and verify it passes**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml dump_plan_event_reports_table_and_row_totals
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

Run:

```powershell
git add migration_core/src/lib.rs
git commit -m "feat: emit Rust dump plan telemetry"
```

---

### Task 2: Full Export PK Range Parallelism

**Files:**
- Modify: `migration_core/src/lib.rs`

- [ ] **Step 1: Add unit tests for large table range eligibility**

Add tests near `numeric_single_primary_key_is_parallel_dump_eligible`:

```rust
#[test]
fn large_numeric_pk_table_is_range_dump_candidate() {
    let table = NormalizedTable {
        name: "events".to_string(),
        columns: vec![NormalizedColumn {
            name: "id".to_string(),
            data_type: "bigint".to_string(),
            nullable: false,
            default: None,
            primary_key: true,
            unique: false,
            auto_increment: false,
        }],
        indexes: vec![],
        foreign_keys: vec![],
    };

    assert!(should_use_pk_range_dump(&table, 200_000, 50_000));
}

#[test]
fn small_numeric_pk_table_stays_whole_table_candidate() {
    let table = NormalizedTable {
        name: "small_events".to_string(),
        columns: vec![NormalizedColumn {
            name: "id".to_string(),
            data_type: "bigint".to_string(),
            nullable: false,
            default: None,
            primary_key: true,
            unique: false,
            auto_increment: false,
        }],
        indexes: vec![],
        foreign_keys: vec![],
    };

    assert!(!should_use_pk_range_dump(&table, 10_000, 50_000));
}
```

- [ ] **Step 2: Run focused tests and verify failure**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml range_dump_candidate
```

Expected: compile failure because `should_use_pk_range_dump` is not defined.

- [ ] **Step 3: Implement range eligibility helper**

Add:

```rust
fn should_use_pk_range_dump(table: &NormalizedTable, row_count: u64, chunk_size: usize) -> bool {
    let threshold = (chunk_size as u64).saturating_mul(2);
    row_count >= threshold && single_numeric_primary_key(table).is_some()
}
```

- [ ] **Step 4: Refactor range dump helper to support any table index**

Create a helper based on `dump_single_mysql_table_parallel`:

```rust
fn dump_mysql_table_parallel_ranges<F: FnMut(Value)>(
    endpoint: &Endpoint,
    output_path: &Path,
    table: &NormalizedTable,
    index: usize,
    table_total: usize,
    chunk_size: usize,
    data_format: &str,
    threads: usize,
    request_id: Option<String>,
    mut emit: F,
) -> Result<Option<(DumpTableManifest, u64, u64)>, String> {
    // Same logic as dump_single_mysql_table_parallel, but:
    // - table_path is format!("{:04}_{}", index + 1, safe_dump_component(&table.name))
    // - table_progress current is index + 1 and total is table_total
    // - return one DumpTableManifest instead of Vec<DumpTableManifest>
    // - row_progress rows value is cumulative rows_dumped after each completed range
}
```

When implementing this step, ensure the `DumpRangeEvent::Done` path emits cumulative row progress or updates incoming row progress before UI sees it. The UI must not receive per-chunk `rows` as if it were table cumulative progress.

- [ ] **Step 5: Use range helper from full-schema table workers**

Change `spawn_dump_table_worker` to accept `threads: usize`. In the worker:

```rust
if endpoint.engine == "mysql" {
    if let Some((manifest, rows, chunks)) = dump_mysql_table_parallel_ranges(
        &endpoint,
        &output_path,
        &table,
        index,
        table_total,
        chunk_size,
        &data_format,
        threads,
        request_id.clone(),
        |event| {
            let _ = sender.send(DumpTableEvent::Progress(event));
        },
    )? {
        return Ok((manifest, rows, chunks));
    }
}
```

Update all `spawn_dump_table_worker` call sites to pass `threads`.

- [ ] **Step 6: Preserve single-table public behavior**

Update `dump_single_mysql_table_parallel` to call `dump_mysql_table_parallel_ranges` with `index = 0` and convert the manifest into the existing `Vec<DumpTableManifest>` result.

- [ ] **Step 7: Run Rust tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml
```

Expected: all Rust tests pass.

- [ ] **Step 8: Commit Task 2**

Run:

```powershell
git add migration_core/src/lib.rs
git commit -m "feat: parallelize large MySQL tables in full exports"
```

---

### Task 3: Python Event Forwarding Contracts

**Files:**
- Modify: `src/exporters/rust_dump_exporter.py`
- Modify: `tests/test_rust_dump_exporter.py`

- [ ] **Step 1: Add tests for dump plan forwarding**

Add a test:

```python
def test_emit_core_event_forwards_dump_plan_to_detail_callback():
    from src.exporters.rust_dump_exporter import emit_core_event

    details = []
    emit_core_event(
        {
            "event": "dump_plan",
            "tables_total": 2,
            "rows_total": 150,
            "tables": [{"name": "a", "rows": 100}, {"name": "b", "rows": 50}],
        },
        detail_callback=details.append,
    )

    assert details == [{
        "event": "dump_plan",
        "tables_total": 2,
        "rows_total": 150,
        "tables": [{"name": "a", "rows": 100}, {"name": "b", "rows": 50}],
    }]
```

- [ ] **Step 2: Add tests that dumping table events do not count as completed**

Add:

```python
def test_emit_core_event_counts_only_completed_table_progress():
    from src.exporters.rust_dump_exporter import emit_core_event

    table_progress = []
    statuses = []

    emit_core_event(
        {"event": "table_progress", "table": "users", "status": "dumping", "current": 1, "total": 2},
        table_progress_callback=lambda current, total, table: table_progress.append((current, total, table)),
        table_status_callback=lambda table, status, message: statuses.append((table, status, message)),
    )
    emit_core_event(
        {"event": "table_progress", "table": "users", "status": "completed", "current": 1, "total": 2},
        table_progress_callback=lambda current, total, table: table_progress.append((current, total, table)),
        table_status_callback=lambda table, status, message: statuses.append((table, status, message)),
    )

    assert table_progress == [(1, 2, "users")]
    assert statuses == [("users", "loading", ""), ("users", "done", "")]
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```powershell
python -m pytest tests\test_rust_dump_exporter.py -q
```

Expected: new tests fail before implementation.

- [ ] **Step 4: Implement event forwarding**

In `emit_core_event`:

```python
if event_type == "dump_plan":
    if detail_callback:
        detail_callback({
            "event": "dump_plan",
            "tables_total": int(event.get("tables_total") or 0),
            "rows_total": int(event.get("rows_total") or 0),
            "tables": event.get("tables") if isinstance(event.get("tables"), list) else [],
        })
    return
```

For `table_progress`, call `table_progress_callback` only when `status == "completed"`:

```python
if table_progress_callback and status == "completed":
    table_progress_callback(current, total, table)
```

Keep `table_status_callback` behavior unchanged.

- [ ] **Step 5: Run tests and verify pass**

Run:

```powershell
python -m pytest tests\test_rust_dump_exporter.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

Run:

```powershell
git add src/exporters/rust_dump_exporter.py tests/test_rust_dump_exporter.py
git commit -m "fix: clarify Rust dump event forwarding"
```

---

### Task 4: Export Dialog Overall Progress State

**Files:**
- Modify: `src/ui/dialogs/db_dialogs.py`

- [ ] **Step 1: Add UI state initialization**

In `RustDumpExportDialog.__init__`, add:

```python
self.export_table_totals: dict = {}
self.export_table_done: dict = {}
self.export_table_status: dict = {}
self.export_total_rows: int = 0
self.export_completed_tables: int = 0
self.export_total_tables: int = 0
self.export_last_percent: int = 0
self.export_telemetry_events: List[dict] = []
self.export_table_started_at: dict = {}
self.export_table_finished_at: dict = {}
```

In `do_export`, reset these fields before starting the worker.

- [ ] **Step 2: Update initial labels**

Set:

```python
self.label_percent.setText("📊 전체 진행률: 0%")
self.label_data.setText("📦 데이터: 0 / 0 rows")
self.label_speed.setText("⚡ 속도: 0 rows/s")
self.label_tables.setText("📋 테이블: 0 / 0 완료")
self.label_status.setText("Export 준비 중...")
```

- [ ] **Step 3: Implement dump plan handling in `on_detail_progress`**

At the top of `on_detail_progress`:

```python
if info.get("event") == "dump_plan":
    tables = info.get("tables") or []
    self.export_table_totals = {
        str(item.get("name")): int(item.get("rows") or 0)
        for item in tables
        if item.get("name")
    }
    self.export_table_done = {name: 0 for name in self.export_table_totals}
    self.export_total_rows = int(info.get("rows_total") or sum(self.export_table_totals.values()))
    self.export_total_tables = int(info.get("tables_total") or len(self.export_table_totals))
    self.label_tables.setText(f"📋 테이블: 0 / {self.export_total_tables} 완료")
    self.label_data.setText(f"📦 데이터: 0 / {self.export_total_rows:,} rows")
    self.label_status.setText("Export 계획 수립 완료")
    return
```

- [ ] **Step 4: Make row progress overall and monotonic**

Continue `on_detail_progress` with:

```python
table = str(info.get("table") or "")
if table:
    rows_done = int(info.get("rows_done") or 0)
    table_total = int(info.get("rows_total") or 0)
    if table_total and table not in self.export_table_totals:
        self.export_table_totals[table] = table_total
        self.export_total_rows = max(self.export_total_rows, sum(self.export_table_totals.values()))
    if table_total:
        rows_done = min(rows_done, table_total)
    self.export_table_done[table] = max(self.export_table_done.get(table, 0), rows_done)

overall_done = sum(self.export_table_done.values())
if self.export_total_rows > 0:
    computed_percent = int((overall_done / self.export_total_rows) * 100)
else:
    computed_percent = int(info.get("percent") or 0)
percent = max(self.export_last_percent, min(computed_percent, 100))
self.export_last_percent = percent
self.progress_bar.setMaximum(100)
self.progress_bar.setValue(percent)
self.label_percent.setText(f"📊 전체 진행률: {percent}%")
self.label_data.setText(f"📦 데이터: {overall_done:,} / {self.export_total_rows:,} rows")
self.label_speed.setText(f"⚡ 속도: {info.get('speed', 'Rust DB Core')}")
if table:
    table_total = self.export_table_totals.get(table) or int(info.get("rows_total") or 0)
    table_percent = int((self.export_table_done.get(table, 0) / table_total) * 100) if table_total else 0
    self.label_status.setText(f"🔄 {table} ({table_percent}%)")
```

- [ ] **Step 5: Stop table progress from touching the main bar**

Replace `on_table_progress` body with:

```python
self.export_completed_tables = max(self.export_completed_tables, current)
self.export_total_tables = max(self.export_total_tables, total)
self.label_tables.setText(f"📋 테이블: {self.export_completed_tables} / {self.export_total_tables} 완료")
self._add_log(f"테이블 완료: {table_name} ({self.export_completed_tables}/{self.export_total_tables})")
```

Do not call `self.progress_bar.setMaximum(total)` or `self.progress_bar.setValue(current)`.

- [ ] **Step 6: Record table status timings**

In `on_table_status`, set start/end timestamps:

```python
now = datetime.now()
if status == "loading":
    self.export_table_started_at.setdefault(table_name, now)
elif status in ("done", "error"):
    self.export_table_finished_at[table_name] = now
```

- [ ] **Step 7: Commit Task 4**

Run:

```powershell
git add src/ui/dialogs/db_dialogs.py
git commit -m "fix: make export progress overall and monotonic"
```

---

### Task 5: Export Telemetry and Saved Log Summary

**Files:**
- Modify: `src/ui/dialogs/db_dialogs.py`

- [ ] **Step 1: Store sanitized raw events**

In `on_raw_output`, parse JSON lines:

```python
try:
    event = json.loads(line)
except Exception:
    event = None
if isinstance(event, dict) and event.get("event") in {"dump_plan", "row_progress", "table_progress"}:
    for key in ("password", "credentials"):
        event.pop(key, None)
    self.export_telemetry_events.append(event)
```

Do not append every raw JSON line to `log_entries`.

- [ ] **Step 2: Add summary helpers**

Add methods:

```python
def _export_table_duration_seconds(self, table_name: str) -> float:
    start = self.export_table_started_at.get(table_name)
    end = self.export_table_finished_at.get(table_name)
    if not start or not end:
        return 0.0
    return max(0.0, (end - start).total_seconds())

def _export_slow_table_summaries(self) -> List[dict]:
    summaries = []
    for table, total_rows in self.export_table_totals.items():
        summaries.append({
            "table": table,
            "rows": total_rows,
            "done": self.export_table_done.get(table, 0),
            "duration_sec": self._export_table_duration_seconds(table),
        })
    return sorted(summaries, key=lambda item: item["duration_sec"], reverse=True)
```

- [ ] **Step 3: Write summaries in `save_log`**

Before "상세 로그", write:

```python
f.write("\n" + "=" * 70 + "\n")
f.write("Export Telemetry Summary\n")
f.write("=" * 70 + "\n")
f.write(f"총 rows: {self.export_total_rows:,}\n")
f.write(f"완료 rows: {sum(self.export_table_done.values()):,}\n")
f.write(f"수집 이벤트: {len(self.export_telemetry_events):,}\n")
f.write("\n느린 테이블 Top 10\n")
for item in self._export_slow_table_summaries()[:10]:
    f.write(
        f"- {item['table']}: {item['duration_sec']:.1f}s, "
        f"{item['done']:,}/{item['rows']:,} rows\n"
    )
```

- [ ] **Step 4: Commit Task 5**

Run:

```powershell
git add src/ui/dialogs/db_dialogs.py
git commit -m "feat: save export telemetry summaries"
```

---

### Task 6: Verification and Live Evidence

**Files:**
- Verify only unless failures require fixes.

- [ ] **Step 1: Format Rust**

Run:

```powershell
cargo fmt --manifest-path migration_core\Cargo.toml
```

Expected: exit code 0.

- [ ] **Step 2: Rust tests**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml
```

Expected: all tests pass.

- [ ] **Step 3: Python focused tests**

Run:

```powershell
python -m pytest tests\test_rust_dump_exporter.py tests\test_db_dialogs.py tests\test_db_core_service.py
```

Expected: all tests pass.

- [ ] **Step 4: Full Python suite**

Run:

```powershell
python -m pytest
```

Expected: all tests pass.

- [ ] **Step 5: Regression gate**

Run:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\rust-core-regression-gate.ps1
```

Expected: `Rust Core regression gate passed.`

- [ ] **Step 6: Release build**

Run:

```powershell
cargo build --manifest-path migration_core\Cargo.toml --release
```

Expected: release build succeeds.

- [ ] **Step 7: Live synthetic MySQL full export check**

Use local MySQL 8.4 on `127.0.0.1:33406` if available. Create a synthetic database with two numeric PK tables, one large enough to split into multiple chunks. Run `dump.run` with `threads=4`, `chunk_size=50000`, and `data_format=tsv`.

Expected evidence:

- output manifest row counts match source row counts
- raw events include `dump_plan`
- large table row events include `strategy = pk_range_parallel`
- chunk files for the large table are created faster than sequential one-worker cadence

- [ ] **Step 8: Restart local app**

Run:

```powershell
$procs = Get-CimInstance Win32_Process | Where-Object { ($_.Name -match '^(python|pythonw|cmd|tunnelforge-core|migration-core)') -and (($_.CommandLine -like '*tunnelforge*') -or ($_.CommandLine -like '*main.py*')) }
$procs | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Process -FilePath "cmd.exe" -ArgumentList "/k cd /d C:\Users\QESG\sh-project\tunnelforge && python main.py" -WorkingDirectory "C:\Users\QESG\sh-project\tunnelforge"
```

Expected: TunnelForge starts and uses the rebuilt `tunnelforge-core.exe`.

---

## Completion Audit

Before declaring completion:

- [ ] Confirm the main progress bar is not updated from table ordinal progress.
- [ ] Confirm row progress is aggregated across all tables.
- [ ] Confirm displayed overall percent is monotonic.
- [ ] Confirm dump plan telemetry is emitted by Rust Core.
- [ ] Confirm large eligible MySQL tables in a full export use `pk_range_parallel`.
- [ ] Confirm saved export logs contain telemetry summary and slow table summary.
- [ ] Confirm no `mysqlsh` dependency was reintroduced.
- [ ] Confirm all verification commands in Task 6 passed.
