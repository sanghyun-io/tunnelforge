from types import SimpleNamespace

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.ui.dialogs import fix_wizard_dialog


class _FakeSignal:
    def connect(self, _callback):
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
