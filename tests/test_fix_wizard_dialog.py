from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.ui.dialogs import fix_wizard_dialog
from src.ui.workers import fix_wizard_worker


class _FakeSignal:
    def connect(self, _callback):
        pass

    def disconnect(self, *_args, **_kwargs):
        pass


class _FakeWorker:
    progress = _FakeSignal()
    finished = _FakeSignal()

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False

    def start(self):
        self.started = True

    def isRunning(self):
        return False


class _FakeRunningWorker:
    """closeEvent가 terminate() 없이 협조적으로 취소하는지 검증하기 위한 스텁.

    isRunning()이 항상 True이고 wait()가 항상 타임아웃되는 상황(=취소 요청 후에도
    즉시 끝나지 않는 워커)을 시뮬레이션한다.
    """

    progress = _FakeSignal()
    finished = _FakeSignal()

    def __init__(self):
        self.cancel_requested = False
        self.terminate_called = False

    def isRunning(self):
        return True

    def request_cancel(self):
        self.cancel_requested = True

    def wait(self, _timeout_ms):
        return False

    def terminate(self):
        self.terminate_called = True


def test_legacy_fix_wizard_execution_page_runs_dry_run_only(monkeypatch):
    app = QApplication.instance() or QApplication([])
    captured = {}

    def fake_worker(**kwargs):
        worker = _FakeWorker(**kwargs)
        captured["worker"] = worker
        return worker

    monkeypatch.setattr(fix_wizard_dialog, "FixWizardWorker", fake_worker)
    monkeypatch.setattr(
        fix_wizard_dialog.QMessageBox,
        "warning",
        lambda *args, **kwargs: QMessageBox.StandardButton.Yes,
    )

    dialog = fix_wizard_dialog.FixWizardDialog(
        None,
        connector=SimpleNamespace(),
        issues=[],
        schema="app",
    )

    dialog.execution_page.execute()

    worker = captured["worker"]
    assert worker.kwargs["dry_run"] is True
    assert worker.started is True
    dialog.close()


def test_fix_wizard_worker_rejects_legacy_python_mutation_mode():
    QApplication.instance() or QApplication([])

    try:
        fix_wizard_dialog.FixWizardWorker(
            connector=SimpleNamespace(),
            schema="app",
            steps=[],
            dry_run=False,
        )
    except RuntimeError as exc:
        assert "Rust Core" in str(exc)
    else:
        raise AssertionError("FixWizardWorker accepted dry_run=False")


def test_close_event_uses_cooperative_cancel_not_terminate(monkeypatch):
    """closeEvent는 실행 중인 워커에 request_cancel()만 호출하고 terminate()는
    호출하지 않아야 한다. terminate()는 facade가 잡고 있는 락을 해제하지 못한 채
    스레드를 강제 종료시켜 데드락을 유발할 수 있다."""
    # QApplication 참조를 반드시 변수에 보관해야 한다 — 참조가 없으면 이 표현식이
    # 끝나는 즉시 가비지 컬렉션되어 뒤이은 QWidget(FixWizardDialog) 생성이
    # "QWidget: Must construct a QApplication before a QWidget" 로 fatal abort된다.
    app = QApplication.instance() or QApplication([])

    # 실제 콘솔 로거(이모지 포함 메시지)가 cp949 콘솔에 기록을 시도하면
    # UnicodeEncodeError가 발생할 수 있다 (src/core/logger.py는 이번 WP 범위 밖이라
    # 직접 수정하지 않는다). 이 테스트는 협조적 취소 로직만 검증하면 되므로
    # 실제 로깅 I/O를 피하도록 로거를 무력화한다.
    monkeypatch.setattr(
        "src.core.logger.get_logger",
        lambda _name: SimpleNamespace(
            info=lambda *a, **k: None,
            warning=lambda *a, **k: None,
            log=lambda *a, **k: None,
        ),
    )

    dialog = fix_wizard_dialog.FixWizardDialog(
        None,
        connector=SimpleNamespace(),
        issues=[],
        schema="app",
    )

    fake_worker = _FakeRunningWorker()
    dialog.execution_page.worker = fake_worker

    dialog.close()

    assert fake_worker.cancel_requested is True
    assert fake_worker.terminate_called is False


def test_fix_wizard_worker_cancel_before_start_skips_all_phases(monkeypatch):
    """request_cancel()이 run() 시작 전에 호출되면 어떤 DB 작업도 시작하지 않고
    즉시 실패로 종료해야 한다."""
    app = QApplication.instance() or QApplication([])

    def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("취소된 워커는 어떤 DB 작업도 시작하면 안 됩니다")

    monkeypatch.setattr(fix_wizard_worker, "FKSafeCharsetChanger", _fail_if_called)
    monkeypatch.setattr(fix_wizard_worker, "BatchFixExecutor", _fail_if_called)

    worker = fix_wizard_worker.FixWizardWorker(
        connector=SimpleNamespace(),
        schema="app",
        steps=[object()],
        dry_run=True,
        charset_tables_to_fix={"orders"},
    )

    results = {}
    worker.finished.connect(
        lambda success, message, result: results.update(success=success, message=message)
    )

    worker.request_cancel()
    worker.run()

    assert results["success"] is False
    assert "취소" in results["message"]


def test_fix_wizard_worker_cancel_between_phases_skips_other_issues(monkeypatch):
    """문자셋 변경 단계 도중 취소가 요청되면, 단계 사이 체크포인트에서 감지되어
    기타 이슈(BatchFixExecutor) 처리는 실행되지 않아야 한다."""
    app = QApplication.instance() or QApplication([])

    holder = {}

    class _FakeCharsetChanger:
        def __init__(self, _connector, _schema):
            pass

        def execute_safe_charset_change(self, tables, charset, collation, dry_run, progress_callback):
            # 문자셋 변경이 진행되는 도중 다이얼로그가 닫혀 취소가 요청된 상황을 시뮬레이션
            holder["worker"].request_cancel()
            progress_callback("문자셋 변경 진행 중")
            return True, "ok", {"fk_count": 0}

    monkeypatch.setattr(fix_wizard_worker, "FKSafeCharsetChanger", _FakeCharsetChanger)

    batch_executor_calls = []

    class _FakeBatchExecutor:
        def __init__(self, _connector, _schema):
            pass

        def set_progress_callback(self, _cb):
            pass

        def execute_batch(self, steps, dry_run):
            batch_executor_calls.append((steps, dry_run))
            raise AssertionError("취소 요청 이후에는 기타 이슈 처리가 실행되면 안 됩니다")

    monkeypatch.setattr(fix_wizard_worker, "BatchFixExecutor", _FakeBatchExecutor)

    worker = fix_wizard_worker.FixWizardWorker(
        connector=SimpleNamespace(),
        schema="app",
        steps=[object()],
        dry_run=True,
        charset_tables_to_fix={"orders"},
    )
    holder["worker"] = worker

    results = {}
    worker.finished.connect(
        lambda success, message, result: results.update(success=success, message=message)
    )

    worker.run()

    assert batch_executor_calls == []
    assert results["success"] is False
    assert "취소" in results["message"]


def test_charset_fix_page_resets_state_when_charset_issues_cleared():
    """문자셋 이슈를 선택했다가 뒤로 가서 선택을 해제한 경우, CharsetFixPage는
    이전 라운드의 table_infos/FK 캐시/charset_tables_to_fix를 반드시 초기화해야
    한다. 그렇지 않으면 PreviewPage가 이미 취소된 테이블에 대한 stale ALTER TABLE
    SQL을 계속 노출하게 된다."""
    app = QApplication.instance() or QApplication([])

    dialog = fix_wizard_dialog.FixWizardDialog(
        None,
        connector=SimpleNamespace(),
        issues=[],
        schema="app",
    )

    page = dialog.charset_fix_page

    # 이전 라운드(문자셋 이슈 선택)에서 남은 상태를 시뮬레이션
    stale_info = fix_wizard_dialog.CharsetTableInfo(
        table_name="orders",
        current_charset="latin1",
        current_collation="latin1_swedish_ci",
        fk_parents=[],
        fk_children=[],
        is_original_issue=True,
        skip=False,
    )
    page.table_infos = [stale_info]
    page._fk_cache = [object()]
    dialog.charset_tables_to_fix = {"orders"}

    # 뒤로 가서 문자셋 이슈 선택을 해제한 상태 (has_charset_issues() == False)
    dialog.charset_issues = []

    page.initializePage()

    assert page.table_infos == []
    assert page._fk_cache == []
    assert dialog.charset_tables_to_fix == set()


def test_issue_selection_page_is_complete_ignores_hidden_rows():
    """필터로 숨겨진 행은 validatePage()에서 선택 대상으로 취급되지 않으므로,
    isComplete()도 동일하게 숨겨진 행의 체크 상태를 무시해야 한다. 그렇지 않으면
    체크된 행이 전부 필터로 숨겨져도 '다음' 버튼이 활성화된 채로 남는다."""
    app = QApplication.instance() or QApplication([])

    issue = SimpleNamespace(
        severity="warning",
        issue_type=fix_wizard_dialog.IssueType.RESERVED_KEYWORD,
        location="app.orders.name",
        description="예약어 컬럼명",
    )

    dialog = fix_wizard_dialog.FixWizardDialog(
        None,
        connector=SimpleNamespace(),
        issues=[issue],
        schema="app",
    )

    page = dialog.issue_page
    page.initializePage()
    page.checkboxes[0].setChecked(True)

    assert page.isComplete() is True  # 아직 필터로 숨겨지지 않음

    page.chk_warning.setChecked(False)  # warning 필터 해제 → 해당 행 숨김

    assert page.table.isRowHidden(0) is True
    assert page.isComplete() is False
