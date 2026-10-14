import pytest

from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import CANCELLED_MESSAGE, RustDumpWorker


@pytest.mark.parametrize(
    "mode", ["single_connection", "parallel_table_lock", "parallel_unsynchronized"]
)
def test_export_worker_forwards_snapshot_mode(monkeypatch, mode):
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
        mysql_snapshot_mode=mode,
    )

    worker._run_export_schema()

    assert captured["kwargs"]["mysql_snapshot_mode"] == mode


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


def test_table_export_worker_sends_exactly_the_checked_tables_with_the_fk_choice(monkeypatch):
    captured = {}
    run = {"mode": "single_connection", "method": "같은 시점으로 내보냄 · 동시 작업 1개", "omitted": []}

    class FakeExporter:
        last_run = run

        def __init__(self, config):
            self.config = config

        def export_tables(self, *args, **kwargs):
            captured["args"], captured["kwargs"] = args, kwargs
            return True, "ok", list(args[1])

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpExporter", FakeExporter)
    outside = [{"child": "orders", "constraint": "fk", "parent": "users"}]
    worker = RustDumpWorker(
        "export_tables", RustDumpConfig("localhost", 3306, "root", "password"),
        schema="app", tables=["orders", "customers"], output_dir="C:/tmp/export",
        mysql_snapshot_mode="auto", mysql_snapshot_fallback="parallel_unsynchronized",
        auto_added_tables=["customers"], fk_parents_outside_dump=outside,
    )

    worker._run_export_tables()

    assert captured["args"][5] is False, "the worker never adds FK parents unless asked"
    assert captured["kwargs"]["mysql_snapshot_fallback"] == "parallel_unsynchronized"
    assert captured["kwargs"]["auto_added_tables"] == ["customers"]
    assert captured["kwargs"]["fk_parents_outside_dump"] == outside
    assert worker.export_run == run


def test_import_preview_worker_forwards_import_options_and_emits_the_plan(monkeypatch):
    captured = {}
    plan = {"dry_run": True, "replace": ["users"], "blockers": []}

    class FakeImporter:
        last_error_code = None

        def __init__(self, config):
            self.config = config

        def preview_import(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return True, "", plan

        def import_dump(self, *args, **kwargs):
            raise AssertionError("a preview must not start an import")

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", FakeImporter)
    worker = RustDumpWorker(
        "import_preview",
        RustDumpConfig("localhost", 3306, "root", "password"),
        input_dir="C:/tmp/dump",
        target_schema="app",
        threads=4,
        timezone_sql="SET SESSION time_zone = '+09:00'",
        use_source_timezone=False,
    )
    emitted = []
    worker.preview_finished.connect(lambda *args: emitted.append(args))

    worker.run()

    assert captured["args"] == ("C:/tmp/dump", "app", 4, "SET SESSION time_zone = '+09:00'")
    assert captured["kwargs"] == {"use_source_timezone": False}
    assert emitted == [(True, "", plan)]


def test_import_preview_worker_answers_when_the_importer_cannot_start(monkeypatch):
    class BrokenImporter:
        def __init__(self, config):
            raise FileNotFoundError("tunnelforge-core not found")

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", BrokenImporter)
    worker = RustDumpWorker("import_preview", RustDumpConfig("localhost", 3306, "root", "password"), input_dir="C:/tmp/dump")
    emitted = []
    worker.preview_finished.connect(lambda *args: emitted.append(args))

    worker.run()

    assert emitted == [(False, "tunnelforge-core not found", {})]


def test_cancelled_import_preview_sends_no_plan(monkeypatch):
    class FakeImporter:
        last_error_code = None

        def __init__(self, config):
            pass

        def preview_import(self, *args, **kwargs):
            worker.cancel()
            return True, "", {"dry_run": True, "replace": ["users"], "blockers": []}

    monkeypatch.setattr("src.ui.workers.rust_dump_worker.RustDumpImporter", FakeImporter)
    worker = RustDumpWorker("import_preview", RustDumpConfig("localhost", 3306, "root", "password"), input_dir="C:/tmp/dump")
    emitted = []
    worker.preview_finished.connect(lambda *args: emitted.append(args))

    worker.run()

    assert emitted == [(False, CANCELLED_MESSAGE, {})]

