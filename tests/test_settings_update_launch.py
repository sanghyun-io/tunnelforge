import hashlib
import os
from pathlib import Path
import threading
from unittest.mock import MagicMock

import pytest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
)

from src import update_integrity
from src.core import i18n
from src.core.update_downloader import UpdateDownloader
from src.ui.dialogs import settings
from src.ui.workers.update_worker import UpdateDownloadWorker


def _record_expected_integrity(dialog, package):
    contents = package.read_bytes()
    dialog._downloaded_installer_sha256 = hashlib.sha256(contents).hexdigest()
    dialog._downloaded_installer_size = len(contents)


def _capture_settings_leases(monkeypatch):
    lease_type = settings.VerifiedFileLease
    leases = []

    def create_lease(*args, **kwargs):
        lease = lease_type(*args, **kwargs)
        leases.append(lease)
        return lease

    monkeypatch.setattr(settings, "VerifiedFileLease", create_lease)
    return leases


def test_windows_update_launches_visible_installer_directly(monkeypatch, tmp_path):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_text("installer", encoding="utf-8")
    main_window = MagicMock()

    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    _record_expected_integrity(dialog, installer)

    popen = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setattr(settings.QMessageBox, "question", MagicMock(return_value=QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(settings.subprocess, "Popen", popen)

    settings.SettingsDialog._launch_installer(dialog)

    popen.assert_called_once()
    args, kwargs = popen.call_args
    assert args[0] == [str(installer)]
    assert "cmd.exe" not in args[0]
    assert "/SILENT" not in args[0]
    assert kwargs["close_fds"] is True
    main_window.close_app.assert_called_once()


def test_macos_update_keeps_verified_package_without_path_dispatch(monkeypatch, tmp_path):
    package = tmp_path / "TunnelForge-macOS-2.0.7-arm64.dmg"
    package.write_text("dmg", encoding="utf-8")
    main_window = MagicMock()
    owner = MagicMock()

    dialog = MagicMock()
    dialog._downloaded_installer_path = str(package)
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    dialog._downloaded_installer_owner = owner
    _record_expected_integrity(dialog, package)

    popen = MagicMock()
    open_url = MagicMock(return_value=True)
    information = MagicMock()
    app = MagicMock()
    leases = _capture_settings_leases(monkeypatch)
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    monkeypatch.setattr(settings.subprocess, "Popen", popen)
    monkeypatch.setattr(settings.QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(settings.QMessageBox, "information", information)
    monkeypatch.setattr(settings.QApplication, "instance", MagicMock(return_value=app))

    i18n.set_language("en")
    try:
        settings.SettingsDialog._launch_installer(dialog)
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)

    popen.assert_not_called()
    information.assert_called_once()
    assert information.call_args.args[1] == "Installer Launch Disabled"
    assert information.call_args.args[2].startswith(
        "For security, the downloaded installer is not launched automatically"
    )
    assert str(package) in information.call_args.args[2]
    open_url.assert_called_once()
    opened_path = Path(open_url.call_args.args[0].toLocalFile())
    assert opened_path == package.parent
    assert opened_path != package
    main_window.close_app.assert_not_called()
    app.quit.assert_not_called()
    owner.discard_downloaded_installer.assert_not_called()
    assert package.exists()
    assert len(leases) == 1 and leases[0].closed is True
    assert dialog._downloaded_installer_path == str(package)
    assert dialog._downloaded_installer_sha256 == hashlib.sha256(b"dmg").hexdigest()
    assert dialog._downloaded_installer_size == len(b"dmg")


def test_macos_replacement_callback_cannot_launch_anything(
    monkeypatch, tmp_path
):
    package = tmp_path / "TunnelForge-macOS-2.0.7-arm64.dmg"
    package.write_bytes(b"safe")
    main_window = MagicMock()
    owner = MagicMock()
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(package)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"safe").hexdigest()
    dialog._downloaded_installer_size = 4
    dialog._downloaded_installer_owner = owner
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    leases = _capture_settings_leases(monkeypatch)
    callback_calls = []
    popen = MagicMock()
    open_url = MagicMock(return_value=True)

    def dispatch(_dialog, _verified):
        callback_calls.append(package)
        replacement = tmp_path / "open-url-replacement.dmg"
        replacement.write_bytes(b"safe")
        os.replace(replacement, package)
        settings.subprocess.Popen([str(package)])
        return True

    information = MagicMock()
    app = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    monkeypatch.setattr(
        settings.SettingsDialog,
        "_dispatch_verified_installer",
        dispatch,
    )
    monkeypatch.setattr(settings.subprocess, "Popen", popen)
    monkeypatch.setattr(settings.QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(settings.QMessageBox, "information", information)
    monkeypatch.setattr(settings.QApplication, "instance", MagicMock(return_value=app))

    settings.SettingsDialog._launch_installer(dialog)

    assert callback_calls == []
    popen.assert_not_called()
    assert len(leases) == 1 and leases[0].closed is True
    information.assert_called_once()
    open_url.assert_called_once()
    assert Path(open_url.call_args.args[0].toLocalFile()) == package.parent
    main_window.close_app.assert_not_called()
    app.quit.assert_not_called()
    owner.discard_downloaded_installer.assert_not_called()
    assert package.read_bytes() == b"safe"


def test_launch_installer_rechecks_integrity_before_process_start(monkeypatch, tmp_path):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_bytes(b"trusted installer")
    main_window = MagicMock()

    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    owner = MagicMock()
    owner.discard_downloaded_installer.side_effect = (
        lambda path: Path(path).unlink()
    )
    dialog._downloaded_installer_owner = owner
    _record_expected_integrity(dialog, installer)
    installer.write_bytes(b"corrupt installer")

    popen = MagicMock()
    critical = MagicMock()
    app = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setattr(settings.subprocess, "Popen", popen)
    monkeypatch.setattr(settings.QMessageBox, "critical", critical)
    monkeypatch.setattr(settings.QMessageBox, "question", MagicMock())
    monkeypatch.setattr(settings.QApplication, "instance", MagicMock(return_value=app))

    settings.SettingsDialog._launch_installer(dialog)

    popen.assert_not_called()
    main_window.close_app.assert_not_called()
    app.quit.assert_not_called()
    settings.QMessageBox.question.assert_not_called()
    critical.assert_called_once()
    owner.discard_downloaded_installer.assert_called_once_with(str(installer))
    assert not installer.exists()


def test_settings_records_download_verification_metadata():
    dialog = MagicMock()

    settings.SettingsDialog._on_download_verification_ready(dialog, "a" * 64, 42)

    assert dialog._downloaded_installer_sha256 == "a" * 64
    assert dialog._downloaded_installer_size == 42


def _download_owned_package(monkeypatch, tmp_path, name, contents):
    download_dir = tmp_path / "owned-download"
    download_dir.mkdir()
    downloader = UpdateDownloader()
    downloader.download_url = f"https://example.com/{name}"
    downloader.installer_filename = name
    downloader.file_size = len(contents)
    downloader.expected_sha256 = hashlib.sha256(contents).hexdigest()
    response = MagicMock()
    response.headers = {"content-length": str(len(contents))}
    response.iter_content.return_value = [contents]
    monkeypatch.setattr(
        "src.core.update_downloader.tempfile.mkdtemp",
        lambda **_kwargs: str(download_dir),
    )
    monkeypatch.setattr(
        "src.core.update_downloader.requests.get",
        lambda *_args, **_kwargs: response,
    )
    return Path(downloader.download_installer()), downloader


def test_settings_unowned_integrity_failure_keeps_path_and_app_running(
    monkeypatch, tmp_path
):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_bytes(b"tampered")
    main_window = MagicMock()
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"expected").hexdigest()
    dialog._downloaded_installer_size = len(b"expected")
    dialog._downloaded_installer_owner = None
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    popen = MagicMock()
    app = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setattr(settings.subprocess, "Popen", popen)
    monkeypatch.setattr(settings.QMessageBox, "critical", MagicMock())
    monkeypatch.setattr(settings.QMessageBox, "question", MagicMock())
    monkeypatch.setattr(settings.QApplication, "instance", MagicMock(return_value=app))

    settings.SettingsDialog._launch_installer(dialog)

    assert installer.read_bytes() == b"tampered"
    popen.assert_not_called()
    main_window.close_app.assert_not_called()
    app.quit.assert_not_called()


def test_settings_integrity_cleanup_error_keeps_primary_failure(monkeypatch, tmp_path):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_bytes(b"tampered")
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"expected").hexdigest()
    dialog._downloaded_installer_size = len(b"expected")
    dialog._downloaded_installer_owner = MagicMock()
    dialog._downloaded_installer_owner.discard_downloaded_installer.side_effect = OSError(
        "cleanup failed"
    )
    dialog._latest_version = "2.0.7"
    monkeypatch.setattr(settings.QMessageBox, "critical", MagicMock())

    assert settings.SettingsDialog._verify_installer_before_launch(dialog) is False
    settings.QMessageBox.critical.assert_called_once()


def test_settings_popen_callback_holds_lease_and_rechecks_identity(
    monkeypatch, tmp_path
):
    installer, downloader = _download_owned_package(
        monkeypatch,
        tmp_path,
        "TunnelForge-Setup-2.0.7.exe",
        b"safe",
    )
    main_window = MagicMock()
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"safe").hexdigest()
    dialog._downloaded_installer_size = len(b"safe")
    dialog._downloaded_installer_owner = downloader
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    owner = downloader._find_owned_parent(str(installer))
    assert owner is not None
    owner.close()

    leases = _capture_settings_leases(monkeypatch)
    replacement_blocked = []
    popen_calls = []

    def popen(args, **kwargs):
        popen_calls.append((args, kwargs))
        assert leases[0].closed is False
        replacement = installer.parent / "replacement.exe"
        replacement.write_bytes(b"safe")
        try:
            os.replace(replacement, installer)
        except PermissionError:
            replacement_blocked.append(True)
        return MagicMock()

    critical = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setattr(
        settings.QMessageBox,
        "question",
        MagicMock(return_value=QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(settings.QMessageBox, "critical", critical)
    monkeypatch.setattr(settings.subprocess, "Popen", popen)

    settings.SettingsDialog._launch_installer(dialog)

    assert len(popen_calls) == 1
    assert len(leases) == 1 and leases[0].closed is True
    if os.name == "nt":
        assert replacement_blocked == [True]
        main_window.close_app.assert_called_once()
        critical.assert_not_called()
    else:
        assert replacement_blocked == []
        main_window.close_app.assert_not_called()
        critical.assert_called_once()

    post_context_replacement = installer.parent / "post-context.exe"
    post_context_replacement.write_bytes(b"after")
    os.replace(post_context_replacement, installer)
    assert installer.read_bytes() == b"after"


def test_settings_confirmation_cancel_closes_lease_without_launch_or_discard(
    monkeypatch, tmp_path
):
    installer, downloader = _download_owned_package(
        monkeypatch,
        tmp_path,
        "TunnelForge-Setup-2.0.7.exe",
        b"safe",
    )
    owner = downloader._find_owned_parent(str(installer))
    assert owner is not None
    owner.close()
    discard = MagicMock(wraps=downloader.discard_downloaded_installer)
    monkeypatch.setattr(downloader, "discard_downloaded_installer", discard)
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"safe").hexdigest()
    dialog._downloaded_installer_size = 4
    dialog._downloaded_installer_owner = downloader
    dialog._latest_version = "2.0.7"
    main_window = MagicMock()
    dialog.parent.return_value = main_window
    leases = _capture_settings_leases(monkeypatch)
    popen = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setattr(
        settings.QMessageBox,
        "question",
        MagicMock(return_value=QMessageBox.StandardButton.No),
    )
    monkeypatch.setattr(settings.subprocess, "Popen", popen)

    settings.SettingsDialog._launch_installer(dialog)

    assert len(leases) == 1 and leases[0].closed is True
    popen.assert_not_called()
    discard.assert_not_called()
    main_window.close_app.assert_not_called()
    assert installer.read_bytes() == b"safe"

    replacement = installer.parent / "after-cancel.exe"
    replacement.write_bytes(b"after")
    os.replace(replacement, installer)
    assert installer.read_bytes() == b"after"


def test_settings_popen_failure_preserves_verified_installer(
    monkeypatch, tmp_path
):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_bytes(b"safe")
    owner = MagicMock()
    main_window = MagicMock()
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"safe").hexdigest()
    dialog._downloaded_installer_size = 4
    dialog._downloaded_installer_owner = owner
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window
    leases = _capture_settings_leases(monkeypatch)
    critical = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "win32")
    monkeypatch.setattr(
        settings.QMessageBox,
        "question",
        MagicMock(return_value=QMessageBox.StandardButton.Yes),
    )
    monkeypatch.setattr(
        settings.subprocess,
        "Popen",
        MagicMock(side_effect=OSError("launch failed")),
    )
    monkeypatch.setattr(settings.QMessageBox, "critical", critical)

    settings.SettingsDialog._launch_installer(dialog)

    assert len(leases) == 1 and leases[0].closed is True
    assert installer.read_bytes() == b"safe"
    owner.discard_downloaded_installer.assert_not_called()
    main_window.close_app.assert_not_called()
    critical.assert_called_once()


def test_non_windows_settings_reveals_parent_without_launch_or_critical(
    monkeypatch, tmp_path
):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_bytes(b"safe")
    main_window = MagicMock()
    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._downloaded_installer_sha256 = hashlib.sha256(b"safe").hexdigest()
    dialog._downloaded_installer_size = 4
    dialog._downloaded_installer_owner = MagicMock()
    dialog.parent.return_value = main_window

    popen = MagicMock()
    open_url = MagicMock(return_value=True)
    information = MagicMock()
    critical = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "linux")
    monkeypatch.setattr(settings.subprocess, "Popen", popen)
    monkeypatch.setattr(settings.QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(settings.QMessageBox, "information", information)
    monkeypatch.setattr(settings.QMessageBox, "critical", critical)

    settings.SettingsDialog._launch_installer(dialog)

    popen.assert_not_called()
    open_url.assert_called_once()
    assert Path(open_url.call_args.args[0].toLocalFile()) == installer.parent
    information.assert_called_once()
    critical.assert_not_called()
    main_window.close_app.assert_not_called()
    assert installer.exists()


class _MinimalDownloadDialog(settings.SettingsDialog):
    def __init__(self):
        QDialog.__init__(self)
        self.config_mgr = MagicMock()
        self.btn_check_update = QPushButton()
        self.btn_download = QPushButton()
        self.btn_download.clicked.connect(lambda: None)
        self.btn_cancel_download = QPushButton()
        self.download_progress = QProgressBar()
        self.download_detail_label = QLabel()
        self._download_worker = None
        self._downloaded_installer_path = None
        self._downloaded_installer_sha256 = None
        self._downloaded_installer_size = 0
        self._downloaded_installer_owner = None
        self._latest_version = "2.0.7"
        self._download_generation = 0
        self._download_signal_relays = {}


def test_settings_ignores_queued_success_from_cancelled_generation(
    monkeypatch, tmp_path
):
    app = QApplication.instance() or QApplication([])
    installer = tmp_path / "late-installer.exe"
    installer.write_bytes(b"late")
    main_thread_id = threading.get_ident()
    worker_thread_ids = []

    class LateSuccessWorker(UpdateDownloadWorker):
        def __init__(self, config_manager=None):
            super().__init__(config_manager=config_manager)
            self.downloader = MagicMock()

        def run(self):
            worker_thread_ids.append(threading.get_ident())
            self.verification_ready.emit("a" * 64, 4)
            self.info_fetched.emit("2.0.7", 4)
            self.finished.emit(True, str(installer))

    import src.ui.workers as worker_package

    monkeypatch.setattr(worker_package, "UpdateDownloadWorker", LateSuccessWorker)
    dialog = _MinimalDownloadDialog()

    dialog._start_download()
    worker = dialog._download_worker
    assert worker.wait(5000)

    dialog._cancel_download()
    for _ in range(5):
        app.processEvents()

    assert worker_thread_ids and worker_thread_ids[0] != main_thread_id
    assert dialog._downloaded_installer_path is None
    assert dialog._downloaded_installer_sha256 is None
    assert dialog._downloaded_installer_size == 0
    worker.downloader.discard_downloaded_installer.assert_called_with(str(installer))
