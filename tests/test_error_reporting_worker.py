import gc
import logging
import threading
import time
import weakref
import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QObject, QThread, pyqtSignal


REPORT_URL = "https://relay.example.test/v1/reports"
ISSUE_URL = "https://github.com/sanghyun-io/tunnelforge/issues/12"
TOKEN = "11111111-1111-4111-8111-111111111111"


class FakeSignal:
    def __init__(self, fail_connect=False):
        self._slots = []
        self._fail_connect = fail_connect

    def connect(self, slot, *_args):
        if self._fail_connect:
            raise RuntimeError("connect-secret")
        self._slots.append(slot)

    def emit(self, *args):
        for slot in list(self._slots):
            slot(*args)


class FakeConfigManager:
    def mutate_app_settings(self, _mutator):
        return None

    def get_app_settings_snapshot(self):
        return {}


class PersistingConfigManager(FakeConfigManager):
    def __init__(self):
        self.settings = {}

    def mutate_app_settings(self, mutator):
        changed, result = mutator(self.settings)
        return result


class EnabledPolicy:
    current_token = TOKEN

    def __init__(self, config_manager):
        self.config_manager = config_manager

    def capture_submission_token(self):
        return self.current_token

    def is_submission_token_current(self, token):
        return token == self.current_token

    def authorize_submission(self, token):
        return self.is_submission_token_current(token)


class DisabledPolicy(EnabledPolicy):
    current_token = None


def capture_report(worker):
    results = []
    worker.report_finished.connect(lambda *args: results.append(args))
    worker.run()
    return results


def worker_kwargs(**overrides):
    values = {
        "operation_kind": "export",
        "db_engine": "mysql",
        "phase": "dump.run",
        "relay_url": REPORT_URL,
    }
    values.update(overrides)
    return values


def mixin_kwargs(**overrides):
    values = worker_kwargs(**overrides)
    values.pop("relay_url")
    return values


def test_dialog_worker_sequence_leaves_global_retention_empty():
    import src.ui.workers.error_reporting_worker as worker_module

    assert worker_module._ACTIVE_ERROR_REPORT_WORKERS == set()


def process_events_until(predicate, timeout=5.0):
    app = QCoreApplication.instance() or QCoreApplication([])
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.001)
    app.processEvents()
    assert predicate()


def test_worker_does_not_shadow_inherited_qthread_finished_signal():
    from src.ui.workers.error_reporting_worker import ErrorReportingWorker

    assert "finished" not in ErrorReportingWorker.__dict__
    assert "report_finished" in ErrorReportingWorker.__dict__


def test_worker_checks_affirmative_consent_before_building_or_network(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module

    monkeypatch.setattr(worker_module, "ConsentPolicy", DisabledPolicy)
    monkeypatch.setattr(
        worker_module,
        "build_error_report",
        lambda *args, **kwargs: pytest.fail("disabled reporting must not build"),
    )
    monkeypatch.setattr(
        worker_module,
        "ErrorReportTransport",
        lambda *args, **kwargs: pytest.fail("disabled reporting must not use network"),
    )
    worker = worker_module.ErrorReportingWorker(
        FakeConfigManager(), **worker_kwargs()
    )

    assert capture_report(worker) == [
        (False, "Anonymous error reporting is disabled.", "")
    ]


@pytest.mark.parametrize(
    "relay_url",
    ["", "http://relay.example.test/v1/reports", "not-a-url"],
)
def test_worker_checks_configured_https_url_before_building_or_network(
    monkeypatch, relay_url
):
    import src.ui.workers.error_reporting_worker as worker_module

    monkeypatch.setattr(worker_module, "ConsentPolicy", EnabledPolicy)
    monkeypatch.setattr(
        worker_module,
        "build_error_report",
        lambda *args, **kwargs: pytest.fail("invalid relay must not build"),
    )
    worker = worker_module.ErrorReportingWorker(
        FakeConfigManager(), **worker_kwargs(relay_url=relay_url)
    )

    assert capture_report(worker) == [
        (False, "Anonymous error reporting is unavailable.", "")
    ]


@pytest.mark.parametrize(
    "operation_kind,phase,expected_message",
    [
        ("export", "dump.run", "Rust DB Core export operation failed."),
        ("import", "dump.import", "Rust DB Core import operation failed."),
    ],
)
def test_worker_derives_fixed_safe_message_and_submits_under_consent_lease(
    monkeypatch, operation_kind, phase, expected_message
):
    import src.ui.workers.error_reporting_worker as worker_module
    from src.core.error_report_transport import RelayResult

    built = []
    submitted = []

    def fake_build(config_manager, **kwargs):
        built.append((config_manager, kwargs))
        return {"safe": "payload"}

    class FakeTransport:
        def __init__(self, relay_url, *, submission_authorizer):
            assert relay_url == REPORT_URL
            self.submission_authorizer = submission_authorizer

        def submit(self, payload):
            assert self.submission_authorizer() is True
            submitted.append(payload)
            return RelayResult(True, "Report accepted.", "", 202)

    monkeypatch.setattr(worker_module, "ConsentPolicy", EnabledPolicy)
    monkeypatch.setattr(worker_module, "build_error_report", fake_build)
    monkeypatch.setattr(worker_module, "ErrorReportTransport", FakeTransport)
    config_manager = FakeConfigManager()
    worker = worker_module.ErrorReportingWorker(
        config_manager,
        **worker_kwargs(operation_kind=operation_kind, phase=phase),
    )

    results = capture_report(worker)

    assert built == [
        (
            config_manager,
            {
                "operation_kind": operation_kind,
                "db_engine": "mysql",
                "phase": phase,
                "error_message": expected_message,
                "exception": None,
                "db_server_version": None,
            },
        )
    ]
    assert submitted == [{"safe": "payload"}]
    assert results == [(True, "Report accepted.", "")]


def test_worker_persists_only_fixed_local_last_submission_fields(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module
    from src.core.error_report_transport import RelayResult

    class FakeTransport:
        def __init__(self, *_args, **_kwargs):
            pass

        def submit(self, _payload):
            return RelayResult(True, "remote-secret", ISSUE_URL, 201)

    monkeypatch.setattr(worker_module, "ConsentPolicy", EnabledPolicy)
    monkeypatch.setattr(worker_module, "build_error_report", lambda *_args, **_kwargs: {"safe": True})
    monkeypatch.setattr(worker_module, "ErrorReportTransport", FakeTransport)
    config_manager = PersistingConfigManager()

    worker = worker_module.ErrorReportingWorker(config_manager, **worker_kwargs())
    capture_report(worker)

    assert config_manager.settings["error_reporting_last_attempt_status"] == "submitted"
    assert config_manager.settings["error_reporting_last_attempt_issue_url"] == ISSUE_URL
    assert config_manager.settings["error_reporting_last_attempt_at"].endswith("Z")
    assert set(config_manager.settings) == {
        "error_reporting_last_attempt_status",
        "error_reporting_last_attempt_issue_url",
        "error_reporting_last_attempt_at",
    }


@pytest.mark.parametrize(
    "extra",
    [
        {"error_message": "arbitrary secret"},
        {"context": {"schema": "customer_db"}},
    ],
)
def test_worker_constructor_rejects_arbitrary_reporting_input(extra):
    from src.ui.workers.error_reporting_worker import ErrorReportingWorker

    with pytest.raises(TypeError):
        ErrorReportingWorker(FakeConfigManager(), **worker_kwargs(), **extra)


def test_mixin_api_rejects_arbitrary_error_message():
    from src.ui.workers.error_reporting_worker import ErrorReportingMixin

    class Host(ErrorReportingMixin):
        pass

    with pytest.raises(TypeError):
        Host()._start_error_report_worker(
            **mixin_kwargs(), error_message="arbitrary secret"
        )


def test_worker_failure_does_not_log_payload_or_exception_text(monkeypatch, caplog):
    import src.ui.workers.error_reporting_worker as worker_module

    payload_secret = "payload-object-secret"
    exception_secret = "network-exception-secret"

    class FailingTransport:
        def __init__(self, _relay_url, *, submission_authorizer):
            self.submission_authorizer = submission_authorizer

        def submit(self, payload):
            assert payload["secret"] == payload_secret
            raise RuntimeError(exception_secret)

    monkeypatch.setattr(worker_module, "ConsentPolicy", EnabledPolicy)
    monkeypatch.setattr(
        worker_module,
        "build_error_report",
        lambda *args, **kwargs: {"secret": payload_secret},
    )
    monkeypatch.setattr(worker_module, "ErrorReportTransport", FailingTransport)
    caplog.set_level(logging.DEBUG, logger="tunnelforge.error_reporting_worker")
    worker = worker_module.ErrorReportingWorker(
        FakeConfigManager(), **worker_kwargs()
    )

    results = capture_report(worker)

    assert results == [(False, "Anonymous error report could not be sent.", "")]
    assert payload_secret not in caplog.text
    assert exception_secret not in caplog.text


@pytest.mark.parametrize("reenable", [False, True])
def test_worker_with_revoked_pending_token_never_enters_transport_request(
    monkeypatch, reenable
):
    import src.ui.workers.error_reporting_worker as worker_module
    from src.core.error_report_transport import RelayResult

    class MutablePolicy(EnabledPolicy):
        current_token = TOKEN

    policy = MutablePolicy(FakeConfigManager())

    def build_then_revoke(*_args, **_kwargs):
        policy.current_token = None
        if reenable:
            policy.current_token = "22222222-2222-4222-8222-222222222222"
        return {"safe": "payload"}

    class LeaseAwareTransport:
        def __init__(self, _relay_url, *, submission_authorizer):
            self.submission_authorizer = submission_authorizer

        def submit(self, _payload):
            if self.submission_authorizer():
                pytest.fail("revoked pending worker must not initiate POST")
            return RelayResult(False, "Relay request cancelled.", "", None)

    monkeypatch.setattr(worker_module, "ConsentPolicy", lambda _config: policy)
    monkeypatch.setattr(worker_module, "build_error_report", build_then_revoke)
    monkeypatch.setattr(worker_module, "ErrorReportTransport", LeaseAwareTransport)
    worker = worker_module.ErrorReportingWorker(
        FakeConfigManager(), **worker_kwargs()
    )

    assert capture_report(worker) == [
        (False, "Anonymous error reporting consent changed.", "")
    ]


def test_mixin_delivers_result_but_retains_until_inherited_finished(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class FakeWorker:
        def __init__(self, _config_manager, **_kwargs):
            self.report_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.deleted = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            assert not self.running
            self.deleted = True

    class Host(worker_module.ErrorReportingMixin):
        def __init__(self):
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []
            self.logs = []
            self.log_threads = []

        def _add_log(self, message):
            self.logs.append(message)
            self.log_threads.append(QThread.currentThread())

    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FakeWorker)
    host = Host()
    host._start_error_report_worker(**mixin_kwargs())
    worker = created[0]

    worker.report_finished.emit(True, "remote-secret", ISSUE_URL)

    assert host.logs == ["Anonymous error report submitted."]
    assert host._error_report_workers == [worker]
    assert worker in worker_module._ACTIVE_ERROR_REPORT_WORKERS
    assert worker.deleted is False

    worker.running = False
    worker.finished.emit()

    assert host._error_report_workers == []
    assert worker not in worker_module._ACTIVE_ERROR_REPORT_WORKERS
    assert worker.deleted is True


def test_delayed_prior_operation_result_is_not_logged_but_workers_are_cleaned(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class FakeWorker:
        def __init__(self, _config_manager, **_kwargs):
            self.report_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.deleted = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            self.deleted = True

    class Host(worker_module.ErrorReportingMixin):
        def __init__(self):
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []
            self.logs = []

        def _add_log(self, message):
            self.logs.append(message)

    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FakeWorker)
    host = Host()
    host._begin_error_report_operation()
    host._start_error_report_worker(**mixin_kwargs())
    operation_a_worker = created[-1]

    host._begin_error_report_operation()
    host._start_error_report_worker(**mixin_kwargs())
    operation_b_worker = created[-1]

    operation_a_worker.report_finished.emit(True, "remote-a", ISSUE_URL)
    operation_b_worker.report_finished.emit(True, "remote-b", ISSUE_URL)

    assert host.logs == ["Anonymous error report submitted."]

    for worker in (operation_a_worker, operation_b_worker):
        worker.running = False
        worker.finished.emit()

    assert host._error_report_workers == []
    assert operation_a_worker.deleted is True
    assert operation_b_worker.deleted is True


def test_repeated_lifecycle_callbacks_cleanup_worker_once(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class FakeWorker:
        def __init__(self, _config_manager, **_kwargs):
            self.report_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.delete_calls = 0
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            assert not self.running
            self.delete_calls += 1

    class CountingList(list):
        def __init__(self):
            super().__init__()
            self.remove_calls = 0

        def remove(self, item):
            self.remove_calls += 1
            super().remove(item)

    class CountingSet(set):
        def __init__(self):
            super().__init__()
            self.discard_calls = 0

        def discard(self, item):
            self.discard_calls += 1
            super().discard(item)

    class Host(worker_module.ErrorReportingMixin):
        def __init__(self):
            self.config_manager = FakeConfigManager()
            self._error_report_workers = CountingList()

        def _add_log(self, _message):
            pass

    active_workers = CountingSet()
    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FakeWorker)
    monkeypatch.setattr(
        worker_module, "_ACTIVE_ERROR_REPORT_WORKERS", active_workers
    )
    host = Host()
    host._start_error_report_worker(**mixin_kwargs())
    worker = created[0]

    worker.running = False
    worker.finished.emit()
    worker.finished.emit()

    assert host._error_report_workers == []
    assert host._error_report_workers.remove_calls == 1
    assert active_workers == set()
    assert active_workers.discard_calls == 1
    assert worker.delete_calls == 1


def test_real_qthread_result_does_not_release_retention_while_running(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    import src.ui.workers.error_reporting_worker as worker_module

    result_emitted = threading.Event()
    release_run = threading.Event()

    class BlockingWorker(QThread):
        report_finished = pyqtSignal(bool, str, str)

        def __init__(self, _config_manager, **_kwargs):
            super().__init__()

        def run(self):
            self.report_finished.emit(True, "remote-secret", ISSUE_URL)
            result_emitted.set()
            release_run.wait(5)

    class Host(QObject, worker_module.ErrorReportingMixin):
        def __init__(self):
            super().__init__()
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []
            self.logs = []
            self.log_threads = []

        def _add_log(self, message):
            self.logs.append(message)
            self.log_threads.append(QThread.currentThread())

    monkeypatch.setattr(worker_module, "ErrorReportingWorker", BlockingWorker)
    host = Host()
    host._start_error_report_worker(**mixin_kwargs())
    worker = host._error_report_workers[0]
    assert result_emitted.wait(5)
    process_events_until(lambda: bool(host.logs))

    assert worker.isRunning() is True
    assert host._error_report_workers == [worker]
    assert host.logs == ["Anonymous error report submitted."]
    assert host.log_threads == [app.thread()]

    release_run.set()
    process_events_until(lambda: not host._error_report_workers)
    assert sip.isdeleted(worker) is True
    assert app is QCoreApplication.instance()


def test_high_iteration_real_qthread_lifecycle_cleans_only_after_stop(monkeypatch):
    app = QCoreApplication.instance() or QCoreApplication([])
    import src.ui.workers.error_reporting_worker as worker_module

    cleanup_states = []

    class FastWorker(QThread):
        report_finished = pyqtSignal(bool, str, str)

        def __init__(self, _config_manager, **_kwargs):
            super().__init__()

        def run(self):
            self.report_finished.emit(True, "remote-secret", ISSUE_URL)

        def deleteLater(self):
            cleanup_states.append(self.isRunning())
            super().deleteLater()

    class Host(QObject, worker_module.ErrorReportingMixin):
        def __init__(self):
            super().__init__()
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []
            self.logs = []

        def _add_log(self, message):
            self.logs.append(message)

    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FastWorker)
    host = Host()

    for _ in range(200):
        host._start_error_report_worker(**mixin_kwargs())

    process_events_until(
        lambda: not host._error_report_workers
        and not worker_module._ACTIVE_ERROR_REPORT_WORKERS
        and len(cleanup_states) == 200,
        timeout=10,
    )

    assert cleanup_states == [False] * 200
    assert host.logs == ["Anonymous error report submitted."] * 200
    assert app is QCoreApplication.instance()


@pytest.mark.parametrize("receiver_kind", ["garbage_collected", "cpp_deleted"])
def test_completion_is_safe_after_receiver_is_deleted(monkeypatch, receiver_kind):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class FakeWorker:
        def __init__(self, _config_manager, **_kwargs):
            self.report_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.deleted = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            assert not self.running
            self.deleted = True

    class Host(QObject, worker_module.ErrorReportingMixin):
        def __init__(self):
            super().__init__()
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []

        def _add_log(self, _message):
            raise AssertionError("deleted receiver must not be called")

    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FakeWorker)
    host = Host()
    host._start_error_report_worker(**mixin_kwargs())
    retained_workers = host._error_report_workers
    host_ref = weakref.ref(host)
    if receiver_kind == "garbage_collected":
        del host
        gc.collect()
        assert host_ref() is None
    else:
        sip.delete(host)
        assert sip.isdeleted(host) is True

    worker = created[0]
    worker.report_finished.emit(False, "remote-secret", "")
    worker.running = False
    worker.finished.emit()

    assert worker.deleted is True
    assert retained_workers == []


@pytest.mark.parametrize(
    "failure_stage",
    [
        "construction",
        "local_retention",
        "global_retention",
        "result_connect",
        "lifecycle_connect",
        "start",
    ],
)
def test_mixin_contains_startup_failures_and_removes_nonrunning_partial_state(
    monkeypatch, failure_stage
):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class FakeWorker:
        def __init__(self, _config_manager, **_kwargs):
            if failure_stage == "construction":
                raise RuntimeError("construction-secret")
            self.report_finished = FakeSignal(failure_stage == "result_connect")
            self.finished = FakeSignal(failure_stage == "lifecycle_connect")
            self.deleted = False
            created.append(self)

        def start(self):
            if failure_stage == "start":
                raise RuntimeError("start-secret")

        def isRunning(self):
            return False

        def deleteLater(self):
            self.deleted = True

    class FailingAppendList(list):
        def append(self, _worker):
            raise RuntimeError("retention-secret")

    class FailingSet(set):
        def add(self, _worker):
            raise RuntimeError("global-secret")

    class Host(worker_module.ErrorReportingMixin):
        def __init__(self):
            self.config_manager = FakeConfigManager()
            self._error_report_workers = (
                FailingAppendList() if failure_stage == "local_retention" else []
            )
            self.logs = []

        def _add_log(self, message):
            self.logs.append(message)

    active_workers = FailingSet() if failure_stage == "global_retention" else set()
    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FakeWorker)
    monkeypatch.setattr(worker_module, "_ACTIVE_ERROR_REPORT_WORKERS", active_workers)
    host = Host()

    host._start_error_report_worker(**mixin_kwargs())

    assert list(host._error_report_workers) == []
    assert list(active_workers) == []
    assert host.logs == ["Anonymous error reporting could not be started."]
    if created:
        assert created[0].deleted is True


def test_start_exception_after_thread_begins_retains_until_lifecycle_finished(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class StartedThenRaisedWorker:
        def __init__(self, _config_manager, **_kwargs):
            self.report_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.deleted = False
            created.append(self)

        def start(self):
            self.running = True
            raise RuntimeError("start-secret")

        def isRunning(self):
            return self.running

        def deleteLater(self):
            assert not self.running
            self.deleted = True

    class Host(worker_module.ErrorReportingMixin):
        def __init__(self):
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []
            self.logs = []

        def _add_log(self, message):
            self.logs.append(message)

    monkeypatch.setattr(
        worker_module, "ErrorReportingWorker", StartedThenRaisedWorker
    )
    host = Host()
    host._start_error_report_worker(**mixin_kwargs())
    worker = created[0]

    assert host._error_report_workers == [worker]
    assert worker.deleted is False
    assert host.logs == ["Anonymous error reporting could not be started."]

    worker.running = False
    worker.finished.emit()

    assert host._error_report_workers == []
    assert worker.deleted is True


def test_result_callback_failure_cannot_prevent_lifecycle_cleanup(monkeypatch):
    import src.ui.workers.error_reporting_worker as worker_module

    created = []

    class FakeWorker:
        def __init__(self, _config_manager, **_kwargs):
            self.report_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.deleted = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            self.deleted = True

    class Host(worker_module.ErrorReportingMixin):
        def __init__(self):
            self.config_manager = FakeConfigManager()
            self._error_report_workers = []

        def _on_error_report_finished(self, *_args):
            raise ValueError("completion-secret")

    monkeypatch.setattr(worker_module, "ErrorReportingWorker", FakeWorker)
    host = Host()
    host._start_error_report_worker(**mixin_kwargs())
    worker = created[0]

    worker.report_finished.emit(False, "remote-secret", "")
    assert host._error_report_workers == [worker]

    worker.running = False
    worker.finished.emit()

    assert host._error_report_workers == []
    assert worker.deleted is True
