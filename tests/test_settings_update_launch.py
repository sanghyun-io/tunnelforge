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
