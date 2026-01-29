"""
테마 정의 모듈

라이트/다크 테마의 색상 정의 및 테마 타입 관리
"""

from dataclasses import dataclass
from enum import Enum


class ThemeType(Enum):
    """테마 타입 열거형"""
    LIGHT = "light"
    DARK = "dark"
    SYSTEM = "system"


@dataclass
class ThemeColors:
    """테마 색상 정의"""
    # 배경 색상
    background: str
    background_secondary: str
    background_tertiary: str

    # 전경 (텍스트) 색상
    foreground: str
    foreground_secondary: str
    foreground_disabled: str

    # 강조 색상
    primary: str
    primary_hover: str
    primary_light: str

    # 상태 색상
    success: str
    success_light: str
    success_hover: str
    danger: str
    danger_light: str
    danger_hover: str
    warning: str
    warning_light: str
    warning_hover: str

    # 입력 필드
    input_background: str
    input_border: str
    input_border_focus: str
    input_error_background: str
    input_success_background: str

    # 테이블
    table_header: str
    table_row_alt: str
    table_selection: str
    table_border: str
    table_gridline: str

    # 기타 UI 요소
    border: str
    border_light: str
    scrollbar: str
    scrollbar_hover: str
    shadow: str


# 라이트 테마 정의
LIGHT_THEME = ThemeColors(
    # 배경
    background="#ffffff",
    background_secondary="#f8f9f9",
    background_tertiary="#ecf0f1",

    # 전경
    foreground="#2c3e50",
    foreground_secondary="#7f8c8d",
    foreground_disabled="#95a5a6",

    # 강조
    primary="#3498db",
    primary_hover="#2980b9",
    primary_light="#ebf5fb",

    # 상태
    success="#27ae60",
    success_light="#d5f5e3",
    success_hover="#219a52",
    danger="#e74c3c",
    danger_light="#fadbd8",
    danger_hover="#c0392b",
    warning="#f1c40f",
    warning_light="#fcf3cf",
    warning_hover="#d4ac0d",

    # 입력
    input_background="#ffffff",
    input_border="#bdc3c7",
    input_border_focus="#3498db",
    input_error_background="#fdf2f0",
    input_success_background="#f0fdf4",

    # 테이블
    table_header="#ecf0f1",
    table_row_alt="#f8f9f9",
    table_selection="#d5f5e3",
    table_border="#bdc3c7",
    table_gridline="#ecf0f1",

    # 기타
    border="#bdc3c7",
    border_light="#ecf0f1",
    scrollbar="#bdc3c7",
    scrollbar_hover="#95a5a6",
    shadow="rgba(0, 0, 0, 0.1)"
)


# 다크 테마 정의
DARK_THEME = ThemeColors(
    # 배경
    background="#1e1e1e",
    background_secondary="#252526",
    background_tertiary="#2d2d30",

    # 전경
    foreground="#d4d4d4",
    foreground_secondary="#9d9d9d",
    foreground_disabled="#6d6d6d",

    # 강조
    primary="#569cd6",
    primary_hover="#4a8ac4",
    primary_light="#264f78",

    # 상태
    success="#4ec9b0",
    success_light="#1e4a40",
    success_hover="#3db89b",
    danger="#f14c4c",
    danger_light="#4a1e1e",
    danger_hover="#d43c3c",
    warning="#dcdcaa",
    warning_light="#4a4a1e",
    warning_hover="#c9c98a",

    # 입력
    input_background="#3c3c3c",
    input_border="#555555",
    input_border_focus="#569cd6",
    input_error_background="#4a1e1e",
    input_success_background="#1e4a40",

    # 테이블
    table_header="#2d2d30",
    table_row_alt="#252526",
    table_selection="#264f78",
    table_border="#555555",
    table_gridline="#3c3c3c",

    # 기타
    border="#555555",
    border_light="#3c3c3c",
    scrollbar="#555555",
    scrollbar_hover="#6d6d6d",
    shadow="rgba(0, 0, 0, 0.3)"
)


def get_theme_colors(theme_type: ThemeType, system_theme: ThemeType = None) -> ThemeColors:
    """테마 타입에 따른 색상 반환

    Args:
        theme_type: 선택된 테마 타입
        system_theme: 시스템 테마 (ThemeType.SYSTEM 선택 시 사용)

    Returns:
        ThemeColors: 해당 테마의 색상 정의
    """
    if theme_type == ThemeType.SYSTEM:
        if system_theme == ThemeType.DARK:
            return DARK_THEME
        return LIGHT_THEME
    elif theme_type == ThemeType.DARK:
        return DARK_THEME
    else:
        return LIGHT_THEME
