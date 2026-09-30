"""TF-STATUS-110 hardening: TLS controls of the cross-engine endpoint form (manual host entry)."""
import os
import sys
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt6.QtWidgets import QApplication

from src.core import connection_trust as ct
from src.core.cross_engine_migration import DatabaseEngine
from src.ui.dialogs.cross_engine_migration_endpoint_form import EndpointForm

app = QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _clean_registry():
    ct.clear_registered_tls()
    yield
    ct.clear_registered_tls()


TUNNEL = {"id": "t1", "name": "T", "connection_mode": "ssh_tunnel", "remote_host": "db.internal",
          "remote_port": 3306, "local_port": 13306, "db_engine": "mysql", "default_schema": "app",
          "db_tls_mode": "verify_full", "db_tls_ca_file": "ca.pem"}


class Config:
    def __init__(self, *tunnels):
        self.tunnels = list(tunnels)

    def load_config(self):
        return {"tunnels": self.tunnels}

    def get_tunnel_credentials(self, tid):
        return "u", "p"


def _form(*tunnels, require_tunnel=False):
    return EndpointForm("src", DatabaseEngine.MYSQL, None, Config(*tunnels), require_tunnel=require_tunnel)


def _type_host(form, host):
    form.input_host.setText(host)
    form.input_host.textEdited.emit(host)  # setText alone is not a user edit


def _mode(form):
    return form.combo_tls.currentData()


def test_manual_loopback_defaults_to_disable_without_warning():
    form = _form()
    assert _mode(form) == "disable" and form.lbl_tls_warning.isHidden()
    assert form.payload()["tls"] == {"mode": "disable"}


def test_manual_remote_host_defaults_to_verify_full_and_sends_it():
    form = _form()
    _type_host(form, "db.example.com")
    assert _mode(form) == "verify_full" and form.lbl_tls_warning.isHidden()
    form.input_tls_ca.setText("C:/ca.pem")
    assert form.payload()["tls"] == {"mode": "verify_full", "ca_file": "C:/ca.pem"}


def test_disable_for_remote_host_warns_and_choice_survives_host_edits():
    form = _form()
    _type_host(form, "db.example.com")
    form.combo_tls.setCurrentIndex(form.combo_tls.findData("disable"))
    form._on_tls_chosen()
    assert not form.lbl_tls_warning.isHidden() and form.lbl_tls_warning.text().startswith("⚠")
    _type_host(form, "other.example.com")
    assert _mode(form) == "disable"  # an explicit choice is not overridden by the host default
    _type_host(form, "localhost")
    assert form.lbl_tls_warning.isHidden()  # loopback exemption


def test_verify_ca_is_sent_with_ca_file():
    form = _form()
    _type_host(form, "db.example.com")
    form.combo_tls.setCurrentIndex(form.combo_tls.findData("verify_ca"))
    form.input_tls_ca.setText("ca.pem")
    assert form.payload()["tls"] == {"mode": "verify_ca", "ca_file": "ca.pem"}


def test_explicit_form_value_wins_over_registry():
    ct.register_endpoint_tls("127.0.0.1", 3306, ct.TlsPolicy("verify_full", "reg.pem", "reg.name"))
    form = _form()
    form.combo_tls.setCurrentIndex(form.combo_tls.findData("verify_ca"))
    payload = form.payload()
    assert payload["tls"] == {"mode": "verify_ca"}


def test_selected_tunnel_profile_supplies_tls_and_server_name():
    form = _form(TUNNEL)
    form.combo_tunnel.setCurrentIndex(1)
    assert _mode(form) == "verify_full" and form.input_tls_ca.text() == "ca.pem"
    assert form.payload()["tls"] == {"mode": "verify_full", "ca_file": "ca.pem", "server_name": "db.internal"}
    _type_host(form, "127.0.0.1")  # hand-editing the host detaches it from the tunnel's server name
    assert "server_name" not in form.payload()["tls"]


def test_legacy_tunnel_profile_stays_disabled_and_warns():
    legacy = {k: v for k, v in TUNNEL.items() if not k.startswith("db_tls")}
    form = _form(legacy)
    form.combo_tunnel.setCurrentIndex(1)
    assert _mode(form) == "disable"
    assert not form.lbl_tls_warning.isHidden()


def test_tunnel_only_form_locks_tls_inputs():
    form = _form(TUNNEL, require_tunnel=True)
    assert not form.combo_tls.isEnabled() and not form.btn_tls_ca.isEnabled()
    form.combo_tunnel.setCurrentIndex(1)
    assert form.payload()["tls"]["mode"] == "verify_full"
