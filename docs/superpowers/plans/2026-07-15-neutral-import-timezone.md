# Neutral Import Timezone Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Import Auto preserve the target server/session timezone for MySQL and PostgreSQL while keeping UTC and KST as explicit choices.

**Architecture:** The behavior change stays in the PyQt dialog. Auto passes `timezone_sql=None`, which the existing worker/exporter omits from the Rust JSONL payload and the existing Rust Core interprets as no session SQL. No new protocol field or saved-setting migration is introduced.

**Tech Stack:** Python 3.9+, PyQt6, Rust `dump.import`, pytest, Cargo tests.

## Global Constraints

- Auto remains selected by default and emits no timezone-changing SQL.
- UTC and KST retain engine-specific explicit SQL.
- The duplicate None radio is removed; `resolve_timezone_sql(..., "none")` remains compatible.
- `mysql.time_zone_name` is never queried by Import Auto.
- New Korean UI text has exact English translations.
- Rust Core validation and DB ownership remain unchanged.

---

### Task 1: Neutral Import Auto Mode

**Files:**
- Modify: `src/ui/dialogs/db_import_dialog.py`
- Modify: `src/core/i18n/legacy_translate.py`
- Test: `tests/test_db_import_dialog.py`
- Test: `tests/test_rust_dump_exporter.py`
- Test: `tests/test_i18n.py`

**Interfaces:**

```python
def resolve_timezone_sql(engine: str, tz_mode: str) -> Optional[str]:
    # auto/none -> None
    # mysql kst/utc -> SET SESSION time_zone
    # postgresql kst/utc -> SET TIME ZONE
```

- [ ] **Step 1: Add failing MySQL Auto and UI-copy tests**

```python
def test_mysql_import_auto_preserves_server_session_timezone(dialog, worker_capture):
    dialog.radio_tz_auto.setChecked(True)
    dialog.start_import()
    assert worker_capture.kwargs["timezone_sql"] is None
    assert "mysql.time_zone_name" not in worker_capture.executed_queries


def test_auto_timezone_copy_describes_preservation(dialog):
    assert "서버/세션 기본값 유지" in dialog.radio_tz_auto.text()
    assert "자동 보정" not in dialog.radio_tz_auto.toolTip()


def test_timezone_group_has_no_duplicate_none_choice(dialog):
    assert not hasattr(dialog, "radio_tz_none")
```

Update tests that selected `radio_tz_none` to select the default Auto mode. Keep existing PostgreSQL Auto and explicit KST tests.

- [ ] **Step 2: Run RED UI tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_db_import_dialog.py -q`

Expected: MySQL Auto still probes timezone support and can synthesize `+09:00`; old copy and duplicate None radio remain.

- [ ] **Step 3: Implement the minimal dialog change**

```python
timezone_sql = None
if self.radio_tz_kst.isChecked():
    timezone_sql = resolve_timezone_sql(db_engine, "kst")
    self.txt_log.addItem("ℹ️ 타임존을 강제로 '+09:00' (KST)로 설정합니다.")
elif self.radio_tz_utc.isChecked():
    timezone_sql = resolve_timezone_sql(db_engine, "utc")
    self.txt_log.addItem("ℹ️ 타임존을 강제로 '+00:00' (UTC)로 설정합니다.")
else:
    self.txt_log.addItem("ℹ️ 서버/세션 기본 타임존을 유지합니다.")
```

Change the Auto label to `자동 (서버/세션 기본값 유지, 권장)`, replace its tooltip with a no-session-change statement, remove `radio_tz_none`, and remove `check_timezone_support` only after `rg` confirms no caller. Add exact English mappings for the Auto label, tooltip, and log line.

- [ ] **Step 4: Add failing payload-omission assertion**

```python
def test_import_dump_omits_timezone_sql_when_none(tmp_path):
    importer.import_dump(..., timezone_sql=None)
    assert "timezone_sql" not in facade.payload
```

- [ ] **Step 5: Run RED exporter test, then preserve existing omission behavior**

Run: `.venv\Scripts\python.exe -m pytest tests\test_rust_dump_exporter.py -q`

Expected: if omission is already covered by implementation, the new test may pass immediately; in that case record it as characterization evidence rather than claiming a RED production change. Do not modify the exporter unless the key is actually serialized as null.

- [ ] **Step 6: Run GREEN Python and Rust tests**

Run: `$env:QT_QPA_PLATFORM='offscreen'; .venv\Scripts\python.exe -m pytest tests\test_db_import_dialog.py tests\test_rust_dump_exporter.py tests\test_i18n.py -q`

Run: `cargo test --manifest-path migration_core\Cargo.toml import_timezone_sql_accepts_mysql_and_postgresql_timezone_forms --lib`

Run: `rg -n "check_timezone_support|mysql\.time_zone_name|radio_tz_none" src/ui/dialogs/db_import_dialog.py tests/test_db_import_dialog.py`

Expected: selected tests pass; production dialog has no support probe or duplicate option; Rust accepts omitted/explicit safe modes.

- [ ] **Step 7: Commit**

```powershell
git add src/ui/dialogs/db_import_dialog.py src/core/i18n/legacy_translate.py tests/test_db_import_dialog.py tests/test_rust_dump_exporter.py tests/test_i18n.py
git commit -m "Fix: preserve import session timezone by default"
```

## Self-Review

- Auto semantics are identical for MySQL and PostgreSQL.
- The change does not add a Rust option or settings migration.
- Explicit KST/UTC behavior remains test-covered.
- Visible text and payload omission are both regression-tested.
