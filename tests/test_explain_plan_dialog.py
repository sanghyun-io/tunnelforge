"""TF-STATUS-133: execution plan dialog (Qt offscreen)."""
import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6 import sip
from PyQt6.QtCore import QTimer
from PyQt6.QtWidgets import QApplication, QMessageBox, QWidget

from src.core import explain_plan as ep
from src.core.db_core_client import DbCoreServiceError
from src.ui.dialogs import explain_plan_dialog as dlg

app = QApplication.instance() or QApplication(sys.argv)
FIXTURES = Path(__file__).parent / "fixtures" / "explain"


def _fixture(name):
    data = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    meta = data["meta"]
    return ep.parse_explain_rows(meta["engine"], meta["analyze"], meta["format"], data["rows"])


def pump_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.002)
    assert predicate()


@pytest.fixture(autouse=True)
def boxes(monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: shown.append(("warning", a[2])))
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    return shown


class Facade:
    def __init__(self):
        self.cancelled = []

    def cancel_query(self, job_id):
        self.cancelled.append(job_id)
        return {}


_PARENTS = []  # a dialog dies with its parent widget, so keep parents alive for the test session


def _make(explain, facade=None):
    parent = QWidget()
    _PARENTS.append(parent)
    return dlg.ExplainPlanDialog(parent, facade or Facade(), "c1", "SELECT 1", explain=explain)


def _rows(dialog):
    out = []

    def walk(item, depth):
        out.append((depth, [item.text(c) for c in range(item.columnCount())], item))
        for i in range(item.childCount()):
            walk(item.child(i), depth + 1)

    root = dialog.tree.invisibleRootItem()
    for i in range(root.childCount()):
        walk(root.child(i), 0)
    return out


def test_tree_shows_nodes_estimates_actuals_and_highlights_flags():
    dialog = _make(None)
    dialog.show_result(_fixture("mysql80_analyze"))
    rows = _rows(dialog)
    assert [r[0] for r in rows][:3] == [0, 1, 2]
    scan = next(r for r in rows if r[1][0] == "Table scan on a")
    assert scan[1][2] == "300" and scan[1][3] == "300" and scan[1][5] == "1"
    assert "풀 테이블 스캔" in scan[1][6]
    assert scan[2].background(0).color().name() == "#fdecea"  # flagged rows are highlighted
    plain = next(r for r in rows if r[1][0].startswith("Nested loop"))
    assert plain[2].background(0).color().name() != "#fdecea"
    assert "ANALYZE" in dialog.lbl_status.text() and ep.ANALYZE_WARNING in dialog.lbl_status.text()
    assert dialog.txt_raw.toPlainText() == dialog.result.raw


def test_postgres_plan_summary_and_raw_tab():
    dialog = _make(None)
    result = _fixture("pg18_analyze")
    dialog.show_result(result)
    assert "Execution Time: 0.22 ms" in dialog.lbl_status.text()
    assert json.loads(dialog.txt_raw.toPlainText()) == json.loads(result.raw)
    seq = [r for r in _rows(dialog) if r[1][0].startswith("Seq Scan")]
    assert seq and all("순차 스캔" in r[1][6] for r in seq)


def test_plain_explain_says_nothing_was_executed_and_gives_no_advice():
    dialog = _make(None)
    dialog.show_result(_fixture("pg18_plain"))
    assert "실행 안 함" in dialog.lbl_status.text()
    text = " ".join(" ".join(r[1]) for r in _rows(dialog)) + dialog.lbl_status.text()
    assert "인덱스" not in text and "CREATE INDEX" not in text and "권고" not in text


def test_analyze_needs_explicit_choice_and_confirmation(monkeypatch):
    calls = []

    def explain(facade, connection_id, sql, analyze=False, job_id=None, timeout_ms=None):
        calls.append(analyze)
        return _fixture("pg18_analyze" if analyze else "pg18_plain")

    dialog = _make(explain)
    assert dialog.lbl_warning.isHidden() and not dialog.chk_analyze.isChecked()
    dialog.run_explain()
    pump_until(lambda: not dialog.is_running())
    assert calls == [False]

    dialog.chk_analyze.setChecked(True)
    assert not dialog.lbl_warning.isHidden()
    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.No)
    dialog.run_explain()
    assert calls == [False] and not dialog.is_running()  # declined: nothing executed

    monkeypatch.setattr(QMessageBox, "question", lambda *a: QMessageBox.StandardButton.Yes)
    dialog.run_explain()
    pump_until(lambda: not dialog.is_running())
    assert calls == [False, True]
    assert dialog.result.analyze


def test_gui_stays_responsive_while_the_plan_is_fetched():
    started, release = threading.Event(), threading.Event()

    def explain(facade, connection_id, sql, analyze=False, job_id=None, timeout_ms=None):
        started.set()
        assert release.wait(3)
        return _fixture("mysql80_plain")

    dialog = _make(explain)
    ticks = []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(1))
    timer.start(5)
    dialog.run_explain()
    pump_until(lambda: started.is_set() and len(ticks) >= 3)
    assert dialog.is_running() and not dialog.btn_run.isEnabled() and dialog.btn_cancel.isEnabled()
    release.set()
    pump_until(lambda: not dialog.is_running())
    timer.stop()
    assert dialog.btn_run.isEnabled() and dialog.result is not None
    pump_until(lambda: not dlg.has_active_explain_workers())


def test_cancel_asks_server_to_cancel_and_discards_late_result():
    started, release = threading.Event(), threading.Event()

    def explain(facade, connection_id, sql, analyze=False, job_id=None, timeout_ms=None):
        started.set()
        release.wait(3)
        return _fixture("mysql80_plain")

    facade = Facade()
    dialog = _make(explain, facade)
    dialog.run_explain()
    pump_until(started.is_set)
    job_id = dialog._job_id
    dialog.cancel_running()
    assert facade.cancelled == [job_id] and not dialog.is_running() and dialog.btn_run.isEnabled()
    release.set()
    pump_until(lambda: not dlg.has_active_explain_workers())
    assert dialog.result is None and dialog.tree.topLevelItemCount() == 0  # late result discarded


def test_close_while_running_and_destroyed_dialog_do_not_break(boxes):
    started, release = threading.Event(), threading.Event()

    def explain(facade, connection_id, sql, analyze=False, job_id=None, timeout_ms=None):
        started.set()
        release.wait(3)
        return _fixture("mysql80_plain")

    closed = _make(explain)
    closed.run_explain()
    pump_until(started.is_set)
    closed.reject()
    release.set()
    pump_until(lambda: not dlg.has_active_explain_workers())
    assert closed.result is None

    started.clear()
    release.clear()
    destroyed = _make(explain)
    destroyed.run_explain()
    pump_until(started.is_set)
    sip.delete(destroyed)
    release.set()
    pump_until(lambda: not dlg.has_active_explain_workers())
    assert boxes == []


def test_core_refusal_and_errors_are_shown_not_raised(boxes):
    def refusing(facade, connection_id, sql, analyze=False, job_id=None, timeout_ms=None):
        raise DbCoreServiceError("ANALYZE는 쿼리를 실제로 실행하므로 거부", error_code="explain_refused")

    dialog = _make(refusing)
    dialog.run_explain()
    pump_until(lambda: not dialog.is_running())
    assert boxes and "거부" in boxes[0][1] and "거부" in dialog.lbl_status.text()
    assert dialog.btn_run.isEnabled() and dialog.result is None

    def broken(*args, **kwargs):
        raise ValueError("MySQL 실행 계획 JSON 형식을 인식할 수 없습니다")

    dialog = _make(broken)
    dialog.run_explain()
    pump_until(lambda: not dialog.is_running())
    assert "인식할 수 없습니다" in boxes[-1][1]
