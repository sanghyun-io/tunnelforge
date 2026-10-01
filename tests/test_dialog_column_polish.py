"""UI polish for the job list (TF-STATUS-132) and recovered SQL (TF-STATUS-129) dialogs."""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication, QHeaderView, QSplitter

from src.core import job_history as jh
from src.core import workspace_store as ws
from src.ui.dialogs.job_list_dialog import STATUS_COLORS, STATUS_LABELS, JobListDialog
from src.ui.dialogs.recovered_sql_dialog import RecoveredSqlDialog

_app = QApplication.instance() or QApplication([])
LONG_ERROR = "테이블 orders 가져오기 실패: Duplicate entry '1042' for key 'orders.PRIMARY' " * 3


@pytest.fixture
def dialog(tmp_path):
    history = jh.JobHistory(tmp_path / "jobs.json")
    now = datetime.now(timezone.utc)
    for index, (kind, status, error) in enumerate([
        (jh.KIND_EXPORT_FULL, jh.STATUS_COMPLETED, ""),
        (jh.KIND_IMPORT, jh.STATUS_FAILED, LONG_ERROR),
        (jh.KIND_PROMOTE, jh.STATUS_PARTIAL, "일부만 이동됨"),
        (jh.KIND_MIGRATION_RUN, jh.STATUS_CANCELLED, ""),
    ]):
        job_id = history.begin(kind, profile_name="p", target=f"C:/very/long/path/{index}", now=now - timedelta(minutes=index))
        history.finish(job_id, status, error=error, now=now)
    shown = JobListDialog(history)
    shown.resize(1100, 560)
    shown.show()
    _app.processEvents()
    yield shown
    shown.close()


def _row(dialog, status_text):
    for row in range(dialog.table.rowCount()):
        if dialog.table.item(row, 5).text() == status_text:
            return row
    raise AssertionError(status_text)


def test_error_summary_column_stretches_and_no_horizontal_scroll(dialog):
    header = dialog.table.horizontalHeader()
    assert header.stretchLastSection()
    assert header.sectionResizeMode(7) == QHeaderView.ResizeMode.Stretch
    assert not dialog.table.horizontalScrollBar().isVisible()
    assert header.length() <= dialog.table.viewport().width() + 1
    assert dialog.table.columnWidth(7) > dialog.table.columnWidth(5)  # it takes the remaining width


def test_error_summary_and_truncatable_cells_have_full_tooltips(dialog):
    row = _row(dialog, STATUS_LABELS[jh.STATUS_FAILED])
    assert dialog.table.item(row, 7).toolTip() == dialog.table.item(row, 7).text() and "orders.PRIMARY" in dialog.table.item(row, 7).toolTip()
    assert dialog.table.item(row, 2).toolTip() == dialog.table.item(row, 2).text()
    assert dialog.table.item(row, 3).toolTip() == dialog.table.item(row, 3).text()
    done = _row(dialog, STATUS_LABELS[jh.STATUS_COMPLETED])
    assert dialog.table.item(done, 7).toolTip() == ""  # nothing to show for rows without an error


def test_status_colours_never_replace_the_status_text(dialog):
    for status, color in STATUS_COLORS.items():
        assert status in (jh.STATUS_FAILED, jh.STATUS_PARTIAL, jh.STATUS_INTERRUPTED)
        assert color.startswith("#")
    failed = dialog.table.item(_row(dialog, STATUS_LABELS[jh.STATUS_FAILED]), 5)
    partial = dialog.table.item(_row(dialog, STATUS_LABELS[jh.STATUS_PARTIAL]), 5)
    done = dialog.table.item(_row(dialog, STATUS_LABELS[jh.STATUS_COMPLETED]), 5)
    assert failed.text() == "실패" and partial.text() == "부분 완료"  # the words stay
    assert failed.foreground().color().name() == STATUS_COLORS[jh.STATUS_FAILED]
    assert partial.foreground().color().name() == STATUS_COLORS[jh.STATUS_PARTIAL]
    assert failed.foreground().color().name() != partial.foreground().color().name()
    assert done.foreground().color().name() != STATUS_COLORS[jh.STATUS_FAILED]  # completed keeps the default colour


def test_recovered_sql_list_is_wide_and_shows_full_details_in_tooltips(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "ws")
    profile = "c3f1a9d2-7b44-4c1e-9d3a-5e2b7a8f0011"
    path = "C:/Users/someone/projects/reports/monthly_sales_report_2026_09.sql"
    store.save(ws.WorkspaceState(profile_id=profile, tabs=[ws.TabState(id="t", title_index=1, text="SELECT 1;", dirty=True),
                                                           ws.TabState(id="u", title_index=2, text="SELECT 2;", dirty=True, file_path=path)]),
               now=datetime.now(timezone.utc) - timedelta(days=100))
    dialog = RecoveredSqlDialog(store, [])
    dialog.show()
    _app.processEvents()
    try:
        assert isinstance(dialog.splitter, QSplitter)
        assert dialog.workspace_list.width() >= 300 and dialog.splitter.sizes()[0] >= 300
        assert dialog.workspace_list.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        assert not dialog.workspace_list.horizontalScrollBar().isVisible()
        assert dialog.tab_list.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        item = dialog.workspace_list.item(0)
        assert profile in item.toolTip() and "탭: 2개" in item.toolTip() and "만료" in item.toolTip()
        names = [dialog.tab_list.item(i).toolTip() for i in range(dialog.tab_list.count())]
        assert path in names
    finally:
        dialog.close()
