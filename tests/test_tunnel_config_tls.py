"""TF-STATUS-110: TLS section of the tunnel config dialog."""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from src.core.ssh_trust import HostKeyPrompt
from src.ui.dialogs import tunnel_config
from src.ui.dialogs.tunnel_config import TunnelConfigDialog

app = QApplication.instance() or QApplication(sys.argv)


def _dialog(data=None, engine=None):
    return TunnelConfigDialog(None, tunnel_data=data, tunnel_engine=engine)


def _mode(dialog):
    return dialog.combo_tls_mode.currentData()


def test_new_ssh_profile_defaults_to_verify_full_without_warning():
    dialog = _dialog()
    assert _mode(dialog) == "verify_full"
    assert dialog.lbl_tls_warning.isHidden()


def test_new_direct_loopback_profile_defaults_to_disable_without_warning():
    dialog = _dialog({"connection_mode": "direct", "remote_host": "localhost"})
    assert _mode(dialog) == "disable"
    assert dialog.lbl_tls_warning.isHidden()


def test_new_profile_default_follows_connection_mode_until_user_chooses():
    dialog = _dialog()
    dialog.radio_direct.setChecked(True)
    dialog.input_remote_host.setText("127.0.0.1")
    assert _mode(dialog) == "disable" and dialog.lbl_tls_warning.isHidden()
    dialog.input_remote_host.setText("db.example.com")
    assert _mode(dialog) == "verify_full"

    dialog.combo_tls_mode.setCurrentIndex(dialog.combo_tls_mode.findData("verify_ca"))
    dialog._on_tls_mode_chosen()  # user picked it explicitly
    dialog.input_remote_host.setText("localhost")
    assert _mode(dialog) == "verify_ca"


def test_direct_non_loopback_disable_warns():
    dialog = _dialog({"connection_mode": "direct", "remote_host": "db.example.com", "id": "x", "db_tls_mode": "disable"})
    assert not dialog.lbl_tls_warning.isHidden()
    assert dialog.lbl_tls_warning.text().startswith("⚠")


def test_legacy_profile_without_tls_key_stays_disabled_and_warns_persistently():
    legacy = {"id": "old", "name": "old", "connection_mode": "ssh_tunnel", "remote_host": "db.internal",
              "bastion_host": "b", "bastion_port": 22, "bastion_user": "u", "bastion_key": "k",
              "remote_port": 3306, "db_engine": "mysql"}
    dialog = _dialog(legacy)
    assert _mode(dialog) == "disable"
    assert not dialog.lbl_tls_warning.isHidden()
    assert "저장된 TLS 설정이 없어" in dialog.lbl_tls_warning.text()
    assert dialog.get_data()["db_tls_mode"] == "disable"  # saved as an explicit choice, never upgraded silently

    dialog.combo_tls_mode.setCurrentIndex(dialog.combo_tls_mode.findData("verify_full"))
    assert dialog.lbl_tls_warning.isHidden()
    assert dialog.get_data()["db_tls_mode"] == "verify_full"


def test_tunnel_disable_never_exempt_even_for_loopback_target():
    dialog = _dialog({"id": "t", "connection_mode": "ssh_tunnel", "remote_host": "127.0.0.1", "db_tls_mode": "disable"})
    assert not dialog.lbl_tls_warning.isHidden()


def test_get_data_includes_tls_fields_and_ca_follows_mode():
    dialog = _dialog({"id": "t", "connection_mode": "ssh_tunnel", "remote_host": "db.internal",
                      "db_tls_mode": "verify_full", "db_tls_ca_file": "C:/ca.pem"})
    data = dialog.get_data()
    assert (data["db_tls_mode"], data["db_tls_ca_file"]) == ("verify_full", "C:/ca.pem")
    assert dialog.tls_ca_widget.isEnabled()
    dialog.combo_tls_mode.setCurrentIndex(dialog.combo_tls_mode.findData("disable"))
    assert not dialog.tls_ca_widget.isEnabled()


def test_host_key_button_only_in_ssh_mode():
    dialog = _dialog()
    assert dialog.btn_host_key.isEnabled()
    dialog.radio_direct.setChecked(True)
    assert not dialog.btn_host_key.isEnabled()


class _Store:
    def __init__(self, entry):
        self.entry = entry

    def get_known_host(self, host, port):
        return self.entry


class _Engine:
    def __init__(self, stored_fp, current_fp):
        self.known_hosts = _Store({"fingerprint": stored_fp} if stored_fp else None)
        self._current = HostKeyPrompt("b", 22, "ssh-ed25519", current_fp)
        self.refreshed = []

    def probe_bastion_fingerprint(self, host, port):
        return self._current

    def refresh_host_key(self, host, port):
        self.refreshed.append((host, port))


def _patch_dialogs(monkeypatch, confirm):
    calls = SimpleNamespace(confirm=[], info=[])
    monkeypatch.setattr(tunnel_config.trust_prompts, "confirm_host_key_refresh",
                        lambda *args: calls.confirm.append(args) or confirm)
    monkeypatch.setattr(tunnel_config.QMessageBox, "information", lambda *args: calls.info.append(args[2]))
    monkeypatch.setattr(tunnel_config.QMessageBox, "critical", lambda *args: calls.info.append(args[2]))
    return calls


def test_host_key_refresh_needs_explicit_confirmation(monkeypatch):
    engine = _Engine("SHA256:old", "SHA256:new")
    dialog = _dialog({"bastion_host": "b"}, engine)
    calls = _patch_dialogs(monkeypatch, confirm=False)
    dialog._manage_host_key()
    assert calls.confirm and engine.refreshed == []

    calls = _patch_dialogs(monkeypatch, confirm=True)
    dialog._manage_host_key()
    assert engine.refreshed == [("b", 22)]


def test_host_key_refresh_is_noop_when_keys_match(monkeypatch):
    engine = _Engine("SHA256:same", "SHA256:same")
    dialog = _dialog({"bastion_host": "b"}, engine)
    calls = _patch_dialogs(monkeypatch, confirm=True)
    dialog._manage_host_key()
    assert not calls.confirm and engine.refreshed == []
    assert any("같습니다" in text for text in calls.info)
