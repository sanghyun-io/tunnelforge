from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker


def test_export_worker_forwards_single_connection_snapshot_mode(monkeypatch):
    captured = {}

    class FakeExporter:
        def __init__(self, config):
            self.config = config

        def export_full_schema(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return True, "ok"

    monkeypatch.setattr(
        "src.ui.workers.rust_dump_worker.RustDumpExporter",
        FakeExporter,
    )
    worker = RustDumpWorker(
        "export_schema",
        RustDumpConfig("localhost", 3306, "root", "password"),
        schema="app",
        output_dir="C:/tmp/export",
        threads=8,
        mysql_snapshot_mode="single_connection",
    )

    worker._run_export_schema()

    assert captured["kwargs"]["mysql_snapshot_mode"] == "single_connection"


def test_export_worker_forwards_incomplete_choice_and_exposes_refusal(monkeypatch):
    captured = {}
    refusal = {"objects": ["trigger:t:trg"], "bypassable": True}

    class FakeExporter:
        last_refusal = refusal

        def __init__(self, config):
            self.config = config

        def export_full_schema(self, *args, **kwargs):
            captured["kwargs"] = kwargs
            return False, "refused"

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpExporter", FakeExporter)
    worker = RustDumpWorker(
        "export_schema",
        RustDumpConfig("localhost", 3306, "root", "password"),
        schema="app",
        output_dir="C:/tmp/export",
        allow_incomplete=True,
    )

    worker._run_export_schema()

    assert captured["kwargs"]["allow_incomplete"] is True
    assert worker.export_refusal == refusal
