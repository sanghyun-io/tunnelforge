from pathlib import Path
from unittest.mock import MagicMock

from PyQt6.QtWidgets import QMessageBox

from src.ui.dialogs import settings


def test_windows_update_launches_visible_installer_directly(monkeypatch, tmp_path):
    installer = tmp_path / "TunnelForge-Setup-2.0.7.exe"
    installer.write_text("installer", encoding="utf-8")
    main_window = MagicMock()

    dialog = MagicMock()
    dialog._downloaded_installer_path = str(installer)
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window

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


def test_macos_update_opens_package_and_closes_app(monkeypatch, tmp_path):
    package = tmp_path / "TunnelForge-macOS-2.0.7-arm64.dmg"
    package.write_text("dmg", encoding="utf-8")
    main_window = MagicMock()

    dialog = MagicMock()
    dialog._downloaded_installer_path = str(package)
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window

    popen = MagicMock()
    open_url = MagicMock(return_value=True)
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    monkeypatch.setattr(settings.QMessageBox, "question", MagicMock(return_value=QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(settings.subprocess, "Popen", popen)
    monkeypatch.setattr(settings.QDesktopServices, "openUrl", open_url)

    settings.SettingsDialog._launch_installer(dialog)

    popen.assert_not_called()
    open_url.assert_called_once()
    assert Path(open_url.call_args.args[0].toLocalFile()) == package
    main_window.close_app.assert_called_once()


def test_macos_update_open_failure_keeps_app_running(monkeypatch, tmp_path):
    package = tmp_path / "TunnelForge-macOS-2.0.7-arm64.dmg"
    package.write_text("dmg", encoding="utf-8")
    main_window = MagicMock()

    dialog = MagicMock()
    dialog._downloaded_installer_path = str(package)
    dialog._latest_version = "2.0.7"
    dialog.parent.return_value = main_window

    open_url = MagicMock(return_value=False)
    critical = MagicMock()
    monkeypatch.setattr(settings.sys, "platform", "darwin")
    monkeypatch.setattr(settings.QMessageBox, "question", MagicMock(return_value=QMessageBox.StandardButton.Yes))
    monkeypatch.setattr(settings.QDesktopServices, "openUrl", open_url)
    monkeypatch.setattr(settings.QMessageBox, "critical", critical)

    settings.SettingsDialog._launch_installer(dialog)

    open_url.assert_called_once()
    critical.assert_called_once()
    main_window.close_app.assert_not_called()
