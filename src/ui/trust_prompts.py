"""SSH 신원 확인 / 개인키 비밀번호 대화상자 (TF-STATUS-110).

TunnelEngine 은 워커 스레드에서 호출되는 경우가 많다. TrustPrompter 는 어느 스레드에서
불려도 대화상자를 GUI 스레드에서 띄우고 결과를 기다려 돌려준다.
"""
import threading
from typing import Callable, Optional

from PyQt6.QtCore import QObject, QThread, pyqtSignal
from PyQt6.QtWidgets import QLineEdit, QInputDialog, QMessageBox

from src.core.logger import get_logger
from src.core.ssh_trust import HostKeyPrompt

logger = get_logger(__name__)

PROMPT_TIMEOUT_SECONDS = 300


def confirm_host_key(parent, prompt: HostKeyPrompt) -> bool:
    """처음 보는 SSH 호스트 키: 지문을 보여주고 사용자가 명시적으로 신뢰해야 True."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("처음 접속하는 SSH 서버")
    box.setText(
        f"처음 접속하는 SSH 서버입니다.\n\n"
        f"서버: {prompt.host}:{prompt.port}\n"
        f"키 종류: {prompt.key_type}\n"
        f"SHA256 지문: {prompt.fingerprint}"
    )
    box.setInformativeText(
        "이 지문이 서버 관리자가 알려준 값과 같은지 확인하세요.\n"
        "확인 없이 신뢰하면 중간자 공격에 노출될 수 있습니다."
    )
    trust = box.addButton("지문을 확인했고 신뢰함", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is trust


def confirm_host_key_refresh(parent, host: str, port, stored_fingerprint: Optional[str],
                             new: HostKeyPrompt) -> bool:
    """명시적 '호스트 키 갱신' 확인: 저장된 지문과 서버의 현재 지문을 나란히 보여준다."""
    stored_text = stored_fingerprint or "(저장된 키 없음)"
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle("SSH 호스트 키 갱신")
    box.setText(
        f"서버: {host}:{port}\n\n"
        f"저장된 지문: {stored_text}\n"
        f"현재 서버 지문({new.key_type}): {new.fingerprint}"
    )
    box.setInformativeText(
        "서버가 정상적으로 교체·재설치된 것이 확실할 때만 갱신하세요.\n"
        "그렇지 않다면 중간자 공격일 수 있습니다."
    )
    update = box.addButton("호스트 키 갱신", QMessageBox.ButtonRole.AcceptRole)
    cancel = box.addButton("취소", QMessageBox.ButtonRole.RejectRole)
    box.setDefaultButton(cancel)
    box.exec()
    return box.clickedButton() is update


def ask_passphrase(parent, key_path: str, retry: bool) -> Optional[str]:
    """암호화된 개인키의 비밀번호를 묻는다. 취소하면 None. 결과는 저장하지 않는다."""
    label = f"개인키 비밀번호를 입력하세요.\n{key_path}"
    if retry:
        label = "비밀번호가 올바르지 않습니다. 다시 입력하세요.\n" + key_path
    text, ok = QInputDialog.getText(
        parent, "SSH 개인키 비밀번호", label, QLineEdit.EchoMode.Password
    )
    return text if ok else None


class TrustPrompter(QObject):
    """어느 스레드에서 호출돼도 GUI 스레드에서 대화상자를 실행하는 다리."""

    _requested = pyqtSignal(object)

    def __init__(self, parent_widget=None):
        super().__init__(parent_widget)
        self._parent_widget = parent_widget
        self._requested.connect(self._run)

    def install(self, engine) -> None:
        engine.host_key_confirmer = self.confirm_host_key
        engine.passphrase_provider = self.ask_passphrase

    def confirm_host_key(self, prompt: HostKeyPrompt) -> bool:
        return bool(self._on_gui_thread(lambda: confirm_host_key(self._parent_widget, prompt), False))

    def ask_passphrase(self, key_path: str, retry: bool) -> Optional[str]:
        return self._on_gui_thread(lambda: ask_passphrase(self._parent_widget, key_path, retry), None)

    def _on_gui_thread(self, fn: Callable, default):
        if QThread.currentThread() is self.thread():
            return fn()
        slot = {"fn": fn, "done": threading.Event(), "value": default}
        self._requested.emit(slot)
        slot["done"].wait(PROMPT_TIMEOUT_SECONDS)
        return slot["value"]

    def _run(self, slot) -> None:
        try:
            slot["value"] = slot["fn"]()
        except Exception:  # Qt 슬롯에서 예외를 밖으로 내보내면 안 된다
            logger.exception("trust prompt failed")
        finally:
            slot["done"].set()
