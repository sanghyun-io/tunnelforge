"""The GC pause used while result grids fill must be restored on every path (TF-STATUS-131)."""
import gc

import pytest
from PyQt6.QtWidgets import QApplication, QTableWidget, QTabWidget

from src.ui.dialogs.streaming_result import GcPause, StreamingResultMixin

_app = QApplication.instance() or QApplication([])


@pytest.fixture(autouse=True)
def _gc_state():
    was_enabled = gc.isenabled()
    gc.enable()
    GcPause._holds = 0
    GcPause._was_enabled = True
    yield
    GcPause._holds = 0
    GcPause._was_enabled = True
    gc.enable() if was_enabled else gc.disable()


class _Host(StreamingResultMixin):
    """The mixin with just enough of the dialog around it."""

    def __init__(self, fail_setup=False):
        self.result_tabs = QTabWidget()
        self._result_counter = 0
        self.worker = None
        self.fail_setup = fail_setup

    def _add_result_table(self, columns, rows, exec_time, query="", finalize=True):
        table = QTableWidget()
        table.setColumnCount(len(columns))
        table._export_columns = columns
        table._export_rows = rows
        table._source_query = query
        self._result_counter += 1
        self.result_tabs.addTab(table, "x")
        return table

    def _setup_result_table_editability(self, *args):
        if self.fail_setup:
            raise RuntimeError("editability failed")


def _start(host, idx=0):
    host._on_result_started(idx, ["id"])
    host._on_result_rows(idx, [["1"], ["2"]])


def test_gc_is_paused_while_streaming_and_restored_on_completion():
    host = _Host()
    _start(host)
    assert gc.isenabled() is False
    host._finalize_streamed_result(0)
    assert gc.isenabled() is True


def test_gc_is_restored_after_cancel_and_error():
    for message in ("쿼리가 사용자에 의해 취소되었습니다", "boom"):
        host = _Host()
        _start(host)
        host._finalize_streamed_result(0, error=message)
        assert gc.isenabled() is True, message


def test_gc_is_restored_even_if_finishing_the_grid_raises():
    host = _Host(fail_setup=True)
    _start(host)
    with pytest.raises(RuntimeError):
        host._finalize_streamed_result(0)
    assert gc.isenabled() is True


def test_two_concurrent_grids_reenable_gc_only_after_the_last_one():
    first, second = _Host(), _Host()  # two windows
    _start(first)
    _start(second)
    third_tab = _Host()
    _start(third_tab)
    first._finalize_streamed_result(0)
    assert gc.isenabled() is False
    second._finalize_streamed_result(0, error="취소")
    assert gc.isenabled() is False
    third_tab._finalize_streamed_result(0)
    assert gc.isenabled() is True
    # two result tabs of one window
    window = _Host()
    _start(window, 0)
    _start(window, 1)
    window._finalize_streamed_result(0)
    assert gc.isenabled() is False
    window._finalize_streamed_result(1)
    assert gc.isenabled() is True


def test_gc_that_was_already_disabled_stays_disabled():
    gc.disable()
    host = _Host()
    _start(host)
    host._finalize_streamed_result(0)
    assert gc.isenabled() is False


def test_window_close_or_worker_end_restores_gc_and_is_idempotent():
    host = _Host()
    _start(host, 0)
    _start(host, 1)
    host._finalize_all_streamed()
    assert gc.isenabled() is True
    host._finalize_all_streamed()
    host._finalize_streamed_result(0)  # already finished: no double release
    assert GcPause._holds == 0 and gc.isenabled() is True


def test_destroyed_grid_releases_its_hold():
    host = _Host()
    table = QTableWidget()
    host._hold_gc(table)
    assert gc.isenabled() is False
    del table
    assert gc.isenabled() is True and GcPause._holds == 0


def test_release_is_idempotent_and_never_goes_negative():
    hold = GcPause.acquire()
    other = GcPause.acquire()
    hold.release()
    hold.release()
    assert gc.isenabled() is False and GcPause._holds == 1
    other.release()
    assert gc.isenabled() is True and GcPause._holds == 0
