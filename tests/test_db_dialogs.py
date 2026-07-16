import inspect
import threading
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication, QDialog, QListWidgetItem, QMessageBox

from src.core.db_core_service import (
    DbCoreOutcome,
    DbCoreRequestKind,
    DbCoreServiceError,
    is_db_core_facade_retained,
)
from src.ui.dialogs.db_dialogs import RustDumpWizard
from src.ui.dialogs.db_import_dialog import RustDumpImportDialog
from src.exporters.rust_dump_exporter import RustDumpConfig
from src.ui.workers.rust_dump_worker import RustDumpWorker


def _service_error(message, *, code, outcome):
    return DbCoreServiceError(
        message,
        code=code,
        request_kind=DbCoreRequestKind.MUTATION,
        outcome=outcome,
        request_id="test-request",
        process_generation=1,
        rust_code=None,
        payload={},
    )

def test_preselected_export_tunnel_passes_mysql_default_database(monkeypatch):
    captured = {}

    class FakeMySQLConnector:
        def __init__(self, host, port, user, password, database=None):
            captured["host"] = host
            captured["port"] = port
            captured["user"] = user
            captured["password"] = password
            captured["database"] = database

        def connect(self):
            return True, "ok"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.MySQLConnector", FakeMySQLConnector)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("root", "tunnelpass")
    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 3309)

    wizard = RustDumpWizard(
        tunnel_engine=tunnel_engine,
        config_manager=config_manager,
        preselected_tunnel={
            "id": "mysql-tunnel",
            "name": "MySQL 터널",
            "db_engine": "mysql",
            "default_database": "tf_source84",
        },
    )

    connector, connection_info = wizard._connect_preselected_tunnel()

    assert connector is not None
    assert connection_info == "MySQL 터널_root"
    assert captured == {
        "host": "127.0.0.1",
        "port": 3309,
        "user": "root",
        "password": "tunnelpass",
        "database": "tf_source84",
    }

def test_preselected_export_tunnel_uses_postgres_connector_for_postgresql(monkeypatch):
    captured = {}

    class FailingMySQLConnector:
        def __init__(self, *args, **kwargs):
            raise AssertionError("PostgreSQL tunnel must not create MySQLConnector")

    class FakePostgresConnector:
        engine = "postgresql"

        def __init__(self, host, port, user, password, database=None):
            captured["host"] = host
            captured["port"] = port
            captured["user"] = user
            captured["password"] = password
            captured["database"] = database

        def connect(self):
            return True, "ok"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.MySQLConnector", FailingMySQLConnector)
    monkeypatch.setattr("src.ui.dialogs.db_dialogs.PostgresConnector", FakePostgresConnector)
    config_manager = MagicMock()
    config_manager.get_tunnel_credentials.return_value = ("postgres", "tunnelpass")
    tunnel_engine = MagicMock()
    tunnel_engine.is_running.return_value = True
    tunnel_engine.get_connection_info.return_value = ("127.0.0.1", 55432)

    wizard = RustDumpWizard(
        tunnel_engine=tunnel_engine,
        config_manager=config_manager,
        preselected_tunnel={
            "id": "pg-tunnel",
            "name": "PostgreSQL 터널",
            "db_engine": "postgresql",
            "default_database": "postgres",
            "default_schema": "public",
        },
    )

    connector, connection_info = wizard._connect_preselected_tunnel()

    assert connector is not None
    assert connector.engine == "postgresql"
    assert connection_info == "PostgreSQL 터널_postgres"
    assert captured == {
        "host": "127.0.0.1",
        "port": 55432,
        "user": "postgres",
        "password": "tunnelpass",
        "database": "postgres",
    }

def _make_worker_with_fake_runner(owns_facade: bool):
    config = RustDumpConfig(host="127.0.0.1", port=3306, user="root", password="pw", engine="mysql")
    worker = RustDumpWorker("export_schema", config)

    class FakeClient:
        def __init__(self):
            self.cancel_calls = []
            self.shutdown_calls = []

        def cancel_active_request(self, *, timeout_seconds):
            self.cancel_calls.append(timeout_seconds)
            return True

        def shutdown(self, *, timeout_seconds):
            self.shutdown_calls.append(timeout_seconds)

    class FakeFacade:
        def __init__(self, client):
            self.client = client

    class FakeRunner:
        def __init__(self, facade, owns):
            self.facade = facade
            self._owns_facade = owns

    client = FakeClient()
    runner = FakeRunner(FakeFacade(client), owns_facade)
    worker._active_runner = runner
    return worker, runner, client


def test_rust_dump_worker_cancel_shuts_down_owned_dedicated_process_directly():
    worker, _runner, client = _make_worker_with_fake_runner(owns_facade=True)

    result = worker.cancel()

    assert result is True
    assert worker._cancel_requested is True
    assert client.cancel_calls == []
    assert client.shutdown_calls == [5.0]

def test_rust_dump_worker_cancel_does_not_touch_shared_facade_process():
    worker, _runner, client = _make_worker_with_fake_runner(owns_facade=False)

    result = worker.cancel()

    assert result is True
    assert worker._cancel_requested is True
    assert client.cancel_calls == []
    assert client.shutdown_calls == []


def test_rust_dump_worker_cancel_has_no_private_process_access():
    source = inspect.getsource(RustDumpWorker.cancel) + inspect.getsource(
        RustDumpWorker._cancel_owned_runner
    )

    assert "_process" not in source
    assert ".poll(" not in source
    assert ".terminate(" not in source
    assert "_owns_facade" in source
    assert "cancel_active_request" not in source
    assert ".shutdown(" in source


def test_rust_dump_worker_owned_cancel_closes_request_admission_without_gap():
    worker, _runner, _client = _make_worker_with_fake_runner(owns_facade=True)
    admission_reached = threading.Event()
    release_legacy_cancel = threading.Event()
    request_admitted = []

    class AdmissionAtomicClient:
        def __init__(self):
            self._lock = threading.Lock()
            self.shutdown_started = False
            self.cancel_calls = []
            self.shutdown_calls = []

        def cancel_active_request(self, *, timeout_seconds):
            self.cancel_calls.append(timeout_seconds)
            admission_reached.set()
            release_legacy_cancel.wait(timeout=2.0)
            return False

        def shutdown(self, *, timeout_seconds):
            with self._lock:
                self.shutdown_started = True
                self.shutdown_calls.append(timeout_seconds)
                admission_reached.set()

        def try_admit_request(self):
            with self._lock:
                admitted = not self.shutdown_started
                request_admitted.append(admitted)

    client = AdmissionAtomicClient()
    worker._active_runner.facade.client = client
    cancel_thread = threading.Thread(target=worker.cancel)

    cancel_thread.start()
    assert admission_reached.wait(timeout=2.0)
    client.try_admit_request()
    release_legacy_cancel.set()
    cancel_thread.join(timeout=2.0)

    assert not cancel_thread.is_alive()
    assert request_admitted == [False]
    assert client.cancel_calls == []
    assert client.shutdown_calls == [5.0]


def test_rust_dump_worker_retains_failed_runner_until_explicit_retry():
    from src.core.db_core_service import (
        DbCoreOutcome,
        DbCoreServiceError,
        retain_db_core_facade_for_retry,
    )

    residual = _service_error(
        "owner still alive",
        code="db_core_residual_process",
        outcome=DbCoreOutcome.FAILED,
    )
    worker, _runner, client = _make_worker_with_fake_runner(owns_facade=True)
    worker.task_type = "noop"
    runner = worker._active_runner
    retain_db_core_facade_for_retry(runner.facade)
    client.shutdown = MagicMock(side_effect=[residual, None])

    worker.run()
    assert worker._active_runner is runner

    with pytest.raises(DbCoreServiceError):
        worker.retry_owned_shutdown(timeout_seconds=0.05)
    assert worker._active_runner is runner

    assert worker.retry_owned_shutdown(timeout_seconds=0.05) is True
    assert worker._active_runner is None


@pytest.mark.parametrize("task_type", ["export_schema", "export_tables", "import"])
def test_rust_dump_worker_cancel_before_run_skips_mutation_setup_and_finishes_once(
    monkeypatch, task_type
):
    import src.ui.workers.rust_dump_worker as worker_module

    constructed = []

    class FailingRunner:
        def __init__(self, *args, **kwargs):
            constructed.append((args, kwargs))
            raise AssertionError("cancel-before-run must not construct a DB Core runner")

    monkeypatch.setattr(worker_module, "RustDumpExporter", FailingRunner)
    monkeypatch.setattr(worker_module, "RustDumpImporter", FailingRunner)

    config = RustDumpConfig("127.0.0.1", 3306, "root", "pw")
    worker = RustDumpWorker(
        task_type,
        config,
        schema="app",
        tables=["users"],
        output_dir="C:/dump",
        input_dir="C:/dump",
    )
    finished = []
    import_finished = []
    worker.finished.connect(lambda success, message: finished.append((success, message)))
    worker.import_finished.connect(
        lambda success, message, results: import_finished.append(
            (success, message, results)
        )
    )

    worker.cancel()
    worker.run()
    worker.run()

    assert constructed == []
    assert finished == [(False, "작업이 취소되었습니다.")]
    if task_type == "import":
        assert import_finished == [(False, "작업이 취소되었습니다.", {})]
    else:
        assert import_finished == []


@pytest.mark.parametrize(
    ("task_type", "method_name"),
    [
        ("export_schema", "export_full_schema"),
        ("export_tables", "export_tables"),
        ("import", "import_dump"),
    ],
)
def test_rust_dump_worker_cancel_during_runner_creation_cancels_published_runner_without_db_work(
    monkeypatch, task_type, method_name
):
    import src.ui.workers.rust_dump_worker as worker_module

    cancel_calls = []
    shutdown_calls = []
    db_calls = []
    worker = None

    class FakeClient:
        def cancel_active_request(self, *, timeout_seconds):
            cancel_calls.append(timeout_seconds)
            return True

        def shutdown(self, *, timeout_seconds):
            shutdown_calls.append(timeout_seconds)

    class FakeFacade:
        def __init__(self):
            self.client = FakeClient()

    class CancellingRunner:
        def __init__(self, config):
            self.facade = FakeFacade()
            self._owns_facade = True
            self.last_error_metadata = None
            worker.cancel()

        def export_full_schema(self, *args, **kwargs):
            db_calls.append("export_full_schema")
            return True, "ok"

        def export_tables(self, *args, **kwargs):
            db_calls.append("export_tables")
            return True, "ok", ["users"]

        def import_dump(self, *args, **kwargs):
            db_calls.append("import_dump")
            return True, "ok", {"users": {"status": "done", "message": ""}}

    monkeypatch.setattr(worker_module, "RustDumpExporter", CancellingRunner)
    monkeypatch.setattr(worker_module, "RustDumpImporter", CancellingRunner)
    config = RustDumpConfig("127.0.0.1", 3306, "root", "pw")
    worker = RustDumpWorker(
        task_type,
        config,
        schema="app",
        tables=["users"],
        output_dir="C:/dump",
        input_dir="C:/dump",
    )
    finished = []
    import_finished = []
    worker.finished.connect(lambda success, message: finished.append((success, message)))
    worker.import_finished.connect(
        lambda success, message, results: import_finished.append(
            (success, message, results)
        )
    )

    worker.run()
    worker.run()

    assert (cancel_calls, shutdown_calls, db_calls) == ([], [5.0], []), method_name
    assert finished == [(False, "작업이 취소되었습니다.")]
    if task_type == "import":
        assert import_finished == [(False, "작업이 취소되었습니다.", {})]
    else:
        assert import_finished == []


@pytest.mark.parametrize(
    ("task_type", "method_name"),
    [
        ("export_schema", "export_full_schema"),
        ("export_tables", "export_tables"),
        ("import", "import_dump"),
    ],
)
def test_rust_dump_worker_cancel_after_runner_publication_skips_db_work(
    monkeypatch, task_type, method_name
):
    import src.ui.workers.rust_dump_worker as worker_module

    cancel_calls = []
    shutdown_calls = []
    db_calls = []

    class FakeClient:
        def cancel_active_request(self, *, timeout_seconds):
            cancel_calls.append(timeout_seconds)
            return True

        def shutdown(self, *, timeout_seconds):
            shutdown_calls.append(timeout_seconds)

    class FakeFacade:
        def __init__(self):
            self.client = FakeClient()

    class FakeRunner:
        def __init__(self, config):
            self.facade = FakeFacade()
            self._owns_facade = True
            self.last_error_metadata = None

        def export_full_schema(self, *args, **kwargs):
            db_calls.append("export_full_schema")
            return True, "ok"

        def export_tables(self, *args, **kwargs):
            db_calls.append("export_tables")
            return True, "ok", ["users"]

        def import_dump(self, *args, **kwargs):
            db_calls.append("import_dump")
            return True, "ok", {"users": {"status": "done", "message": ""}}

    monkeypatch.setattr(worker_module, "RustDumpExporter", FakeRunner)
    monkeypatch.setattr(worker_module, "RustDumpImporter", FakeRunner)
    config = RustDumpConfig("127.0.0.1", 3306, "root", "pw")
    worker = RustDumpWorker(
        task_type,
        config,
        schema="app",
        tables=["users"],
        output_dir="C:/dump",
        input_dir="C:/dump",
    )
    publish_runner = worker._publish_runner

    def cancel_at_publication_barrier(runner):
        published = publish_runner(runner)
        worker.cancel()
        return published

    worker._publish_runner = cancel_at_publication_barrier
    finished = []
    import_finished = []
    worker.finished.connect(lambda success, message: finished.append((success, message)))
    worker.import_finished.connect(
        lambda success, message, results: import_finished.append(
            (success, message, results)
        )
    )

    worker.run()
    worker.run()

    assert db_calls == [], method_name
    assert cancel_calls == []
    assert shutdown_calls == [5.0, 5.0]
    assert finished == [(False, "작업이 취소되었습니다.")]
    if task_type == "import":
        assert import_finished == [(False, "작업이 취소되었습니다.", {})]
    else:
        assert import_finished == []


def test_rust_dump_worker_idle_cancel_in_post_check_gap_shuts_down_before_facade_request(
    monkeypatch,
):
    import src.ui.workers.rust_dump_worker as worker_module

    cancel_calls = []
    shutdown_calls = []
    facade_requests = []
    db_calls = []
    runner_calls = []
    worker = None

    class IdleClient:
        def __init__(self):
            self.shutdown_started = False

        def cancel_active_request(self, *, timeout_seconds):
            cancel_calls.append(timeout_seconds)
            return False

        def shutdown(self, *, timeout_seconds):
            shutdown_calls.append(timeout_seconds)
            self.shutdown_started = True

    class FailClosedFacade:
        def __init__(self):
            self.client = IdleClient()

        def import_dump(self):
            if self.client.shutdown_started:
                raise DbCoreServiceError(
                    "dedicated client is shut down",
                    code="db_core_process_closed",
                    request_kind=DbCoreRequestKind.MUTATION,
                    outcome=DbCoreOutcome.NOT_STARTED,
                    request_id="idle-cancel",
                    process_generation=1,
                    rust_code=None,
                    payload={"stage": "post_check_gap"},
                )
            facade_requests.append("dump.import")
            db_calls.append("import")
            return True, "ok", {}

    class GapImporter:
        def __init__(self, config):
            self.facade = FailClosedFacade()
            self._owns_facade = True
            self.last_error_metadata = None

        def import_dump(self, *args, **kwargs):
            runner_calls.append("import_dump")
            worker.cancel()
            return self.facade.import_dump()

    monkeypatch.setattr(worker_module, "RustDumpImporter", GapImporter)
    worker = RustDumpWorker(
        "import",
        RustDumpConfig("127.0.0.1", 3306, "root", "pw"),
        input_dir="C:/dump",
    )
    finished = []
    import_finished = []
    worker.finished.connect(lambda success, message: finished.append((success, message)))
    worker.import_finished.connect(
        lambda success, message, results: import_finished.append(
            (success, message, results)
        )
    )

    worker.run()

    assert runner_calls == ["import_dump"]
    assert cancel_calls == []
    assert shutdown_calls == [5.0]
    assert facade_requests == []
    assert db_calls == []
    assert finished == [(False, "작업이 취소되었습니다.")]
    assert import_finished == [(False, "작업이 취소되었습니다.", {})]


def test_rust_dump_worker_idle_cancel_retains_owned_facade_on_shutdown_failure():
    residual = _service_error(
        "owner still alive",
        code="db_core_residual_process",
        outcome=DbCoreOutcome.FAILED,
    )
    worker, runner, client = _make_worker_with_fake_runner(owns_facade=True)
    client.shutdown = MagicMock(side_effect=[residual, None])

    with pytest.raises(DbCoreServiceError) as raised:
        worker.cancel()

    assert raised.value is residual
    assert is_db_core_facade_retained(runner.facade)
    assert worker._active_runner is runner
    assert worker.retry_owned_shutdown(timeout_seconds=0.05) is True
    assert worker._active_runner is None


def test_import_dialog_never_offers_or_relaunches_indeterminate_table_retry(monkeypatch):
    from src.core.db_core_service import DbCoreOutcome, DbCoreRequestKind
    from src.exporters.rust_dump_exporter import (
        RUST_DUMP_ERROR_METADATA_KEY,
        RustDumpErrorMetadata,
    )

    app = QApplication.instance() or QApplication([])
    monkeypatch.setattr(
        "src.ui.dialogs.db_import_dialog.check_rust_dump",
        lambda: (True, "Rust DB Core OK"),
    )
    dialog = RustDumpImportDialog()
    try:
        metadata = RustDumpErrorMetadata(
            code="db_core_transport_failed",
            outcome=DbCoreOutcome.OUTCOME_INDETERMINATE,
            request_kind=DbCoreRequestKind.MUTATION,
            request_id="request-1",
            process_generation=3,
            rust_code="transport_lost",
        )
        item = QListWidgetItem("users")
        dialog.table_list.addItem(item)
        dialog.table_items["users"] = item
        item.setSelected(True)
        dialog.on_import_finished(
            False,
            "transport outcome is unknown",
            {
                "users": {"status": "error", "message": "unknown"},
                RUST_DUMP_ERROR_METADATA_KEY: metadata,
            },
        )

        assert not dialog.btn_retry.isVisible()
        assert not dialog.btn_select_failed.isVisible()

        warnings = []
        monkeypatch.setattr(
            "src.ui.dialogs.db_import_dialog.QMessageBox.warning",
            lambda *args, **kwargs: warnings.append(args),
        )
        dialog.do_import()
        assert dialog._table_retry_is_blocked()

        dialog.do_import = MagicMock()
        dialog.do_retry()

        assert not dialog.do_import.called
        assert warnings
    finally:
        dialog.close()

def test_start_orphan_check_disconnects_connector_after_dialog_exec(monkeypatch):
    app = QApplication.instance() or QApplication([])

    class FakeConnector:
        def __init__(self):
            self.disconnect_calls = 0

        def get_schemas(self):
            return []

        def disconnect(self):
            self.disconnect_calls += 1

    connector = FakeConnector()

    wizard = RustDumpWizard(preselected_tunnel={"id": "t1"})
    monkeypatch.setattr(
        wizard, "_connect_preselected_tunnel", lambda: (connector, "info")
    )
    monkeypatch.setattr(
        "src.ui.dialogs.db_dialogs.OrphanRecordDialog.exec", lambda self: None
    )

    result = wizard.start_orphan_check()

    assert result is True
    assert connector.disconnect_calls == 1


def test_resolve_connector_uses_preselected_tunnel_hook(monkeypatch):
    connector = object()
    calls = []

    wizard = RustDumpWizard(preselected_tunnel={"id": "t1"})
    monkeypatch.setattr(
        wizard,
        "_connect_preselected_tunnel",
        lambda: calls.append("called") or (connector, "tunnel_user"),
    )

    resolved = wizard._resolve_connector(need_connection_info=True)

    assert resolved == (connector, "tunnel_user")
    assert calls == ["called"]


def test_resolve_connector_only_reads_dialog_identifier_when_requested(monkeypatch):
    connector = object()
    instances = []

    class FakeDBConnectionDialog:
        def __init__(self, parent=None, tunnel_engine=None, config_manager=None):
            self.parent = parent
            self.tunnel_engine = tunnel_engine
            self.config_manager = config_manager
            self.identifier_calls = 0
            instances.append(self)

        def exec(self):
            return QDialog.DialogCode.Accepted

        def get_connector(self):
            return connector

        def get_connection_identifier(self):
            self.identifier_calls += 1
            return "dialog_conn"

    monkeypatch.setattr("src.ui.dialogs.db_dialogs.DBConnectionDialog", FakeDBConnectionDialog)

    parent = object()
    tunnel_engine = object()
    config_manager = object()
    wizard = RustDumpWizard(parent, tunnel_engine, config_manager)

    assert wizard._resolve_connector() == (connector, None)
    assert instances[-1].identifier_calls == 0

    assert wizard._resolve_connector(need_connection_info=True) == (connector, "dialog_conn")
    assert instances[-1].identifier_calls == 1
    assert instances[-1].parent is parent
    assert instances[-1].tunnel_engine is tunnel_engine
    assert instances[-1].config_manager is config_manager
