import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.ssh_host_trust import SshHostKeyCheck
from src.ui.dialogs import ssh_host_key_dialog
from src.ui.dialogs.ssh_host_key_dialog import (
    build_changed_ssh_host_dialog,
    build_unknown_ssh_host_dialog,
    confirm_unknown_ssh_host,
    ensure_ssh_host_trusted,
    show_changed_ssh_host,
)


app = QApplication.instance() or QApplication(sys.argv)


@pytest.fixture
def unknown_check():
    return SshHostKeyCheck(
        status="approval_required",
        host="bastion.example",
        port=2222,
        key_type="ssh-ed25519",
        fingerprint_sha256="SHA256:AbCdEf0123456789AbCdEf0123456789AbCdEf01234",
        approval_token="approval-token-secret",
    )


@pytest.fixture
def changed_check():
    return SshHostKeyCheck(
        status="changed",
        host="bastion.example",
        port=2222,
        key_type="ssh-ed25519",
        previous_fingerprint_sha256="SHA256:OldOldOldOldOldOldOldOldOldOldOldOldOldOldOld",
        fingerprint_sha256="SHA256:NewNewNewNewNewNewNewNewNewNewNewNewNewNewNew",
    )


def test_unknown_dialog_shows_identity_and_defaults_cancel(unknown_check):
    box = build_unknown_ssh_host_dialog(None, unknown_check)
    try:
        details = box.informativeText()
        assert f"{unknown_check.host}:{unknown_check.port}" in details
        assert unknown_check.key_type in details
        assert unknown_check.fingerprint_sha256 in details
        cancel = box.button(QMessageBox.StandardButton.Cancel)
        assert cancel is not None
        assert box.defaultButton() is cancel
        assert box.escapeButton() is cancel
        assert {button.text() for button in box.buttons()} == {"신뢰하고 계속", "취소"}
    finally:
        box.close()


def test_changed_dialog_shows_old_and_new_identity_with_no_trust_button(
    changed_check,
):
    box = build_changed_ssh_host_dialog(None, changed_check)
    try:
        details = box.informativeText()
        assert f"{changed_check.host}:{changed_check.port}" in details
        assert changed_check.key_type in details
        assert changed_check.previous_fingerprint_sha256 in details
        assert changed_check.fingerprint_sha256 in details
        assert box.icon() == QMessageBox.Icon.Critical
        assert box.standardButtons() == QMessageBox.StandardButton.Close
        assert all(button.text() != "신뢰하고 계속" for button in box.buttons())
    finally:
        box.close()


@pytest.mark.parametrize(
    ("role", "expected"),
    [
        (QMessageBox.ButtonRole.AcceptRole, True),
        (QMessageBox.ButtonRole.RejectRole, False),
    ],
)
def test_confirm_unknown_host_accepts_only_the_explicit_trust_role(
    monkeypatch, unknown_check, role, expected
):
    clicked = object()
    box = MagicMock()
    box.clickedButton.return_value = clicked
    box.buttonRole.return_value = role
    monkeypatch.setattr(
        ssh_host_key_dialog, "build_unknown_ssh_host_dialog", lambda *args: box
    )

    assert confirm_unknown_ssh_host(None, unknown_check) is expected
    box.exec.assert_called_once_with()
    box.buttonRole.assert_called_once_with(clicked)


def test_show_changed_host_executes_the_blocking_changed_key_dialog(
    monkeypatch, changed_check
):
    box = MagicMock()
    monkeypatch.setattr(
        ssh_host_key_dialog, "build_changed_ssh_host_dialog", lambda *args: box
    )

    show_changed_ssh_host(None, changed_check)

    box.exec.assert_called_once_with()


def test_ensure_trusted_direct_mode_bypasses_ssh_inspection():
    engine = MagicMock()

    assert ensure_ssh_host_trusted(
        None, engine, {"connection_mode": "direct"}
    ) is True
    engine.inspect_ssh_server.assert_not_called()


def test_ensure_trusted_returns_immediately_for_saved_identity():
    engine = MagicMock()
    engine.inspect_ssh_server.return_value = SshHostKeyCheck(
        status="trusted",
        host="bastion.example",
        port=22,
        key_type="ssh-ed25519",
        fingerprint_sha256="SHA256:TrustedTrustedTrustedTrustedTrustedTrustedTru",
    )

    assert ensure_ssh_host_trusted(None, engine, {}) is True
    engine.approve_ssh_server.assert_not_called()


def test_ensure_trusted_persists_explicit_unknown_host_approval(
    monkeypatch, unknown_check
):
    engine = MagicMock()
    engine.inspect_ssh_server.return_value = unknown_check
    confirm = MagicMock(return_value=True)
    monkeypatch.setattr(ssh_host_key_dialog, "confirm_unknown_ssh_host", confirm)

    assert ensure_ssh_host_trusted(None, engine, {}) is True
    confirm.assert_called_once_with(None, unknown_check)
    engine.approve_ssh_server.assert_called_once_with(unknown_check)


def test_ensure_trusted_decline_never_persists_unknown_host(
    monkeypatch, unknown_check
):
    engine = MagicMock()
    engine.inspect_ssh_server.return_value = unknown_check
    monkeypatch.setattr(
        ssh_host_key_dialog, "confirm_unknown_ssh_host", MagicMock(return_value=False)
    )

    assert ensure_ssh_host_trusted(None, engine, {}) is False
    engine.approve_ssh_server.assert_not_called()


def test_ensure_trusted_changed_host_is_blocked_without_approval(
    monkeypatch, changed_check
):
    engine = MagicMock()
    engine.inspect_ssh_server.return_value = changed_check
    show_changed = MagicMock()
    monkeypatch.setattr(ssh_host_key_dialog, "show_changed_ssh_host", show_changed)

    assert ensure_ssh_host_trusted(None, engine, {}) is False
    show_changed.assert_called_once_with(None, changed_check)
    engine.approve_ssh_server.assert_not_called()


@pytest.mark.parametrize("failure_stage", ["inspect", "approve"])
def test_ensure_trusted_probe_or_reprobe_error_is_generic_and_leak_free(
    monkeypatch, unknown_check, failure_stage
):
    engine = MagicMock()
    if failure_stage == "inspect":
        engine.inspect_ssh_server.side_effect = RuntimeError(
            "credential-secret approval-token-secret"
        )
    else:
        engine.inspect_ssh_server.return_value = unknown_check
        engine.approve_ssh_server.side_effect = RuntimeError(
            "credential-secret approval-token-secret"
        )
        monkeypatch.setattr(
            ssh_host_key_dialog,
            "confirm_unknown_ssh_host",
            MagicMock(return_value=True),
        )
    show_error = MagicMock()
    monkeypatch.setattr(ssh_host_key_dialog, "_show_trust_error", show_error)

    assert ensure_ssh_host_trusted(None, engine, {}) is False
    show_error.assert_called_once_with(None)
    rendered_call = repr(show_error.call_args)
    assert "credential-secret" not in rendered_call
    assert "approval-token-secret" not in rendered_call


def test_ensure_trusted_refuses_background_thread_without_showing_ui(monkeypatch):
    engine = MagicMock()
    show_error = MagicMock()
    monkeypatch.setattr(ssh_host_key_dialog, "_is_ui_thread", lambda: False)
    monkeypatch.setattr(ssh_host_key_dialog, "_show_trust_error", show_error)

    assert ensure_ssh_host_trusted(None, engine, {}) is False
    engine.inspect_ssh_server.assert_not_called()
    show_error.assert_not_called()
