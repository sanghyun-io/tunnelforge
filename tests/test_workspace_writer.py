import threading

from src.core import workspace_store as ws
from src.core.workspace_writer import AsyncWorkspaceWriter


def state(text, tabs=1):
    return ws.WorkspaceState(profile_id="p", tabs=[ws.TabState(id=f"t{i}", text=text, dirty=True) for i in range(tabs)])


def test_flush_waits_for_the_latest_snapshot(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "ws")
    writer = AsyncWorkspaceWriter(store, "p")
    try:
        writer.submit(state("SELECT 1;"))
        assert writer.flush(5.0)
        assert store.load("p").state.tabs[0].text == "SELECT 1;"
    finally:
        writer.stop()


def test_pending_snapshots_are_coalesced_to_the_newest(tmp_path, monkeypatch):
    store = ws.WorkspaceStore(tmp_path / "ws")
    gate = threading.Event()
    saved = []
    original = ws.WorkspaceStore.save

    def slow_save(self, snapshot, now=None):
        saved.append(snapshot.tabs[0].text)
        gate.wait(5)
        return original(self, snapshot, now)

    monkeypatch.setattr(ws.WorkspaceStore, "save", slow_save)
    writer = AsyncWorkspaceWriter(store, "p")
    try:
        writer.submit(state("first"))
        while not saved:
            pass
        for text in ("second", "third", "newest"):
            writer.submit(state(text))
        gate.set()
        assert writer.flush(5.0)
        assert saved == ["first", "newest"], "intermediate snapshots must be dropped"
        assert store.load("p").state.tabs[0].text == "newest"
    finally:
        gate.set()
        writer.stop()


def test_failures_are_reported_once_and_the_writer_keeps_working(tmp_path, monkeypatch):
    store = ws.WorkspaceStore(tmp_path / "ws")
    calls = {"n": 0}
    original = ws.WorkspaceStore.save

    def flaky(self, snapshot, now=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ws.WorkspaceStoreError("cannot write workspace file: OSError")
        return original(self, snapshot, now)

    monkeypatch.setattr(ws.WorkspaceStore, "save", flaky)
    writer = AsyncWorkspaceWriter(store, "p")
    try:
        writer.submit(state("a"))
        assert writer.flush(5.0)
        assert "cannot write" in writer.take_error() and writer.take_error() is None
        writer.submit(state("b"))
        assert writer.flush(5.0)
        assert store.load("p").state.tabs[0].text == "b"
    finally:
        writer.stop()


def test_omitted_tabs_are_surfaced_and_delete_works(tmp_path, monkeypatch):
    monkeypatch.setattr(ws, "MAX_TAB_TEXT_BYTES", 5)
    store = ws.WorkspaceStore(tmp_path / "ws")
    writer = AsyncWorkspaceWriter(store, "p")
    try:
        writer.submit(state("far too long"))
        assert writer.flush(5.0) and writer.take_omitted() == ["t0"]
        writer.request_delete()
        assert writer.flush(5.0) and store.load("p").status == ws.STATUS_MISSING
    finally:
        writer.stop()


def test_submit_after_stop_is_ignored(tmp_path):
    store = ws.WorkspaceStore(tmp_path / "ws")
    writer = AsyncWorkspaceWriter(store, "p")
    writer.stop()
    writer.submit(state("late"))
    assert store.load("p").status == ws.STATUS_MISSING
