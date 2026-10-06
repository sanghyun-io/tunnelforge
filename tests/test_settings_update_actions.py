import inspect
from unittest.mock import MagicMock

from src.ui.dialogs import settings
from src.ui.dialogs.settings import SettingsDialog, update_package_action_text
from src.ui.themes import ThemeType


def test_update_package_action_text_uses_reveal_wording_for_macos_packages():
    text = update_package_action_text("open")

    assert text.button == "📂 저장 위치 보기"
    assert "저장 위치 보기" in text.done_message
    assert "설치 시작" not in text.button
    assert "설치 시작" not in text.done_message
    assert "현재 앱은 종료되지 않습니다." in text.confirm_body


def test_update_package_action_text_keeps_installer_wording_for_windows():
    text = update_package_action_text("execute")

    assert text.button == "🚀 설치 시작"
    assert "설치 시작" in text.done_message
    assert "설치를 위해 현재 앱이 종료됩니다." in text.confirm_body


def test_settings_dialog_has_no_connection_pool_tab():
    """WP-4.2: 연결 풀 탭 및 관련 헬퍼는 완전히 제거되어야 한다."""
    source = inspect.getsource(SettingsDialog.init_ui)

    assert "_create_pool_tab" not in source
    assert "connection_pool" not in source
    assert not hasattr(SettingsDialog, "_create_pool_tab")
    assert not hasattr(SettingsDialog, "_refresh_pool_status")
    assert not hasattr(SettingsDialog, "_close_all_pools")


def test_on_theme_changed_previews_without_saving(monkeypatch):
    """테마 콤보 변경은 미리보기만 하고 저장하지 않아야 한다 (save=False)."""
    dialog = MagicMock()
    dialog.theme_combo.currentData.return_value = ThemeType.DARK.value

    theme_mgr = MagicMock()
    monkeypatch.setattr(settings.ThemeManager, "instance", staticmethod(lambda: theme_mgr))

    SettingsDialog._on_theme_changed(dialog, 2)

    theme_mgr.set_theme.assert_called_once_with(ThemeType.DARK, save=False)


def test_save_settings_persists_selected_theme(monkeypatch):
    """저장 버튼을 누르면 선택된 테마가 save=True로 확정 저장되어야 한다."""
    dialog = MagicMock()
    dialog.radio_minimize.isChecked.return_value = False
    dialog.radio_exit.isChecked.return_value = False
    dialog.theme_combo.currentData.return_value = ThemeType.DARK.value
    dialog.language_combo.currentData.return_value = "ko"
    dialog.chk_auto_reconnect.isChecked.return_value = True
    dialog.spin_max_reconnect.value.return_value = 5
    dialog._theme_saved = False

    from src.core.platform_integration import StartupRegistrar

    theme_mgr = MagicMock()
    monkeypatch.setattr(settings.ThemeManager, "instance", staticmethod(lambda: theme_mgr))
    monkeypatch.setattr(settings, "set_language", MagicMock())
    monkeypatch.setattr(StartupRegistrar, "is_supported", property(lambda self: False))

    SettingsDialog.save_settings(dialog)

    theme_mgr.set_theme.assert_called_once_with(ThemeType.DARK, save=True)
    assert dialog._theme_saved is True
    dialog.accept.assert_called_once()


def test_save_settings_does_not_apply_error_reporting_consent(monkeypatch):
    dialog = MagicMock()
    dialog.radio_minimize.isChecked.return_value = False
    dialog.radio_exit.isChecked.return_value = False
    dialog.theme_combo.currentData.return_value = ThemeType.DARK.value
    dialog.language_combo.currentData.return_value = "ko"
    dialog.chk_auto_reconnect.isChecked.return_value = True
    dialog.spin_max_reconnect.value.return_value = 5
    dialog._theme_saved = False
    policy_factory = MagicMock()

    from src.core.platform_integration import StartupRegistrar

    monkeypatch.setattr(settings, "ConsentPolicy", policy_factory)
    monkeypatch.setattr(settings.ThemeManager, "instance", staticmethod(MagicMock))
    monkeypatch.setattr(settings, "set_language", MagicMock())
    monkeypatch.setattr(StartupRegistrar, "is_supported", property(lambda self: False))

    SettingsDialog.save_settings(dialog)

    policy_factory.assert_not_called()


def test_restore_original_theme_if_unsaved_reverts_preview(monkeypatch):
    """미저장 상태에서 취소하면 원래 테마로 save=False 복원해야 한다."""
    dialog = MagicMock()
    dialog._theme_saved = False
    dialog._original_theme_type = ThemeType.LIGHT

    theme_mgr = MagicMock()
    monkeypatch.setattr(settings.ThemeManager, "instance", staticmethod(lambda: theme_mgr))

    SettingsDialog._restore_original_theme_if_unsaved(dialog)

    theme_mgr.set_theme.assert_called_once_with(ThemeType.LIGHT, save=False)


def test_restore_original_theme_if_unsaved_noop_when_saved(monkeypatch):
    """이미 저장된 상태라면 복원 로직이 테마를 되돌리지 않아야 한다."""
    dialog = MagicMock()
    dialog._theme_saved = True
    dialog._original_theme_type = ThemeType.LIGHT

    theme_mgr = MagicMock()
    monkeypatch.setattr(settings.ThemeManager, "instance", staticmethod(lambda: theme_mgr))

    SettingsDialog._restore_original_theme_if_unsaved(dialog)

    theme_mgr.set_theme.assert_not_called()


def test_restore_backup_success_closes_dialog_so_save_cannot_overwrite(monkeypatch):
    """백업 복원 성공 후에는 다이얼로그를 닫아, 이후 '저장'이 복원된 설정을 덮어쓰지 못하게 한다."""
    dialog = MagicMock()
    dialog.backup_list.currentItem.return_value.data.return_value = "backup.json"
    dialog.config_mgr.restore_backup.return_value = (True, "복원됨")
    monkeypatch.setattr(
        settings.QMessageBox, "question", staticmethod(lambda *a, **k: settings.QMessageBox.StandardButton.Yes)
    )
    infos = []
    monkeypatch.setattr(settings.QMessageBox, "information", staticmethod(lambda *a, **k: infos.append(a)))

    SettingsDialog._restore_selected_backup(dialog)

    dialog._close_after_config_replaced.assert_called_once_with()
    dialog.save_settings.assert_not_called()
    assert "재시작" in infos[0][2]


def test_import_config_success_closes_dialog_so_save_cannot_overwrite(monkeypatch):
    dialog = MagicMock()
    dialog.config_mgr.import_config.return_value = (True, "가져옴")
    monkeypatch.setattr(
        settings.QFileDialog, "getOpenFileName", staticmethod(lambda *a, **k: ("C:/x/config.json", ""))
    )
    monkeypatch.setattr(
        settings.QMessageBox, "question", staticmethod(lambda *a, **k: settings.QMessageBox.StandardButton.Yes)
    )
    monkeypatch.setattr(settings.QMessageBox, "information", staticmethod(lambda *a, **k: None))

    SettingsDialog._import_config(dialog)

    dialog._close_after_config_replaced.assert_called_once_with()


def test_close_after_config_replaced_reverts_theme_preview_and_accepts():
    dialog = MagicMock()

    SettingsDialog._close_after_config_replaced(dialog)

    dialog._restore_original_theme_if_unsaved.assert_called_once_with()
    dialog.accept.assert_called_once_with()
