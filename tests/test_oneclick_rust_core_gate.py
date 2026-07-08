import re
from types import SimpleNamespace
from pathlib import Path

import pytest
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.ui.dialogs.oneclick_migration_dialog import (
    OneClickMigrationDialog,
    OneClickMigrationWorker,
    PreflightWidget,
)
from src.ui.dialogs import migration_dialogs, oneclick_migration_dialog
from src.core.migration_analyzer import AnalysisResult, OrphanRecord
from src.core.migration_preflight import PreflightResult, CheckResult, CheckSeverity


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeEndpoint:
    engine = "mysql"

    def to_payload(self):
        return {
            "engine": "mysql",
            "host": "localhost",
            "port": 3306,
            "user": "root",
            "password": "",
            "database": "app",
            "schema": "",
        }


class FakeSchemaConnector:
    def get_schemas(self):
        return ["app"]


class FakeDerivationFacade:
    def __init__(self, result):
        self.result = result
        self.payload = None

    def derive_oneclick_charset_contracts(self, payload):
        self.payload = payload
        return self.result


def test_oneclick_worker_rejects_non_rust_core_connector():
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=object()),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )

    with pytest.raises(RuntimeError, match="Rust DB Core connector"):
        worker._ensure_rust_core_connector()


def test_oneclick_worker_accepts_rust_core_connector_shape():
    connection = SimpleNamespace(
        facade=object(),
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )

    worker._ensure_rust_core_connector()


def test_oneclick_dialog_module_docstring_matches_limited_rust_core_scope():
    source = (
        PROJECT_ROOT / "src" / "ui" / "dialogs" / "oneclick_migration_dialog.py"
    ).read_text(encoding="utf-8")

    assert "전체 마이그레이션 프로세스를 자동으로 실행합니다" not in source
    assert "Rust DB Core" in source
    assert "dry-run" in source
    assert "백업 확인" in source


def test_oneclick_worker_rejects_real_execution_without_backup_confirmation():
    connection = SimpleNamespace(
        facade=object(),
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=False,
        backup_confirmed=True,
    )

    worker.backup_confirmed = False
    with pytest.raises(RuntimeError, match="backup"):
        worker._core_payload(connection)


def test_oneclick_worker_allows_limited_real_execution_with_backup_confirmation():
    connection = SimpleNamespace(
        facade=object(),
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=False,
        backup_confirmed=True,
    )

    payload = worker._core_payload(connection)

    assert payload["dry_run"] is False
    assert payload["backup_confirmed"] is True


def test_oneclick_worker_includes_derived_charset_contracts_when_gate_passes():
    facade = FakeDerivationFacade({
        "success": True,
        "issues": [{
            "issue_type": "charset_issue",
            "severity": "warning",
            "location": "tf_oneclick_app.tf_oneclick_parent",
            "table_name": "tf_oneclick_parent",
            "message": "Table uses a legacy charset.",
            "suggestion": "Convert table charset/collation after FK-safe review.",
            "blocking": False,
        }],
        "contracts": [{
            "issue_index": 0,
            "tables": ["tf_oneclick_parent", "tf_oneclick_child"],
            "fk_order": ["tf_oneclick_parent", "tf_oneclick_child"],
            "target_charset": "utf8mb4",
            "target_collation": "utf8mb4_0900_ai_ci",
            "rollback_sql": [
                "ALTER TABLE `tf_oneclick_app`.`tf_oneclick_child` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
                "ALTER TABLE `tf_oneclick_app`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb3 COLLATE utf8mb3_general_ci;",
            ],
        }],
    })
    connection = SimpleNamespace(
        facade=facade,
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="tf_oneclick_app",
        dry_run=False,
        backup_confirmed=True,
    )

    payload = worker._core_payload(connection)

    assert facade.payload["schema"] == "tf_oneclick_app"
    assert facade.payload["connection"] == FakeEndpoint().to_payload()
    assert payload["issues"][0]["issue_type"] == "charset_issue"
    assert payload["charset_contracts"][0]["issue_index"] == 0
    assert payload["charset_contracts"][0]["target_collation"] == "utf8mb4_0900_ai_ci"


def test_oneclick_worker_omits_charset_contracts_when_derivation_gate_fails():
    facade = FakeDerivationFacade({"success": True, "issues": [], "contracts": []})
    connection = SimpleNamespace(
        facade=facade,
        connection_id="conn-1",
        endpoint=FakeEndpoint(),
    )
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=connection),
        schema="app",
        dry_run=False,
        backup_confirmed=True,
    )

    payload = worker._core_payload(connection)

    assert "issues" not in payload
    assert "charset_contracts" not in payload


def test_oneclick_dialog_keeps_dry_run_default_but_allows_limited_real_execution():
    app = QApplication.instance() or QApplication([])

    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    assert dialog.chk_dry_run.isChecked()
    assert dialog.chk_dry_run.isEnabled()
    assert "InnoDB" in dialog.chk_dry_run.toolTip()
    dialog.close()


def test_oneclick_dialog_disabled_real_execution_tooltip_does_not_reference_closed_138(monkeypatch):
    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(oneclick_migration_dialog, "ONECLICK_REAL_EXECUTION_ENABLED", False)

    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    tooltip = dialog.chk_dry_run.toolTip()
    assert dialog.chk_dry_run.isChecked()
    assert not dialog.chk_dry_run.isEnabled()
    assert "#138" not in tooltip
    assert "until" not in tooltip.lower()
    assert "disabled in this build" in tooltip
    dialog.close()


def test_migration_analyzer_exposes_oneclick_with_limited_real_execution_copy():
    app = QApplication.instance() or QApplication([])

    dialog = migration_dialogs.MigrationAnalyzerDialog(
        None,
        connector=FakeSchemaConnector(),
    )

    assert migration_dialogs.ONE_CLICK_MIGRATION_FEATURE_ENABLED is True
    assert not dialog.btn_oneclick.isHidden()
    assert "One-Click Migration" in dialog.btn_oneclick.text()
    tooltip = dialog.btn_oneclick.toolTip()
    assert "dry-run" in tooltip.lower()
    assert "InnoDB" in tooltip
    assert "백업" in tooltip
    dialog.close()


def test_migration_analyzer_cleanup_keeps_legacy_actual_execution_disabled():
    app = QApplication.instance() or QApplication([])

    dialog = migration_dialogs.MigrationAnalyzerDialog(
        None,
        connector=FakeSchemaConnector(),
    )
    dialog.analysis_result = AnalysisResult(
        schema="app",
        analyzed_at="2026-06-27T00:00:00",
        total_tables=1,
        total_fk_relations=1,
        orphan_records=[
            OrphanRecord(
                child_table="orders",
                child_column="user_id",
                parent_table="users",
                parent_column="id",
                orphan_count=2,
            )
        ],
    )

    dialog.update_orphans_table(dialog.analysis_result.orphan_records)

    assert dialog.btn_dry_run.isEnabled()
    assert not dialog.btn_execute.isEnabled()
    assert "Rust Core" in dialog.btn_execute.toolTip()
    dialog.close()


def test_oneclick_worker_translates_core_events_to_ui_signals():
    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=SimpleNamespace()),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )
    phases = []
    progress = []
    analysis = []
    plans = []
    logs = []

    worker.phase_changed.connect(lambda phase, name: phases.append((phase, name)))
    worker.progress.connect(lambda percent, message: progress.append((percent, message)))
    worker.analysis_result.connect(lambda total, auto, manual: analysis.append((total, auto, manual)))
    worker.execution_plan_ready.connect(lambda steps, summary: plans.append((steps, summary)))
    worker.log_message.connect(lambda message, style: logs.append(message))

    worker._handle_core_event({"event": "phase", "phase": "analysis", "message": "analysis started"})
    worker._handle_core_event({"event": "progress", "percent": 40, "message": "Analysis completed"})
    worker._handle_core_event({
        "event": "analysis",
        "summary": {"total_issues": 2, "auto_fixable": 1, "manual_review": 1},
    })
    worker._handle_core_event({
        "event": "execution_plan",
        "steps": [{"location": "backup"}],
        "summary": {"total_issues": 1},
    })

    assert phases == [("analysis", "분석")]
    assert progress == [(40, "Analysis completed")]
    assert analysis == [(2, 1, 1)]
    assert plans[0][0] == [{"location": "backup"}]
    assert "analysis started" in logs


def test_oneclick_dialog_renders_charset_plan_counts_and_copy():
    """실행 계획은 (더 이상 존재하지 않는) 승인 화면이 아니라 실행 로그에만 분류되어 기록된다."""
    app = QApplication.instance() or QApplication([])
    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )
    logs = []
    dialog._on_log = lambda message, style: logs.append(message)

    dialog._on_execution_plan_ready(
        [
            {
                "issue_type": "charset_issue",
                "location": "app.tf_oneclick_parent",
                "description": "Legacy charset",
                "selected_option": {
                    "strategy": "charset_collation_fk_safe",
                    "label": "Convert charset/collation with FK-safe order",
                    "description": "Convert to utf8mb4 / utf8mb4_0900_ai_ci.",
                    "sql_template": "ALTER TABLE `app`.`tf_oneclick_parent` CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;",
                },
            },
            {
                "issue_type": "charset_issue",
                "location": "app.legacy_customer",
                "description": "Legacy charset without a complete contract",
                "selected_option": {
                    "strategy": "manual",
                    "label": "Manual review",
                    "description": "Review charset/collation manually.",
                    "sql_template": "",
                },
            },
            {
                "issue_type": "int_display_width",
                "location": "app.orders.id",
                "description": "Display width is ignored by MySQL 8.4",
                "selected_option": {
                    "strategy": "skip",
                    "label": "No DB action required",
                    "description": "MySQL 8.4 ignores integer display width.",
                    "sql_template": "",
                },
            },
        ],
        {"total_issues": 3, "auto_fixable": 1, "manual_review": 1, "skip_recommended": 1},
    )

    assert logs[0] == "📋 실행 계획: 전체 3개, 자동 1개, 수동 1개, 조치 불필요 1개"
    joined = "\n".join(logs)
    assert "Convert charset/collation with FK-safe order" in joined
    assert "Review charset/collation manually." in joined
    assert "MySQL 8.4 ignores integer display width." in joined
    dialog.close()


def _read_dialog_source() -> str:
    return (
        PROJECT_ROOT / "src" / "ui" / "dialogs" / "oneclick_migration_dialog.py"
    ).read_text(encoding="utf-8")


def test_oneclick_worker_finished_signal_no_longer_shadows_qthread_finished():
    """커스텀 finished 시그널이 QThread.finished를 가리지 않아야 한다."""
    source = _read_dialog_source()

    assert "migration_finished = pyqtSignal(bool, object)" in source

    worker_class_match = re.search(
        r"class OneClickMigrationWorker\(QThread\):.*?(?=\nclass |\Z)",
        source,
        re.DOTALL,
    )
    assert worker_class_match is not None
    assert not re.search(r"^\s*finished\s*=\s*pyqtSignal", worker_class_match.group(0), re.MULTILINE)


def test_oneclick_dialog_connects_migration_finished_not_generic_finished():
    """다이얼로그는 이름이 바뀐 결과 시그널에 연결하고, 상속받은 finished는 정리용으로만 쓴다."""
    source = _read_dialog_source()

    assert "self.worker.migration_finished.connect(self._on_finished)" in source
    assert ".finished.connect(self._on_finished)" not in source
    assert "self.worker.finished.connect(self.worker.deleteLater)" in source


def test_oneclick_dialog_has_no_fake_cancel_or_unsafe_thread_termination():
    """실제로 취소할 수 없는 Rust 실행에 대해 가짜 취소/강제종료 경로가 없어야 한다."""
    source = _read_dialog_source()

    assert "def cancel(self)" not in source
    assert "self.worker.cancel()" not in source
    assert ".terminate(" not in source
    assert "_is_cancelled" not in source
    assert "_execution_gate" not in source
    assert "resume_execution" not in source
    assert "btn_cancel" not in source
    assert "cancel_migration" not in source


def test_oneclick_dialog_close_event_has_no_blocking_thread_calls():
    """closeEvent는 quit()/wait()/terminate()로 워커를 강제 종료하지 않는다."""
    source = _read_dialog_source()

    close_event_match = re.search(
        r"    def closeEvent\(self, event\):.*?(?=\n    def )",
        source,
        re.DOTALL,
    )
    assert close_event_match is not None
    body = close_event_match.group(0)
    assert ".wait(" not in body
    assert ".quit(" not in body
    assert ".terminate(" not in body


def test_oneclick_dialog_deletes_disconnected_execution_plan_pause_gate():
    """Rust에 실제 일시정지 지점이 없으므로 연결되지 않은 승인 화면 경로는 삭제되어야 한다."""
    source = _read_dialog_source()

    for token in (
        "class ExecutionPlanWidget",
        "start_requested",
        "_on_start_execution_confirmed",
        "execution_plan_widget",
    ):
        assert token not in source


def test_oneclick_worker_has_no_dead_pre_issues_or_empty_report_helper():
    """죽은 코드(_pre_issues, _create_empty_report)는 삭제되어야 한다."""
    source = _read_dialog_source()

    assert "_pre_issues" not in source
    assert "_create_empty_report" not in source


def test_oneclick_dialog_documents_non_interruptible_rust_execution_in_korean():
    """취소 버튼을 되살리지 못하도록 중단 불가 안내 문구가 한국어로 남아 있어야 한다."""
    source = _read_dialog_source()

    assert "중단할 수 없습니다" in source
    assert "Rust 코어" in source


def test_oneclick_worker_still_delegates_to_facade_run_oneclick():
    """워커는 여전히 얇은 래퍼로 Rust facade의 run_oneclick을 호출해야 한다."""
    source = _read_dialog_source()

    assert "connection.facade.run_oneclick(" in source


def test_oneclick_dialog_close_event_detaches_worker_instead_of_blocking(monkeypatch):
    """실행 중 닫기를 확인하면 워커를 분리(detach)만 하고 이벤트를 accept해야 한다."""
    app = QApplication.instance() or QApplication([])
    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=SimpleNamespace()),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )
    monkeypatch.setattr(worker, "isRunning", lambda: True)
    dialog.worker = worker
    worker.migration_finished.connect(dialog._on_finished)

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.Yes),
    )

    class _FakeEvent:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    event = _FakeEvent()
    dialog.closeEvent(event)

    assert event.accepted
    assert not event.ignored
    assert dialog.worker is None
    assert worker in oneclick_migration_dialog._DETACHED_ONECLICK_WORKERS

    # 워커의 상속받은 finished가 발생하면 detach 정리 콜백이 스스로 제거해야 한다.
    worker.finished.emit()
    assert worker not in oneclick_migration_dialog._DETACHED_ONECLICK_WORKERS
    dialog.close()


def test_oneclick_dialog_close_event_ignores_close_when_user_declines(monkeypatch):
    """사용자가 닫기를 취소하면 워커는 그대로 유지되고 이벤트는 ignore되어야 한다."""
    app = QApplication.instance() or QApplication([])
    dialog = OneClickMigrationDialog(
        None,
        connector=SimpleNamespace(),
        schema="app",
    )

    worker = OneClickMigrationWorker(
        connector=SimpleNamespace(connection=SimpleNamespace()),
        schema="app",
        dry_run=True,
        backup_confirmed=True,
    )
    monkeypatch.setattr(worker, "isRunning", lambda: True)
    dialog.worker = worker

    monkeypatch.setattr(
        QMessageBox,
        "question",
        staticmethod(lambda *args, **kwargs: QMessageBox.StandardButton.No),
    )

    class _FakeEvent:
        def __init__(self):
            self.accepted = False
            self.ignored = False

        def accept(self):
            self.accepted = True

        def ignore(self):
            self.ignored = True

    event = _FakeEvent()
    dialog.closeEvent(event)

    assert event.ignored
    assert not event.accepted
    assert dialog.worker is worker
    dialog.close()


def test_preflight_widget_creates_rows_dynamically_from_rust_check_names():
    """Preflight 행은 하드코딩된 목록이 아니라 Rust가 보낸 체크 이름으로 동적 생성된다."""
    app = QApplication.instance() or QApplication([])
    widget = PreflightWidget()

    result = PreflightResult(
        passed=False,
        checks=[
            CheckResult(name="MySQL engine", passed=True, severity=CheckSeverity.INFO, message="MyISAM 없음"),
            CheckResult(name="Backup status", passed=True, severity=CheckSeverity.INFO, message="백업 확인됨"),
            CheckResult(name="Schema inspect", passed=False, severity=CheckSeverity.WARNING, message="검사 지연"),
        ],
        warnings=["검사 지연"],
        errors=[],
    )
    widget.update_result(result)

    assert set(widget.check_rows.keys()) == {"MySQL engine", "Backup status", "Schema inspect"}

    status, _label, detail = widget.check_rows["Schema inspect"]
    assert status.text() == "⚠️"
    assert detail.text() == "검사 지연"

    ok_status, _ok_label, _ok_detail = widget.check_rows["MySQL engine"]
    assert ok_status.text() == "✅"
    widget.close()


def test_preflight_widget_reset_clears_rows_for_rerun():
    """재실행 시 이전 검사 행이 남아있지 않아야 한다."""
    app = QApplication.instance() or QApplication([])
    widget = PreflightWidget()

    widget.update_result(PreflightResult(
        passed=True,
        checks=[CheckResult(name="MySQL engine", passed=True, severity=CheckSeverity.INFO, message="OK")],
        warnings=[],
        errors=[],
    ))
    assert "MySQL engine" in widget.check_rows

    widget.reset()

    assert widget.check_rows == {}
    assert widget.result_label.text() == ""
    widget.close()


def test_preflight_widget_has_no_legacy_korean_checker_mapping():
    """더 이상 존재하지 않는 고정 한글 체크 매핑이 남아 있으면 안 된다."""
    source = _read_dialog_source()

    for legacy_label in ("권한 검사", "디스크 공간 검사", "활성 연결 검사", "MySQL 버전 확인"):
        assert legacy_label not in source
