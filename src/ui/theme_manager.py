"""
테마 매니저 모듈

싱글톤 패턴으로 앱 전체 테마 상태를 관리하고
Windows 시스템 테마 감지 기능 제공
"""

import sys
from PyQt6.QtCore import QObject, pyqtSignal

from src.ui.themes import ThemeType, ThemeColors, get_theme_colors, LIGHT_THEME


class ThemeManager(QObject):
    """테마 관리 싱글톤 클래스"""

    # 테마 변경 시그널 (ThemeColors 객체 전달)
    theme_changed = pyqtSignal(object)

    _instance = None

    def __init__(self):
        super().__init__()
        self._current_theme_type = ThemeType.SYSTEM
        self._current_colors = LIGHT_THEME
        self._config_manager = None

    @classmethod
    def instance(cls) -> 'ThemeManager':
        """싱글톤 인스턴스 반환 (QObject와 호환되는 방식)"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls):
        """테스트용: 싱글톤 인스턴스 초기화"""
        cls._instance = None

    def set_config_manager(self, config_manager):
        """ConfigManager 설정 (설정 저장/로드용)"""
        self._config_manager = config_manager

    def detect_system_theme(self) -> ThemeType:
        """Windows 시스템 다크 모드 감지

        Returns:
            ThemeType: LIGHT 또는 DARK
        """
        if sys.platform == 'win32':
            try:
                import winreg
                key = winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER,
                    r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
                )
                value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                winreg.CloseKey(key)
                return ThemeType.LIGHT if value else ThemeType.DARK
            except Exception:
                pass

        # 기본값: 라이트 테마
        return ThemeType.LIGHT

    @property
    def current_theme_type(self) -> ThemeType:
        """현재 설정된 테마 타입"""
        return self._current_theme_type

    @property
    def current_colors(self) -> ThemeColors:
        """현재 적용된 테마 색상"""
        return self._current_colors

    @property
    def is_dark(self) -> bool:
        """다크 테마 여부"""
        return self._current_colors == get_theme_colors(ThemeType.DARK)

    def set_theme(self, theme_type: ThemeType, save: bool = True):
        """테마 설정 및 적용

        Args:
            theme_type: 설정할 테마 타입
            save: True면 설정 파일에 저장
        """
        self._current_theme_type = theme_type

        # 시스템 테마인 경우 실제 시스템 설정 감지
        system_theme = self.detect_system_theme()
        self._current_colors = get_theme_colors(theme_type, system_theme)

        # 설정 저장
        if save and self._config_manager:
            self._config_manager.set_app_setting('theme', theme_type.value)

        # 변경 시그널 발생
        self.theme_changed.emit(self._current_colors)

    def load_saved_theme(self):
        """저장된 테마 설정 로드 및 적용"""
        if self._config_manager:
            saved_theme = self._config_manager.get_app_setting('theme', 'system')
            try:
                theme_type = ThemeType(saved_theme)
            except ValueError:
                theme_type = ThemeType.SYSTEM

            self.set_theme(theme_type, save=False)
        else:
            # ConfigManager 없으면 시스템 테마 사용
            self.set_theme(ThemeType.SYSTEM, save=False)

    def refresh_system_theme(self):
        """시스템 테마 재감지 (시스템 설정 변경 시 호출)"""
        if self._current_theme_type == ThemeType.SYSTEM:
            system_theme = self.detect_system_theme()
            self._current_colors = get_theme_colors(ThemeType.SYSTEM, system_theme)
            self.theme_changed.emit(self._current_colors)

    def get_theme_display_name(self, theme_type: ThemeType) -> str:
        """테마 타입의 표시 이름 반환"""
        names = {
            ThemeType.SYSTEM: "시스템 설정 따르기",
            ThemeType.LIGHT: "라이트 모드",
            ThemeType.DARK: "다크 모드"
        }
        return names.get(theme_type, "알 수 없음")
