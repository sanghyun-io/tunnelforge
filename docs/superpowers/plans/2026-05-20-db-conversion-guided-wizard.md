# DB Conversion Guided Wizard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current DB conversion command panel with a guided one-way MySQL/PostgreSQL conversion wizard that keeps Rust Core as the execution backend and requires explicit target-schema approval before any target DB changes.

**Architecture:** Keep `CrossEngineMigrationDialog` as the public dialog class and preserve existing Rust Core command wiring through `CrossEngineMigrationWorker`. Add UI state helpers, summary renderers, and safety gates inside the dialog so Python remains orchestration-only while Rust Core still owns inspect, preflight, plan, migrate, resume, and verify.

**Tech Stack:** Python 3.9+, PyQt6 widgets/signals, existing Rust Core JSONL protocol helpers in `src/core/cross_engine_migration.py`, pytest with offscreen Qt.

---

## Scope Check

This is one subsystem: the PyQt DB conversion dialog and its focused tests. The plan does not alter Rust Core migration semantics, exporter/importer performance paths, packaging, or external database connectivity.

## File Structure

- Modify `src/ui/dialogs/cross_engine_migration_dialog.py`
  - Owns the guided wizard layout, step navigation, readable summaries, safety gate, execution approval input, progress presentation, and verification result presentation.
  - Keeps existing `EndpointForm`, `_payload`, `_start_command_with_payload`, worker event wiring, resume-state save/load, and Rust Core command names.
- Modify `tests/test_cross_engine_migration_dialog.py`
  - Updates current command-panel expectations to wizard expectations.
  - Adds tests for one-way safety display, hidden schema JSON, approval typing, progress rendering, and verification mismatch examples.
- Optional modify `src/core/cross_engine_migration.py`
  - Only if a pure helper is needed for deriving the selected `MigrationDirection`; prefer using the existing `MigrationDirection.from_engines` enum directly from the dialog.

## Implementation Notes

Preserve these compatibility points while changing the visible UI:

- Keep `btn_inspect`, `btn_preflight`, `btn_plan`, `btn_migrate`, `btn_resume`, `btn_verify`, `btn_save_report`, `btn_cancel`, and `btn_close` as attributes so current tests and callers can still access them during the transition.
- Keep `btn_full_run` as an attribute but hide it and remove it from the default flow.
- Keep `txt_schema` as the normalized schema storage source for `_payload`, but hide it by default behind advanced schema details.
- Keep `txt_log` as the raw/human log sink, but make the default execution step show human-readable progress labels before raw JSON.
- Do not introduce direct Python DB driver access.

---

### Task 1: Add Wizard Step State And Layout Shell

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing test**

Add this test near the initial dialog-state tests in `tests/test_cross_engine_migration_dialog.py`:

```python
def test_dialog_starts_as_guided_wizard_without_full_run_button():
    dialog = make_dialog()
    try:
        assert dialog.windowTitle() == "DB 전환 마법사"
        assert dialog.current_step_id == "connections"
        assert dialog.step_titles == [
            "1. 연결 선택",
            "2. Source 구조 분석",
            "3. 전환 가능 여부 점검",
            "4. 실행 계획 확인",
            "5. 승인 및 전환 실행",
            "6. 검증 및 결과 저장",
        ]
        assert not dialog.btn_full_run.isVisible()
        assert dialog.btn_previous.text() == "이전"
        assert dialog.btn_next.text() == "다음"
        assert dialog.lbl_direction_summary.text() == "MySQL source_db -> PostgreSQL target_db"
    finally:
        dialog.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_dialog_starts_as_guided_wizard_without_full_run_button -q
```

Expected: FAIL because `current_step_id`, `step_titles`, `btn_previous`, `btn_next`, and `lbl_direction_summary` do not exist yet, and the title is still `DB 전환`.

- [ ] **Step 3: Write the minimal implementation**

In `src/ui/dialogs/cross_engine_migration_dialog.py`, extend imports:

```python
from typing import Dict, List, Optional
```

Add this near the start of `CrossEngineMigrationDialog.__init__`, before `_setup_ui()`:

```python
self.step_ids = [
    "connections",
    "inspect",
    "safety",
    "plan",
    "execute",
    "verify",
]
self.step_titles = [
    "1. 연결 선택",
    "2. Source 구조 분석",
    "3. 전환 가능 여부 점검",
    "4. 실행 계획 확인",
    "5. 승인 및 전환 실행",
    "6. 검증 및 결과 저장",
]
self.current_step_id = self.step_ids[0]
self.step_pages: Dict[str, QWidget] = {}
```

Change the title line:

```python
self.setWindowTitle("DB 전환 마법사")
```

Add these helper methods inside `CrossEngineMigrationDialog`:

```python
def _current_step_index(self) -> int:
    return self.step_ids.index(self.current_step_id)

def _direction_label(self) -> str:
    source_engine = "MySQL" if self.source_form.engine() == DatabaseEngine.MYSQL else "PostgreSQL"
    target_engine = "MySQL" if self.target_form.engine() == DatabaseEngine.MYSQL else "PostgreSQL"
    source_schema = self.source_form.input_schema.text().strip() or self.source_form.input_database.text().strip()
    target_schema = self.target_form.input_schema.text().strip() or self.target_form.input_database.text().strip()
    return f"{source_engine} {source_schema} -> {target_engine} {target_schema}"

def _refresh_direction_summary(self):
    if hasattr(self, "lbl_direction_summary"):
        self.lbl_direction_summary.setText(self._direction_label())

def _show_step(self, step_id: str):
    self.current_step_id = step_id
    for page_id, page in self.step_pages.items():
        page.setVisible(page_id == step_id)
    if hasattr(self, "btn_previous"):
        self.btn_previous.setEnabled(self._current_step_index() > 0)
    if hasattr(self, "btn_next"):
        is_last = self._current_step_index() == len(self.step_ids) - 1
        self.btn_next.setText("완료" if is_last else "다음")
    self._refresh_direction_summary()

def _go_previous_step(self):
    index = self._current_step_index()
    if index > 0:
        self._show_step(self.step_ids[index - 1])

def _go_next_step(self):
    index = self._current_step_index()
    if index >= len(self.step_ids) - 1:
        self.close()
        return
    self._show_step(self.step_ids[index + 1])
```

Inside `_setup_ui`, after endpoint forms are created and added, add the direction label:

```python
self.lbl_direction_summary = QLabel()
self.lbl_direction_summary.setStyleSheet("font-weight: 600;")
layout.addWidget(self.lbl_direction_summary)
```

After creating `self.btn_full_run`, hide it:

```python
self.btn_full_run.hide()
```

After creating `self.btn_close`, create and connect navigation buttons:

```python
self.btn_previous = QPushButton("이전")
self.btn_next = QPushButton("다음")
self.btn_previous.clicked.connect(self._go_previous_step)
self.btn_next.clicked.connect(self._go_next_step)
```

Add them to `control_layout` before cancel/close:

```python
control_layout.addWidget(self.btn_previous)
control_layout.addWidget(self.btn_next)
```

At the end of `_setup_ui`, after `_sync_target_engine_filter()`, add:

```python
self.source_form.input_schema.textChanged.connect(self._refresh_direction_summary)
self.source_form.input_database.textChanged.connect(self._refresh_direction_summary)
self.target_form.input_schema.textChanged.connect(self._refresh_direction_summary)
self.target_form.input_database.textChanged.connect(self._refresh_direction_summary)
self._show_step("connections")
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_dialog_starts_as_guided_wizard_without_full_run_button -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: add DB conversion wizard shell"
```

---

### Task 2: Replace Visible Command Strip With Step Pages

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing tests**

Replace the visible command button assertions in `test_dialog_initial_button_states_and_running_toggle` and add this test:

```python
def test_wizard_navigation_preserves_payload_and_step_controls():
    dialog = make_dialog()
    try:
        assert dialog.current_step_id == "connections"
        assert dialog.btn_previous.isEnabled() is False
        assert dialog.btn_next.isEnabled() is True

        dialog._go_next_step()

        assert dialog.current_step_id == "inspect"
        assert dialog.btn_previous.isEnabled() is True
        assert dialog._payload()["guide_options"]["row_limit"] == 20

        dialog._go_previous_step()

        assert dialog.current_step_id == "connections"
        assert dialog.btn_previous.isEnabled() is False
    finally:
        dialog.close()
```

Update `test_dialog_initial_button_states_and_running_toggle` so it checks hidden command compatibility instead of expecting visible command-strip behavior:

```python
def test_dialog_initial_button_states_and_running_toggle():
    dialog = make_dialog()
    try:
        assert not dialog.btn_save_report.isEnabled()
        assert not dialog.btn_cancel.isEnabled()
        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_full_run.isVisible()
        assert not dialog.source_form.combo_engine.isEnabled()
        assert not dialog.target_form.combo_engine.isEnabled()
        assert dialog._payload()["guide_options"]["row_limit"] == 20
        assert dialog._payload()["source"]["schema"] == "source_db"
        assert dialog._payload()["target"]["database"] == "postgres"
        assert dialog._payload()["target"]["schema"] == "target_db"
        assert dialog.target_form.combo_tunnel.count() == 2
        assert "PostgreSQL" in dialog.target_form.combo_tunnel.itemText(1)

        dialog._set_running(True)

        assert not dialog.btn_inspect.isEnabled()
        assert not dialog.btn_preflight.isEnabled()
        assert not dialog.btn_plan.isEnabled()
        assert not dialog.btn_migrate.isEnabled()
        assert dialog.btn_cancel.isEnabled()
        assert not dialog.btn_next.isEnabled()

        dialog._set_running(False)

        assert dialog.btn_inspect.isEnabled()
        assert dialog.btn_preflight.isEnabled()
        assert dialog.btn_plan.isEnabled()
        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_cancel.isEnabled()
        assert dialog.btn_next.isEnabled()
    finally:
        dialog.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_wizard_navigation_preserves_payload_and_step_controls tests/test_cross_engine_migration_dialog.py::test_dialog_initial_button_states_and_running_toggle -q
```

Expected: FAIL until running state disables wizard navigation and step pages exist.

- [ ] **Step 3: Write the implementation**

In `_setup_ui`, replace the always-visible schema/log/action stack with simple page containers. Use existing widgets inside the relevant pages instead of deleting them.

Add after `self.lbl_direction_summary`:

```python
self.page_container = QWidget()
self.page_layout = QVBoxLayout(self.page_container)
layout.addWidget(self.page_container, 1)
```

Create pages before adding the action group:

```python
for step_id in self.step_ids:
    page = QWidget()
    page_layout = QVBoxLayout(page)
    page_layout.setContentsMargins(0, 0, 0, 0)
    self.step_pages[step_id] = page
    self.page_layout.addWidget(page)
```

Move endpoint layout to the connections page:

```python
self.step_pages["connections"].layout().addLayout(endpoint_layout)
```

Move `option_group` to the plan page:

```python
self.step_pages["plan"].layout().addWidget(option_group)
```

Move `schema_group` to the inspect page:

```python
self.step_pages["inspect"].layout().addWidget(schema_group, 1)
```

Move `txt_log` to the execute page for now:

```python
self.step_pages["execute"].layout().addWidget(self.txt_log, 1)
```

Keep `action_group` visible under the page container. Hide the internal command groups but keep their buttons connected:

```python
step_check.hide()
step_prepare.hide()
step_execute.hide()
step_verify.hide()
```

Add this method:

```python
def _next_enabled_for_current_step(self) -> bool:
    if self.worker and self.worker.isRunning():
        return False
    if self.current_step_id == "execute":
        return self._execution_unlocked and self._approval_matches_target_schema()
    return True
```

Update `_show_step`:

```python
def _show_step(self, step_id: str):
    self.current_step_id = step_id
    for page_id, page in self.step_pages.items():
        page.setVisible(page_id == step_id)
    if hasattr(self, "btn_previous"):
        self.btn_previous.setEnabled(self._current_step_index() > 0 and not (self.worker and self.worker.isRunning()))
    if hasattr(self, "btn_next"):
        is_last = self._current_step_index() == len(self.step_ids) - 1
        self.btn_next.setText("완료" if is_last else "다음")
        self.btn_next.setEnabled(self._next_enabled_for_current_step())
    self._refresh_direction_summary()
```

For this task, add a temporary approval helper that returns `True`; Task 6 replaces it with typed approval:

```python
def _approval_matches_target_schema(self) -> bool:
    return True
```

Update `_set_running` to include navigation buttons:

```python
if hasattr(self, "btn_previous"):
    self.btn_previous.setEnabled((not running) and self._current_step_index() > 0)
if hasattr(self, "btn_next"):
    self.btn_next.setEnabled((not running) and self._next_enabled_for_current_step())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_wizard_navigation_preserves_payload_and_step_controls tests/test_cross_engine_migration_dialog.py::test_dialog_initial_button_states_and_running_toggle -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: organize DB conversion into wizard steps"
```

---

### Task 3: Hide Schema JSON By Default And Render Source Analysis Summary

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing tests**

Add these tests:

```python
def test_inspect_result_shows_readable_source_summary_and_hides_json_by_default():
    dialog = make_dialog()
    schema = {
        "tables": [
            {
                "name": "users",
                "columns": [
                    {"name": "id", "type": "int", "nullable": False, "primary_key": True},
                    {"name": "email", "type": "varchar(255)", "nullable": False},
                ],
                "indexes": [{"name": "idx_users_email"}],
                "foreign_keys": [],
            },
            {
                "name": "orders",
                "columns": [{"name": "user_id", "type": "int", "foreign_key": True}],
                "indexes": [],
                "foreign_keys": [{"name": "fk_orders_users"}],
            },
        ]
    }
    try:
        assert not dialog.txt_schema.isVisible()

        dialog._on_result({
            "event": "result",
            "command": "inspect",
            "success": True,
            "schema": schema,
            "unsupported_objects": ["view:active_users"],
        })

        summary = dialog.lbl_source_summary.text()
        assert "테이블 2개" in summary
        assert "컬럼 3개" in summary
        assert "인덱스 1개" in summary
        assert "FK 1개" in summary
        assert "지원 제외 1개" in summary
        assert json.loads(dialog.txt_schema.toPlainText()) == schema

        dialog.chk_show_schema_json.setChecked(True)

        assert dialog.txt_schema.isVisible()
    finally:
        dialog.close()
```

Update `test_inspect_result_enables_report_and_updates_schema` to assert the source summary:

```python
assert "테이블 1개" in dialog.lbl_source_summary.text()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_inspect_result_shows_readable_source_summary_and_hides_json_by_default tests/test_cross_engine_migration_dialog.py::test_inspect_result_enables_report_and_updates_schema -q
```

Expected: FAIL because `lbl_source_summary` and `chk_show_schema_json` do not exist.

- [ ] **Step 3: Write the implementation**

In `_setup_ui`, inside `schema_group` before `self.txt_schema`, add:

```python
self.lbl_source_summary = QLabel("아직 Source 구조를 분석하지 않았습니다.")
self.lbl_source_summary.setWordWrap(True)
schema_layout.addWidget(self.lbl_source_summary)

self.chk_show_schema_json = QCheckBox("고급 설정: Normalized schema JSON 보기")
self.chk_show_schema_json.setChecked(False)
schema_layout.addWidget(self.chk_show_schema_json)
```

After creating `self.txt_schema`, hide it and connect the checkbox:

```python
self.txt_schema.setVisible(False)
self.chk_show_schema_json.toggled.connect(self.txt_schema.setVisible)
```

Add these helper methods:

```python
def _schema_summary_text(self, schema: Dict, unsupported_objects: List[str]) -> str:
    tables = schema.get("tables") if isinstance(schema.get("tables"), list) else []
    table_count = len(tables)
    column_count = 0
    index_count = 0
    foreign_key_count = 0
    for table in tables:
        if not isinstance(table, dict):
            continue
        columns = table.get("columns") if isinstance(table.get("columns"), list) else []
        indexes = table.get("indexes") if isinstance(table.get("indexes"), list) else []
        foreign_keys = table.get("foreign_keys") if isinstance(table.get("foreign_keys"), list) else []
        column_count += len(columns)
        index_count += len(indexes)
        foreign_key_count += len(foreign_keys)
    return (
        f"테이블 {table_count}개, 컬럼 {column_count}개, "
        f"인덱스 {index_count}개, FK {foreign_key_count}개, "
        f"지원 제외 {len(unsupported_objects)}개"
    )

def _update_source_summary(self, schema: Dict):
    unsupported = [str(item) for item in self.unsupported_objects]
    self.lbl_source_summary.setText(self._schema_summary_text(schema, unsupported))
```

In `_on_result`, after `self.unsupported_objects` is updated, call summary update when schema exists. Replace the schema block tail with:

```python
if schema is not None:
    self.txt_schema.setPlainText(json.dumps(schema, ensure_ascii=False, indent=2))
    self._set_execution_unlocked(False)
    table_count = len(schema.get("tables", [])) if isinstance(schema.get("tables"), list) else 0
    self.lbl_schema_status.setText(f"Rust Core 검사 완료: {table_count}개 테이블")
    self._append_log("스키마 검사 결과를 입력에 반영했습니다.")
```

After the unsupported object block:

```python
if schema is not None:
    self._update_source_summary(schema)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_inspect_result_shows_readable_source_summary_and_hides_json_by_default tests/test_cross_engine_migration_dialog.py::test_inspect_result_enables_report_and_updates_schema -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: summarize source analysis in DB conversion wizard"
```

---

### Task 4: Show One-Way Safety Check And Block Non-Empty Target By Default

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing tests**

Replace `test_readiness_result_logs_direction_summary` with:

```python
def test_readiness_result_shows_only_selected_direction_summary():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "readiness",
            "success": False,
            "directions": [
                {
                    "direction": "mysql_to_postgresql",
                    "success": True,
                    "table_count": 3,
                    "issues": [{"blocking": False, "message": "index prefix requires review"}],
                },
                {
                    "direction": "postgresql_to_mysql",
                    "success": False,
                    "table_count": 2,
                    "issues": [{"blocking": True, "message": "reverse issue"}],
                },
            ],
        })

        text = dialog.lbl_safety_summary.text()
        log = dialog.txt_log.toPlainText()
        assert "MySQL -> PostgreSQL 가능" in text
        assert "warnings=1" in text
        assert "postgresql_to_mysql" not in text
        assert "reverse issue" not in log
    finally:
        dialog.close()
```

Add this test:

```python
def test_preflight_blocks_execution_when_target_is_not_empty():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "preflight",
            "success": False,
            "issues": [
                {
                    "severity": "error",
                    "location": "target.public",
                    "message": "target schema is not empty",
                    "blocking": True,
                }
            ],
        })

        assert not dialog.btn_migrate.isEnabled()
        assert "Target에 기존 테이블 또는 데이터가 있습니다" in dialog.lbl_target_safety.text()
        assert dialog.btn_target_advanced.isVisible()
    finally:
        dialog.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_readiness_result_shows_only_selected_direction_summary tests/test_cross_engine_migration_dialog.py::test_preflight_blocks_execution_when_target_is_not_empty -q
```

Expected: FAIL because the dialog still logs both directions and has no safety summary widgets.

- [ ] **Step 3: Write the implementation**

Add imports:

```python
from src.core.cross_engine_migration import (
    DatabaseEngine,
    MigrationDirection,
    load_resume_state,
    next_workflow_command,
    make_connection_payload,
    render_result_report,
    save_resume_state,
    schema_from_inspect_result,
    state_key_from_payload,
)
```

In `_setup_ui`, add a safety page group:

```python
self.safety_group = QGroupBox("전환 가능 여부 점검")
safety_layout = QVBoxLayout(self.safety_group)
self.lbl_safety_summary = QLabel("아직 전환 가능 여부를 점검하지 않았습니다.")
self.lbl_safety_summary.setWordWrap(True)
self.lbl_target_safety = QLabel("Target 상태를 아직 확인하지 않았습니다.")
self.lbl_target_safety.setWordWrap(True)
self.btn_target_advanced = QPushButton("고급 설정 열기")
self.btn_target_advanced.hide()
self.btn_run_safety = QPushButton("전환 가능 여부 점검")
self.btn_run_safety.clicked.connect(lambda: self._start_command("preflight"))
safety_layout.addWidget(self.lbl_safety_summary)
safety_layout.addWidget(self.lbl_target_safety)
safety_layout.addWidget(self.btn_target_advanced)
safety_layout.addWidget(self.btn_run_safety)
self.step_pages["safety"].layout().addWidget(self.safety_group)
```

Add these helpers:

```python
def _selected_direction(self) -> MigrationDirection:
    return MigrationDirection.from_engines(self.source_form.engine(), self.target_form.engine())

def _direction_display(self, direction: MigrationDirection) -> str:
    if direction == MigrationDirection.MYSQL_TO_POSTGRESQL:
        return "MySQL -> PostgreSQL"
    return "PostgreSQL -> MySQL"

def _selected_direction_result(self, payload: Dict) -> Optional[Dict]:
    directions = payload.get("directions")
    if not isinstance(directions, list):
        return None
    selected = self._selected_direction().value
    for direction in directions:
        if isinstance(direction, dict) and direction.get("direction") == selected:
            return direction
    return None

def _issue_counts(self, issues) -> Dict[str, int]:
    blocking_count = 0
    warning_count = 0
    if isinstance(issues, list):
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("blocking"):
                blocking_count += 1
            else:
                warning_count += 1
    return {"blocking": blocking_count, "warnings": warning_count}

def _is_target_non_empty_issue(self, issue: Dict) -> bool:
    location = str(issue.get("location", "")).lower()
    message = str(issue.get("message", "")).lower()
    return "target" in location and ("not empty" in message or "non-empty" in message or "existing" in message)

def _update_target_safety_from_issues(self, issues) -> bool:
    if not isinstance(issues, list):
        self.lbl_target_safety.setText("Target 상태를 확인했습니다.")
        self.btn_target_advanced.hide()
        return False
    for issue in issues:
        if isinstance(issue, dict) and issue.get("blocking") and self._is_target_non_empty_issue(issue):
            self.lbl_target_safety.setText(
                "Target에 기존 테이블 또는 데이터가 있습니다. 새 schema를 선택하거나 고급 설정에서 명시적으로 처리 방식을 선택해야 합니다."
            )
            self.btn_target_advanced.show()
            return True
    self.lbl_target_safety.setText("Target이 비어 있거나 새 schema로 판단되어 기본 전환 조건을 만족합니다.")
    self.btn_target_advanced.hide()
    return False
```

Replace `_append_readiness_summary` with:

```python
def _append_readiness_summary(self, payload: Dict):
    direction = self._selected_direction_result(payload)
    if not direction:
        self.lbl_safety_summary.setText("선택한 Source -> Target 방향의 점검 결과를 찾지 못했습니다.")
        return
    counts = self._issue_counts(direction.get("issues"))
    status = "가능" if direction.get("success") else "불가"
    display = self._direction_display(self._selected_direction())
    self.lbl_safety_summary.setText(
        f"{display} {status} "
        f"(tables={direction.get('table_count', 0)}, "
        f"blocking={counts['blocking']}, warnings={counts['warnings']})"
    )
    self._append_log("[전환 가능 여부 점검]")
    self._append_log(self.lbl_safety_summary.text())
```

In `_on_result`, inside the `if payload.get("command") in ("preflight", "plan"):` block, before `_set_execution_unlocked`, add:

```python
target_blocked = self._update_target_safety_from_issues(payload.get("issues"))
```

Then change the unlock call:

```python
self._set_execution_unlocked(bool(payload.get("success")) and not target_blocked)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_readiness_result_shows_only_selected_direction_summary tests/test_cross_engine_migration_dialog.py::test_preflight_blocks_execution_when_target_is_not_empty -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: add one-way DB conversion safety check"
```

---

### Task 5: Render Execution Plan As Human Summary

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing test**

Add this test:

```python
def test_plan_result_renders_meaningful_conversion_changes():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "plan",
            "success": True,
            "plan": {
                "tables": [
                    {"name": "users", "estimated_rows": 1000},
                    {"name": "orders", "estimated_rows": 2500},
                ],
                "type_mappings": [
                    {
                        "table": "users",
                        "column": "id",
                        "source_type": "int unsigned",
                        "target_type": "bigint",
                        "note": "unsigned widening",
                    },
                    {
                        "table": "users",
                        "column": "payload",
                        "source_type": "json",
                        "target_type": "jsonb",
                        "note": "json normalization",
                    },
                ],
                "ddl_order": ["create tables", "load data", "create foreign keys"],
            },
            "issues": [{"blocking": False, "message": "index prefix length converted"}],
        })

        text = dialog.lbl_plan_summary.text()
        assert "전환 대상 테이블 2개" in text
        assert "예상 rows 3,500" in text
        assert "int unsigned -> bigint" in text
        assert "json -> jsonb" in text
        assert "FK/index는 데이터 적재 후 생성" in text
    finally:
        dialog.close()
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_plan_result_renders_meaningful_conversion_changes -q
```

Expected: FAIL because `lbl_plan_summary` does not exist.

- [ ] **Step 3: Write the implementation**

In `_setup_ui`, add a plan page group before adding `option_group` to the plan page:

```python
self.plan_group = QGroupBox("실행 계획 확인")
plan_layout = QVBoxLayout(self.plan_group)
self.lbl_plan_summary = QLabel("아직 실행 계획을 생성하지 않았습니다.")
self.lbl_plan_summary.setWordWrap(True)
self.btn_run_plan = QPushButton("계획 생성")
self.btn_run_plan.clicked.connect(lambda: self._start_command("plan"))
plan_layout.addWidget(self.lbl_plan_summary)
plan_layout.addWidget(self.btn_run_plan)
self.step_pages["plan"].layout().addWidget(self.plan_group)
```

Add these helpers:

```python
def _plan_tables(self, payload: Dict) -> List[Dict]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    tables = plan.get("tables") if isinstance(plan.get("tables"), list) else []
    return [table for table in tables if isinstance(table, dict)]

def _plan_type_mappings(self, payload: Dict) -> List[Dict]:
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    mappings = plan.get("type_mappings") if isinstance(plan.get("type_mappings"), list) else []
    return [mapping for mapping in mappings if isinstance(mapping, dict)]

def _plan_summary_text(self, payload: Dict) -> str:
    tables = self._plan_tables(payload)
    mappings = self._plan_type_mappings(payload)
    estimated_rows = 0
    for table in tables:
        try:
            estimated_rows += int(table.get("estimated_rows") or table.get("rows") or 0)
        except (TypeError, ValueError):
            continue
    lines = [
        f"전환 대상 테이블 {len(tables)}개",
        f"예상 rows {estimated_rows:,}",
    ]
    important = []
    for mapping in mappings:
        source_type = str(mapping.get("source_type", "")).strip()
        target_type = str(mapping.get("target_type", "")).strip()
        if source_type and target_type:
            important.append(f"{source_type} -> {target_type}")
    if important:
        lines.append("확인 필요: " + ", ".join(important[:8]))
    plan = payload.get("plan") if isinstance(payload.get("plan"), dict) else {}
    ddl_order = plan.get("ddl_order") if isinstance(plan.get("ddl_order"), list) else []
    ddl_text = " ".join(str(item).lower() for item in ddl_order)
    if "foreign" in ddl_text or "fk" in ddl_text:
        lines.append("FK/index는 데이터 적재 후 생성")
    return "\n".join(lines)

def _update_plan_summary(self, payload: Dict):
    self.lbl_plan_summary.setText(self._plan_summary_text(payload))
```

In `_on_result`, add:

```python
if payload.get("command") == "plan":
    self._update_plan_summary(payload)
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_plan_result_renders_meaningful_conversion_changes -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: summarize DB conversion execution plan"
```

---

### Task 6: Require Exact Target Schema Approval And Improve Execution Progress

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing tests**

Replace `test_migrate_command_requires_confirmation` with:

```python
def test_execute_requires_exact_target_schema_text_before_migrate(monkeypatch):
    dialog = make_dialog()
    started = []
    dialog._set_execution_unlocked(True)
    monkeypatch.setattr(
        dialog,
        "_start_command_with_payload",
        lambda *args, **kwargs: started.append((args, kwargs)),
    )

    try:
        dialog._show_step("execute")

        assert not dialog.btn_migrate.isEnabled()
        assert not dialog.btn_next.isEnabled()

        dialog.input_approval_schema.setText("wrong")

        assert not dialog.btn_migrate.isEnabled()

        dialog.input_approval_schema.setText("target_db")

        assert dialog.btn_migrate.isEnabled()
        dialog._start_command("migrate")

        assert started
        assert started[0][0][0] == "migrate"
    finally:
        dialog.close()
```

Add this test:

```python
def test_execution_progress_prioritizes_current_table_and_chunk():
    dialog = make_dialog()
    try:
        dialog._on_phase_changed("copy", "copying data")
        dialog._on_table_progress("users", "running")
        dialog._on_row_progress("users", 5000, 20000)

        assert "users" in dialog.lbl_current_table.text()
        assert "5,000 / 20,000 rows" in dialog.lbl_current_rows.text()
        assert "copying data" in dialog.lbl_execution_phase.text()
    finally:
        dialog.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_execute_requires_exact_target_schema_text_before_migrate tests/test_cross_engine_migration_dialog.py::test_execution_progress_prioritizes_current_table_and_chunk -q
```

Expected: FAIL because the approval input and progress labels do not exist.

- [ ] **Step 3: Write the implementation**

In `_setup_ui`, add execution widgets before `self.txt_log` is added to the execute page:

```python
self.execution_group = QGroupBox("승인 및 전환 실행")
execution_layout = QVBoxLayout(self.execution_group)
self.lbl_execution_warning = QLabel("대상 schema 이름을 정확히 입력해야 DB 변경 실행이 활성화됩니다.")
self.lbl_execution_warning.setWordWrap(True)
self.input_approval_schema = QLineEdit()
self.input_approval_schema.setPlaceholderText("Target schema 이름 입력")
self.lbl_execution_phase = QLabel("실행 전")
self.lbl_current_table = QLabel("현재 테이블: -")
self.lbl_current_rows = QLabel("현재 rows: -")
execution_layout.addWidget(self.lbl_execution_warning)
execution_layout.addWidget(self.input_approval_schema)
execution_layout.addWidget(self.lbl_execution_phase)
execution_layout.addWidget(self.lbl_current_table)
execution_layout.addWidget(self.lbl_current_rows)
self.step_pages["execute"].layout().addWidget(self.execution_group)
self.input_approval_schema.textChanged.connect(self._update_execution_state_from_approval)
```

Replace `_approval_matches_target_schema`:

```python
def _approval_matches_target_schema(self) -> bool:
    expected = self.target_form.input_schema.text().strip() or self.target_form.input_database.text().strip()
    if not expected:
        expected = "public" if self.target_form.engine() == DatabaseEngine.POSTGRESQL else self.target_form.input_database.text().strip()
    return self.input_approval_schema.text().strip() == expected
```

Add:

```python
def _update_execution_state_from_approval(self):
    self._update_execution_state(bool(self.worker and self.worker.isRunning()))
    if hasattr(self, "btn_next"):
        self.btn_next.setEnabled(self._next_enabled_for_current_step())
```

Replace `_confirm_migration_execution`:

```python
def _confirm_migration_execution(self) -> bool:
    if self._approval_matches_target_schema():
        return True
    QMessageBox.warning(
        self,
        "승인 필요",
        "Target schema 이름을 정확히 입력해야 DB 변경을 실행할 수 있습니다.",
    )
    return False
```

Update `_update_execution_state` so approval controls the button:

```python
def _update_execution_state(self, running: bool):
    approved = self._approval_matches_target_schema() if hasattr(self, "input_approval_schema") else True
    can_execute = (not running) and self._execution_unlocked and approved
    if hasattr(self, "btn_migrate"):
        self.btn_migrate.setEnabled(can_execute)
    if hasattr(self, "lbl_execution_lock"):
        if self._execution_unlocked:
            self.lbl_execution_lock.setText("점검이 통과되었습니다. Target schema 이름 입력 후 DB 변경 실행을 사용할 수 있습니다.")
        else:
            self.lbl_execution_lock.setText("DB 변경 실행은 사전 점검 또는 계획 생성 성공 후 활성화됩니다.")
    if hasattr(self, "btn_next"):
        self.btn_next.setEnabled(self._next_enabled_for_current_step())
```

Add progress methods:

```python
def _on_phase_changed(self, phase: str, message: str):
    self.lbl_execution_phase.setText(message or phase)
    self._append_log(f"[phase:{phase}] {message}")

def _on_table_progress(self, table: str, status: str):
    self.lbl_current_table.setText(f"현재 테이블: {table} ({status})")
    self._append_log(f"[table:{table}] {status}")

def _on_row_progress(self, table: str, rows: int, total):
    total_text = f"{int(total):,}" if total is not None else "?"
    self.lbl_current_rows.setText(f"현재 rows: {int(rows):,} / {total_text} rows")
    self._append_log(f"[rows:{table}] {rows}/{total if total is not None else '?'}")
```

Update `_start_command_with_payload` signal wiring:

```python
self.worker.phase_changed.connect(self._on_phase_changed)
self.worker.table_progress.connect(self._on_table_progress)
self.worker.row_progress.connect(self._on_row_progress)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_execute_requires_exact_target_schema_text_before_migrate tests/test_cross_engine_migration_dialog.py::test_execution_progress_prioritizes_current_table_and_chunk -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: require schema approval before DB conversion"
```

---

### Task 7: Render Strict Verification Results With Mismatch Examples First

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Write the failing tests**

Add these tests:

```python
def test_payload_uses_strict_verification_by_default():
    dialog = make_dialog()
    try:
        payload = dialog._payload()
        assert payload["verify_options"]["mode"] == "strict"
        assert payload["verify_options"]["mismatch_limit"] == 20
    finally:
        dialog.close()
```

```python
def test_verify_result_shows_mismatch_examples_before_summary():
    dialog = make_dialog()
    try:
        dialog._on_result({
            "event": "result",
            "command": "verify",
            "success": False,
            "mismatches": [
                {
                    "table": "users",
                    "key": "id=7",
                    "column": "email",
                    "source_value": "a@example.com",
                    "target_value": "b@example.com",
                    "difference": "value_mismatch",
                }
            ],
            "row_count_differences": [{"table": "orders", "source_rows": 10, "target_rows": 9}],
        })

        text = dialog.txt_verify_result.toPlainText()
        mismatch_index = text.index("users / id=7 / email")
        summary_index = text.index("orders: source 10, target 9")
        assert mismatch_index < summary_index
    finally:
        dialog.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_payload_uses_strict_verification_by_default tests/test_cross_engine_migration_dialog.py::test_verify_result_shows_mismatch_examples_before_summary -q
```

Expected: FAIL because `verify_options` and `txt_verify_result` do not exist.

- [ ] **Step 3: Write the implementation**

In `_setup_ui`, add a verification page group:

```python
self.verify_group = QGroupBox("검증 및 결과 저장")
verify_layout = QVBoxLayout(self.verify_group)
self.lbl_verify_mode = QLabel("기본 검증: strict row/key/value 비교")
self.txt_verify_result = QPlainTextEdit()
self.txt_verify_result.setReadOnly(True)
self.txt_verify_result.setPlaceholderText("검증 실행 후 mismatch 예시와 요약이 표시됩니다.")
self.btn_run_verify = QPushButton("검증")
self.btn_run_verify.clicked.connect(lambda: self._start_command("verify"))
verify_layout.addWidget(self.lbl_verify_mode)
verify_layout.addWidget(self.btn_run_verify)
verify_layout.addWidget(self.txt_verify_result, 1)
self.step_pages["verify"].layout().addWidget(self.verify_group, 1)
```

Update `_payload` by adding `verify_options`:

```python
"verify_options": {
    "mode": "strict",
    "mismatch_limit": 20,
},
```

Add helpers:

```python
def _verification_result_text(self, payload: Dict) -> str:
    lines = []
    mismatches = payload.get("mismatches")
    if isinstance(mismatches, list) and mismatches:
        lines.append("Mismatch 예시")
        for mismatch in mismatches[:20]:
            if not isinstance(mismatch, dict):
                continue
            table = mismatch.get("table", "")
            key = mismatch.get("key", "")
            column = mismatch.get("column", "")
            source_value = mismatch.get("source_value", "")
            target_value = mismatch.get("target_value", "")
            difference = mismatch.get("difference", "")
            lines.append(f"- {table} / {key} / {column}: source={source_value}, target={target_value}, type={difference}")
    row_diffs = payload.get("row_count_differences")
    if isinstance(row_diffs, list) and row_diffs:
        lines.append("")
        lines.append("Row count 차이")
        for diff in row_diffs:
            if not isinstance(diff, dict):
                continue
            lines.append(
                f"- {diff.get('table', '')}: source {diff.get('source_rows', 0)}, target {diff.get('target_rows', 0)}"
            )
    if not lines:
        lines.append("검증 통과: Source와 Target 데이터가 일치합니다.")
    return "\n".join(lines)

def _update_verification_result(self, payload: Dict):
    self.txt_verify_result.setPlainText(self._verification_result_text(payload))
```

In `_on_result`, add:

```python
if payload.get("command") == "verify":
    self._update_verification_result(payload)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py::test_payload_uses_strict_verification_by_default tests/test_cross_engine_migration_dialog.py::test_verify_result_shows_mismatch_examples_before_summary -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "feat: show strict DB conversion verification results"
```

---

### Task 8: Clean Up Workflow Compatibility And Run Regression Tests

**Files:**
- Modify: `src/ui/dialogs/cross_engine_migration_dialog.py`
- Modify: `tests/test_cross_engine_migration_dialog.py`

- [ ] **Step 1: Update any remaining test expectations**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py -q
```

Expected: FAIL only on tests that still expect command-panel wording such as `양방향 점검`, visible `전체 실행`, or QMessageBox-only migration confirmation.

For each failing assertion, update it to the wizard behavior:

```python
assert "[전환 가능 여부 점검]" in dialog.txt_log.toPlainText()
assert not dialog.btn_full_run.isVisible()
assert "Target schema 이름" in dialog.lbl_execution_lock.text()
```

- [ ] **Step 2: Run the focused dialog suite**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_dialog.py -q
```

Expected: PASS with all DB conversion dialog tests green.

- [ ] **Step 3: Run neighboring Python migration tests**

Run:

```powershell
python -m pytest tests/test_cross_engine_migration_protocol.py tests/test_cross_engine_migration_worker.py tests/test_rust_dump_exporter.py -q
```

Expected: PASS. These tests confirm the dialog work did not break Rust Core request building, worker JSONL parsing, or Rust dump/exporter facade behavior.

- [ ] **Step 4: Run full Python tests**

Run:

```powershell
python -m pytest -q
```

Expected: PASS. At the latest baseline this suite had 1535 passing tests; a higher count is expected after adding wizard tests.

- [ ] **Step 5: Run Rust Core tests if any payload contract changed**

Run:

```powershell
cargo test --manifest-path migration_core\Cargo.toml
```

Expected: PASS. If the implementation only changes UI rendering and adds `verify_options` ignored by older commands, Rust tests should still pass without Rust source edits.

- [ ] **Step 6: Check for forbidden legacy DB hot paths**

Run:

```powershell
rg -n "mysqlsh|pymysql|psycopg|migration-core" src tests installer scripts bootstrapper assets
```

Expected: No new Python DB driver hot path or retired helper alias introduced by this wizard work. Existing documented legacy references outside this change must not be expanded.

- [ ] **Step 7: Commit final cleanup**

```powershell
git add src/ui/dialogs/cross_engine_migration_dialog.py tests/test_cross_engine_migration_dialog.py
git commit -m "test: cover DB conversion wizard flow"
```

---

## Manual QA

After tests pass, launch the app:

```powershell
python main.py
```

Open `DB 전환 마법사` and verify:

- The first screen shows Source and Target connection selection.
- The direction label reads like `MySQL tf_source84 -> PostgreSQL public`.
- `전체 실행` is not visible.
- Normalized schema JSON is hidden until `고급 설정: Normalized schema JSON 보기` is checked.
- The safety step says `전환 가능 여부 점검`, not `양방향 점검`.
- The execute step keeps `DB 변경 실행` disabled until the exact target schema text is entered.
- During execution, current table and row progress are easier to see than raw Rust event JSON.
- Verification failure shows concrete table/key/column mismatch examples above row-count summaries.

## Self-Review

Spec coverage:

- Step-by-step wizard: Tasks 1 and 2.
- No default full run: Task 1 and Task 2.
- Source JSON hidden and readable summary: Task 3.
- One-way safety check and no user-facing reverse direction: Task 4.
- Target non-empty block by default: Task 4.
- Human-readable execution plan with type/shape changes: Task 5.
- Required final approval by exact target schema name: Task 6.
- Progress prioritizes current table/chunk before overall log: Task 6.
- Strict verification with mismatch examples first: Task 7.
- Regression verification: Task 8.

Placeholder scan:

- The plan uses concrete paths, method names, widget names, commands, expected failures, expected passes, and commit messages.
- The plan does not require direct Python DB drivers or mysqlsh.

Type consistency:

- `current_step_id`, `step_ids`, `step_titles`, and `step_pages` are initialized before `_setup_ui`.
- `MigrationDirection.from_engines` uses existing `DatabaseEngine` values from `EndpointForm.engine()`.
- `verify_options`, `execution_options`, and `guide_options` all live inside `_payload`.
- `btn_migrate` remains the actual DB change trigger for worker compatibility.
