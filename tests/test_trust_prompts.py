import os
import sys
import threading
import time
from types import SimpleNamespace

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QWidget

from src.core.ssh_trust import HostKeyPrompt
from src.ui import trust_prompts

app = QApplication.instance() or QApplication(sys.argv)


def _run_in_thread_while_pumping(fn):
    result = {}
    worker = threading.Thread(target=lambda: result.setdefault("value", fn()))
    worker.start()
    deadline = time.time() + 10
    while worker.is_alive() and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    worker.join(1)
    assert not worker.is_alive(), "prompt never returned"
    return result["value"]


def test_install_wires_engine_hooks():
    parent = QWidget()
    prompter = trust_prompts.TrustPrompter(parent)
    engine = SimpleNamespace()
    prompter.install(engine)
    assert engine.host_key_confirmer == prompter.confirm_host_key
    assert engine.passphrase_provider == prompter.ask_passphrase


def test_worker_thread_prompt_runs_on_gui_thread(monkeypatch):
    parent = QWidget()
    prompter = trust_prompts.TrustPrompter(parent)
    seen = {}

    def fake_dialog(_parent, prompt):
        seen["thread"] = threading.current_thread()
        seen["prompt"] = prompt
        return True

    monkeypatch.setattr(trust_prompts, "confirm_host_key", fake_dialog)
    prompt = HostKeyPrompt("h", 22, "ssh-ed25519", "SHA256:abc")
    assert _run_in_thread_while_pumping(lambda: prompter.confirm_host_key(prompt)) is True
    assert seen["thread"] is threading.main_thread()
    assert seen["prompt"] == prompt


def test_declined_or_failing_dialog_never_trusts(monkeypatch):
    parent = QWidget()
    prompter = trust_prompts.TrustPrompter(parent)
    prompt = HostKeyPrompt("h", 22, "ssh-ed25519", "SHA256:abc")

    monkeypatch.setattr(trust_prompts, "confirm_host_key", lambda p, pr: False)
    assert prompter.confirm_host_key(prompt) is False  # direct call on the GUI thread

    def boom(_parent, _prompt):
        raise RuntimeError("dialog failed")

    monkeypatch.setattr(trust_prompts, "confirm_host_key", boom)
    assert _run_in_thread_while_pumping(lambda: prompter.confirm_host_key(prompt)) is False


def test_passphrase_prompt_returns_none_when_cancelled(monkeypatch):
    parent = QWidget()
    prompter = trust_prompts.TrustPrompter(parent)
    monkeypatch.setattr(trust_prompts, "ask_passphrase", lambda p, path, retry: None)
    assert _run_in_thread_while_pumping(lambda: prompter.ask_passphrase("/k", False)) is None
    monkeypatch.setattr(trust_prompts, "ask_passphrase", lambda p, path, retry: "secret")
    assert _run_in_thread_while_pumping(lambda: prompter.ask_passphrase("/k", True)) == "secret"
