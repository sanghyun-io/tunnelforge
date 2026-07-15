"""UI-thread approval dialogs for SSH server host keys."""

from PyQt6.QtCore import QThread
from PyQt6.QtWidgets import QApplication, QMessageBox

from src.core.i18n import translate_text
from src.core.ssh_host_trust import SshHostKeyCheck
from src.core.tunnel_engine import TunnelEngine


def _identity_details(check: SshHostKeyCheck, *, changed: bool = False) -> str:
    lines = [
        f"{translate_text('서버:')} {check.host}:{check.port}",
        f"{translate_text('키 알고리즘:')} {check.key_type}",
    ]
    if changed:
        lines.extend(
            [
                f"{translate_text('이전 SHA256 지문:')} {check.previous_fingerprint_sha256}",
                f"{translate_text('새 SHA256 지문:')} {check.fingerprint_sha256}",
            ]
        )
    else:
        lines.append(
            f"{translate_text('SHA256 지문:')} {check.fingerprint_sha256}"
        )
    return "\n".join(lines)


def build_unknown_ssh_host_dialog(parent, check: SshHostKeyCheck) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(translate_text("SSH 호스트 키 확인"))
    box.setText(translate_text("처음 연결하는 SSH 서버입니다."))
    box.setInformativeText(
        _identity_details(check)
        + "\n\n"
        + translate_text(
            "이 지문을 서버 관리자 또는 신뢰할 수 있는 채널로 확인한 후 계속하세요."
        )
    )
    box.addButton(
        translate_text("신뢰하고 계속"), QMessageBox.ButtonRole.AcceptRole
    )
    cancel = box.addButton(QMessageBox.StandardButton.Cancel)
    cancel.setText(translate_text("취소"))
    box.setDefaultButton(cancel)
    box.setEscapeButton(cancel)
    return box


def confirm_unknown_ssh_host(parent, check: SshHostKeyCheck) -> bool:
    box = build_unknown_ssh_host_dialog(parent, check)
    box.exec()
    clicked = box.clickedButton()
    return (
        clicked is not None
        and box.buttonRole(clicked) == QMessageBox.ButtonRole.AcceptRole
    )


def build_changed_ssh_host_dialog(parent, check: SshHostKeyCheck) -> QMessageBox:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(translate_text("SSH 호스트 키 변경 감지"))
    box.setText(
        translate_text("SSH 서버의 호스트 키가 이전에 승인한 키와 다릅니다.")
    )
    box.setInformativeText(
        _identity_details(check, changed=True)
        + "\n\n"
        + translate_text(
            "보안을 위해 연결을 차단했습니다. 서버 관리자를 통해 변경 사유를 확인하세요."
        )
    )
    box.setStandardButtons(QMessageBox.StandardButton.Close)
    close = box.button(QMessageBox.StandardButton.Close)
    close.setText(translate_text("닫기"))
    box.setDefaultButton(close)
    box.setEscapeButton(close)
    return box


def show_changed_ssh_host(parent, check: SshHostKeyCheck) -> None:
    build_changed_ssh_host_dialog(parent, check).exec()


def _show_trust_error(parent) -> None:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Critical)
    box.setWindowTitle(translate_text("SSH 호스트 키 확인 실패"))
    box.setText(
        translate_text(
            "SSH 서버의 호스트 키를 안전하게 확인할 수 없어 연결을 중단했습니다."
        )
    )
    box.setStandardButtons(QMessageBox.StandardButton.Close)
    close = box.button(QMessageBox.StandardButton.Close)
    close.setText(translate_text("닫기"))
    box.setDefaultButton(close)
    box.setEscapeButton(close)
    box.exec()


def _is_ui_thread() -> bool:
    app = QApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


def ensure_ssh_host_trusted(
    parent, engine: TunnelEngine, config: dict
) -> bool:
    """Approve an unknown SSH host on the UI thread or fail closed."""
    if config.get("connection_mode") == "direct":
        return True
    if not _is_ui_thread():
        return False

    try:
        check = engine.inspect_ssh_server(config)
    except Exception:
        _show_trust_error(parent)
        return False

    if check.status == "trusted":
        return True
    if check.status == "changed":
        show_changed_ssh_host(parent, check)
        return False
    if check.status != "approval_required":
        _show_trust_error(parent)
        return False
    if not confirm_unknown_ssh_host(parent, check):
        return False

    try:
        engine.approve_ssh_server(check)
    except Exception:
        _show_trust_error(parent)
        return False
    return True
