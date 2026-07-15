import inspect
import json
import uuid
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import QApplication, QDialog

from src.core.error_report_consent import ConsentState
from src.ui.dialogs import settings
from src.ui.dialogs.settings import SettingsDialog


ISSUE_URL = "https://github.com/sanghyun-io/tunnelforge/issues/42"


class FakeConfigManager:
    def __init__(self, settings=None):
        self.settings = dict(settings or {})
        self.set_calls = []

    def get_app_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_app_setting(self, key, value):
        self.set_calls.append((key, value))
        self.settings[key] = value

    def get_app_settings_snapshot(self):
        return dict(self.settings)

    def list_backups(self):
        return []


class FakeSignal:
    def __init__(self):
        self.slots = []

    def connect(self, slot, *_args):
        self.slots.append(slot)

    def emit(self, *args):
        for slot in list(self.slots):
            slot(*args)


def test_error_reporting_group_uses_local_controls_and_exact_settings_path():
    source = inspect.getsource(SettingsDialog._build_error_reporting_group)

    assert "settings.error_reporting.title" in source
    assert "settings.error_reporting.settings_path" in source
    assert "_show_error_reporting_preview" in source
    assert "_start_error_reporting_health_check" in source
    assert "GitHub App" not in source
    assert "GITHUB_APP" not in source


def test_opening_or_saving_unrelated_settings_never_mutates_consent(monkeypatch):
    policy = MagicMock()
    policy.is_enabled.return_value = False
    config = FakeConfigManager()
    dialog = MagicMock()
    dialog.config_mgr = config
    dialog.chk_error_reporting.isChecked.return_value = False

    monkeypatch.setattr(settings, "ConsentPolicy", lambda _config: policy)

    SettingsDialog._load_error_reporting_settings(dialog)
    SettingsDialog._save_error_reporting_choice(dialog)

    policy.set_enabled.assert_not_called()


@pytest.mark.parametrize("enabled", [True, False])
def test_explicit_checkbox_action_is_the_only_consent_mutation(monkeypatch, enabled):
    policy = MagicMock()
    dialog = MagicMock()
    dialog.config_mgr = FakeConfigManager()

    monkeypatch.setattr(settings, "ConsentPolicy", lambda _config: policy)

    SettingsDialog._on_error_reporting_checkbox_clicked(dialog, enabled)

    policy.set_enabled.assert_called_once_with(enabled)


@pytest.mark.parametrize(
    "installation_id",
    ["1d7c4a31-9bf1-4f5b-9234-6c3f0df3d879", None],
)
def test_local_preview_uses_real_builder_without_config_writes(
    monkeypatch, installation_id
):
    shown = []

    class Dialog:
        config_mgr = FakeConfigManager(
            {"error_reporting_installation_id": installation_id}
            if installation_id is not None
            else None
        )

        def _show_read_only_error_reporting_preview(self, text):
            shown.append(text)

    dialog = Dialog()

    monkeypatch.setattr(
        settings,
        "ErrorReportTransport",
        lambda *_args, **_kwargs: pytest.fail("local preview must not create transport"),
    )
    SettingsDialog._show_error_reporting_preview(dialog)

    payload = json.loads(shown[0])

    assert dialog.config_mgr.set_calls == []
    if installation_id is not None:
        assert payload["report"]["anonymous_installation_id"] == installation_id
    else:
        assert uuid.UUID(payload["report"]["anonymous_installation_id"]).version == 4


def test_unconfigured_relay_disables_opt_in_and_health_but_keeps_preview(monkeypatch):
    monkeypatch.setattr(settings, "ERROR_REPORT_RELAY_URL", "")
    app = QApplication.instance() or QApplication([])
    dialog = SettingsDialog.__new__(SettingsDialog)
    QDialog.__init__(dialog)
    dialog.config_mgr = FakeConfigManager()

    group = SettingsDialog._build_error_reporting_group(dialog)

    assert group.title()
    assert dialog.chk_error_reporting.isEnabled() is False
    assert dialog.btn_error_reporting_health.isEnabled() is False
    assert dialog.btn_error_reporting_preview.isEnabled() is True
    assert app is QApplication.instance()


def test_last_attempt_display_exposes_only_fixed_status_time_and_canonical_issue_url():
    dialog = MagicMock()
    dialog.config_mgr = FakeConfigManager(
        {
            "error_reporting_last_attempt_status": "submitted",
            "error_reporting_last_attempt_at": "2026-07-14T00:00:00Z",
            "error_reporting_last_attempt_issue_url": ISSUE_URL,
            "error_reporting_last_attempt_receipt": "must-not-display",
        }
    )

    details = SettingsDialog._last_error_reporting_attempt(dialog)

    assert details == ("submitted", "2026-07-14T00:00:00Z", ISSUE_URL)


def test_health_worker_never_captures_consent_or_updates_last_submission(monkeypatch):
    config = FakeConfigManager()
    transport = MagicMock()
    transport.health.return_value.success = True

    monkeypatch.setattr(settings, "ErrorReportTransport", lambda _url: transport)
    monkeypatch.setattr(
        settings,
        "ConsentPolicy",
        lambda _config: pytest.fail("health check must not capture consent"),
    )

    worker = settings.ErrorReportingHealthWorker(config, "https://relay.example")
    worker.run()

    assert config.set_calls == []
    transport.health.assert_called_once_with()


def test_health_check_retains_dedicated_worker_through_inherited_finished(monkeypatch):
    created = []

    class FakeHealthWorker:
        def __init__(self, _config, _relay_url):
            self.health_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            self.deleted = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            assert self.running is False
            self.deleted = True

    class Dialog:
        def __init__(self):
            self.config_mgr = FakeConfigManager()
            self._error_reporting_health_workers = []
            self.results = []

        def _on_error_reporting_health_finished(self, success):
            self.results.append(success)

    dialog = Dialog()
    policy_factory = MagicMock()
    monkeypatch.setattr(settings, "ERROR_REPORT_RELAY_URL", "https://relay.example")
    monkeypatch.setattr(settings, "ErrorReportingHealthWorker", FakeHealthWorker)
    monkeypatch.setattr(settings, "ConsentPolicy", policy_factory)

    SettingsDialog._start_error_reporting_health_check(dialog)
    worker = created[0]
    worker.health_finished.emit(True)

    assert dialog.results == [True]
    assert dialog._error_reporting_health_workers == [worker]
    assert worker in settings._ACTIVE_ERROR_REPORT_HEALTH_WORKERS
    policy_factory.assert_not_called()

    worker.running = False
    worker.finished.emit()

    assert dialog._error_reporting_health_workers == []
    assert worker not in settings._ACTIVE_ERROR_REPORT_HEALTH_WORKERS
    assert worker.deleted is True


@pytest.mark.parametrize("failure_point", ["health_connect", "finished_connect", "start"])
def test_health_check_setup_failures_are_cleaned_up_without_escaping(
    monkeypatch, failure_point
):
    created = []

    class FailingSignal:
        def connect(self, _slot, *_args):
            raise RuntimeError("signal setup failed")

    class FakeHealthWorker:
        def __init__(self, _config, _relay_url):
            self.health_finished = (
                FailingSignal() if failure_point == "health_connect" else FakeSignal()
            )
            self.finished = (
                FailingSignal() if failure_point == "finished_connect" else FakeSignal()
            )
            self.deleted = False
            created.append(self)

        def start(self):
            if failure_point == "start":
                raise RuntimeError("thread start failed")

        def isRunning(self):
            return False

        def deleteLater(self):
            self.deleted = True

    class Dialog:
        def __init__(self):
            self.config_mgr = FakeConfigManager()
            self._error_reporting_health_workers = []

        def _on_error_reporting_health_finished(self, _success):
            pass

    dialog = Dialog()
    monkeypatch.setattr(settings, "ERROR_REPORT_RELAY_URL", "https://relay.example")
    monkeypatch.setattr(settings, "ErrorReportingHealthWorker", FakeHealthWorker)

    SettingsDialog._start_error_reporting_health_check(dialog)

    worker = created[0]
    assert dialog._error_reporting_health_workers == []
    assert worker not in settings._ACTIVE_ERROR_REPORT_HEALTH_WORKERS
    assert worker.deleted is True


def test_health_button_is_disabled_while_check_runs_and_restored_after_finish(
    monkeypatch
):
    created = []

    class FakeHealthWorker:
        def __init__(self, _config, _relay_url):
            self.health_finished = FakeSignal()
            self.finished = FakeSignal()
            self.running = False
            created.append(self)

        def start(self):
            self.running = True

        def isRunning(self):
            return self.running

        def deleteLater(self):
            pass

    class Button:
        def __init__(self):
            self.enabled = True

        def isEnabled(self):
            return self.enabled

        def setEnabled(self, enabled):
            self.enabled = enabled

    class Dialog:
        def __init__(self):
            self.config_mgr = FakeConfigManager()
            self._error_reporting_health_workers = []
            self.btn_error_reporting_health = Button()

        def _on_error_reporting_health_finished(self, _success):
            pass

    dialog = Dialog()
    monkeypatch.setattr(settings, "ERROR_REPORT_RELAY_URL", "https://relay.example")
    monkeypatch.setattr(settings, "ErrorReportingHealthWorker", FakeHealthWorker)

    SettingsDialog._start_error_reporting_health_check(dialog)

    worker = created[0]
    assert dialog.btn_error_reporting_health.isEnabled() is False

    worker.running = False
    worker.finished.emit()

    assert dialog.btn_error_reporting_health.isEnabled() is True


def test_settings_dialog_constructs_full_general_tab_offscreen():
    app = QApplication.instance() or QApplication([])

    dialog = SettingsDialog(config_manager=FakeConfigManager())

    assert dialog.tabs.count() == 3
    assert dialog.chk_startup is not None
    dialog.close()
    assert app is QApplication.instance()


def test_error_reporting_settings_keep_consent_state_names_private_from_ui():
    source = inspect.getsource(SettingsDialog._build_error_reporting_group)

    assert ConsentState.DISABLED_BY_USER.value not in source
