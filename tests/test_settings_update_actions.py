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
