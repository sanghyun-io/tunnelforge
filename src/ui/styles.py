"""
UI 스타일시트 중앙화 모듈

모든 UI 파일에서 사용하는 CSS 스타일을 한 곳에서 관리하여
- CSS 파싱 중복 제거
- 유지보수성 향상
- 일관된 디자인 시스템 적용
- 다크 모드 지원
"""

from src.ui.themes import ThemeColors, LIGHT_THEME


def get_current_colors() -> ThemeColors:
    """현재 테마 색상 반환 (ThemeManager 순환 참조 방지)"""
    try:
        from src.ui.theme_manager import ThemeManager
        return ThemeManager.instance().current_colors
    except Exception:
        return LIGHT_THEME


class ButtonStyles:
    """버튼 스타일 정의"""

    # 기본 스타일 (Primary)
    PRIMARY = """
        QPushButton {
            background-color: #3498db; color: white; font-weight: bold;
            padding: 6px 16px; border-radius: 4px; border: none;
        }
        QPushButton:hover { background-color: #2980b9; }
        QPushButton:disabled { background-color: #bdc3c7; color: #7f8c8d; }
    """

    # 보조 스타일 (Secondary)
    SECONDARY = """
        QPushButton {
            background-color: #ecf0f1; color: #2c3e50;
            padding: 6px 16px; border-radius: 4px; border: 1px solid #bdc3c7;
        }
        QPushButton:hover { background-color: #d5dbdb; }
        QPushButton:disabled { background-color: #f8f9f9; color: #bdc3c7; }
    """

    # 위험 스타일 (Danger/Stop)
    DANGER = """
        QPushButton {
            background-color: #e74c3c; color: white; font-weight: bold;
            padding: 4px 12px; border-radius: 4px; border: none;
        }
        QPushButton:hover { background-color: #c0392b; }
        QPushButton:disabled { background-color: #f5b7b1; color: #f8f9f9; }
    """

    # 성공 스타일 (Success/Start)
    SUCCESS = """
        QPushButton {
            background-color: #2ecc71; color: white; font-weight: bold;
            padding: 4px 12px; border-radius: 4px; border: none;
        }
        QPushButton:hover { background-color: #27ae60; }
        QPushButton:disabled { background-color: #a9dfbf; color: #f8f9f9; }
    """

    # 경고 스타일 (Warning)
    WARNING = """
        QPushButton {
            background-color: #f1c40f; color: #333; font-weight: bold;
            padding: 6px 16px; border-radius: 4px; border: none;
        }
        QPushButton:hover { background-color: #d4ac0d; }
        QPushButton:disabled { background-color: #f9e79f; color: #7f8c8d; }
    """

    # 삭제 버튼 (경계선 있는 위험)
    DELETE = """
        QPushButton {
            background-color: #fadbd8; color: #c0392b;
            padding: 4px 10px; border-radius: 4px; border: 1px solid #e74c3c;
        }
        QPushButton:hover { background-color: #f5b7b1; }
        QPushButton:disabled { background-color: #fdf2f0; color: #d98880; }
    """

    # 수정 버튼 (보조 작은 크기)
    EDIT = """
        QPushButton {
            background-color: #ecf0f1; color: #2c3e50;
            padding: 4px 10px; border-radius: 4px; border: 1px solid #bdc3c7;
        }
        QPushButton:hover { background-color: #d5dbdb; }
        QPushButton:disabled { background-color: #f8f9f9; color: #bdc3c7; }
    """

    # 테스트 버튼
    TEST = """
        QPushButton {
            background-color: #bdc3c7; color: #2c3e50;
            padding: 4px 12px; border-radius: 4px; border: 1px solid #95a5a6;
        }
        QPushButton:hover { background-color: #95a5a6; }
        QPushButton:disabled { background-color: #ecf0f1; color: #95a5a6; }
    """

    # 정보 버튼 (작은 크기) - settings.py btn_export, btn_refresh_log
    INFO_SMALL = """
            QPushButton {
                background-color: #3498db; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #2980b9; }
        """

    # 회색 버튼 (작은 크기) - settings.py btn_import, btn_open_log_folder
    MUTED_SMALL = """
            QPushButton {
                background-color: #95a5a6; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #7f8c8d; }
        """

    # 성공 버튼 (작은 크기) - settings.py btn_restore
    SUCCESS_SMALL = """
            QPushButton {
                background-color: #27ae60; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #219a52; }
        """

    # 위험 버튼 (작은 크기) - settings.py btn_clear_log
    DANGER_SMALL = """
            QPushButton {
                background-color: #e74c3c; color: white;
                padding: 6px 12px; border-radius: 4px; border: none;
                font-size: 11px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """

    # 기본 버튼 (중간 크기, disabled 포함) - settings.py btn_check_update
    PRIMARY_MD = """
            QPushButton {
                background-color: #3498db; color: white;
                padding: 8px 16px; border-radius: 4px; border: none;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #2980b9; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """

    # 성공 버튼 (중간 크기, disabled 포함) - settings.py btn_download
    SUCCESS_MD = """
            QPushButton {
                background-color: #27ae60; color: white;
                padding: 8px 16px; border-radius: 4px; border: none;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #229954; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """

    # 위험 버튼 (중간 크기) - settings.py btn_cancel_download
    DANGER_MD = """
            QPushButton {
                background-color: #e74c3c; color: white;
                padding: 8px 12px; border-radius: 4px; border: none;
                font-size: 12px;
            }
            QPushButton:hover { background-color: #c0392b; }
        """

    # 설치 전환 버튼 - settings.py _on_download_finished 에서 btn_download 재스타일
    INSTALL = """
                QPushButton {
                    background-color: #9b59b6; color: white;
                    padding: 8px 16px; border-radius: 4px; border: none;
                    font-size: 12px; font-weight: bold;
                }
                QPushButton:hover { background-color: #8e44ad; }
            """


class LabelStyles:
    """라벨 스타일 정의"""

    # 제목 (큰 글씨)
    TITLE = "font-size: 20px; font-weight: bold; color: #333;"

    # 섹션 헤더
    SECTION_HEADER = "font-weight: bold; color: #2c3e50; margin-top: 15px;"

    # 경고 메시지
    WARNING = "color: #f39c12; font-weight: bold;"

    # 작은 설명 텍스트
    CAPTION = "color: #7f8c8d; font-size: 11px;"

    # 강조 텍스트
    HIGHLIGHT = "color: #2c3e50; font-weight: bold;"


# =============================================================================
# 동적 테마 스타일 생성 함수
# =============================================================================

def get_dynamic_input_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 입력 필드 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QLineEdit, QSpinBox, QComboBox, QTextEdit, QPlainTextEdit {{
            padding: 6px 10px;
            border: 1px solid {colors.input_border};
            border-radius: 4px;
            background-color: {colors.input_background};
            color: {colors.foreground};
        }}
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus,
        QTextEdit:focus, QPlainTextEdit:focus {{
            border-color: {colors.input_border_focus};
        }}
        QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled,
        QTextEdit:disabled, QPlainTextEdit:disabled {{
            background-color: {colors.background_tertiary};
            color: {colors.foreground_disabled};
        }}
        QComboBox::drop-down {{
            border: none;
            padding-right: 8px;
        }}
        QComboBox QAbstractItemView {{
            background-color: {colors.input_background};
            color: {colors.foreground};
            selection-background-color: {colors.primary_light};
            selection-color: {colors.foreground};
            border: 1px solid {colors.input_border};
        }}
    """


def get_dynamic_groupbox_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 그룹박스 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QGroupBox {{
            font-weight: bold;
            border: 1px solid {colors.border};
            border-radius: 6px;
            margin-top: 12px;
            padding-top: 10px;
            background-color: {colors.background};
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
            color: {colors.foreground};
        }}
    """


def get_dynamic_table_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 테이블 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QTableWidget, QTableView {{
            border: 1px solid {colors.table_border};
            gridline-color: {colors.table_gridline};
            selection-background-color: {colors.table_selection};
            background-color: {colors.background};
            color: {colors.foreground};
            alternate-background-color: {colors.table_row_alt};
        }}
        QTableWidget::item, QTableView::item {{
            padding: 5px;
        }}
        QHeaderView::section {{
            background-color: {colors.table_header};
            padding: 8px;
            border: none;
            border-bottom: 1px solid {colors.table_border};
            font-weight: bold;
            color: {colors.foreground};
        }}
    """


def get_dynamic_tab_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 탭 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QTabWidget::pane {{
            border: 1px solid {colors.border};
            border-radius: 4px;
            background-color: {colors.background};
        }}
        QTabBar::tab {{
            background-color: {colors.background_tertiary};
            color: {colors.foreground};
            padding: 8px 16px;
            margin-right: 2px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }}
        QTabBar::tab:selected {{
            background-color: {colors.background};
            border: 1px solid {colors.border};
            border-bottom: none;
        }}
        QTabBar::tab:hover:!selected {{
            background-color: {colors.border_light};
        }}
    """


def get_dynamic_list_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 리스트 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QListWidget, QListView {{
            border: 1px solid {colors.border};
            border-radius: 4px;
            background-color: {colors.background};
            color: {colors.foreground};
            outline: none;
        }}
        QListWidget::item, QListView::item {{
            padding: 6px;
            border-radius: 2px;
        }}
        QListWidget::item:selected, QListView::item:selected {{
            background-color: {colors.table_selection};
            color: {colors.foreground};
        }}
        QListWidget::item:hover, QListView::item:hover {{
            background-color: {colors.background_tertiary};
        }}
    """


def get_dynamic_scrollbar_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 스크롤바 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QScrollBar:vertical {{
            border: none;
            background-color: {colors.background_secondary};
            width: 12px;
            margin: 0px;
        }}
        QScrollBar::handle:vertical {{
            background-color: {colors.scrollbar};
            min-height: 20px;
            border-radius: 6px;
            margin: 2px;
        }}
        QScrollBar::handle:vertical:hover {{
            background-color: {colors.scrollbar_hover};
        }}
        QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
            height: 0px;
        }}
        QScrollBar:horizontal {{
            border: none;
            background-color: {colors.background_secondary};
            height: 12px;
            margin: 0px;
        }}
        QScrollBar::handle:horizontal {{
            background-color: {colors.scrollbar};
            min-width: 20px;
            border-radius: 6px;
            margin: 2px;
        }}
        QScrollBar::handle:horizontal:hover {{
            background-color: {colors.scrollbar_hover};
        }}
        QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
            width: 0px;
        }}
    """


def get_dynamic_progress_style(colors: ThemeColors = None) -> str:
    """테마 기반 동적 프로그레스바 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QProgressBar {{
            border: 1px solid {colors.border};
            border-radius: 4px;
            text-align: center;
            height: 20px;
            background-color: {colors.background_tertiary};
            color: {colors.foreground};
        }}
        QProgressBar::chunk {{
            background-color: {colors.primary};
            border-radius: 3px;
        }}
    """


def get_full_app_style(colors: ThemeColors = None) -> str:
    """앱 전체에 적용할 기본 스타일"""
    if colors is None:
        colors = get_current_colors()

    return f"""
        QWidget {{
            background-color: {colors.background};
            color: {colors.foreground};
        }}
        QMainWindow {{
            background-color: {colors.background_secondary};
        }}
        QMenuBar {{
            background-color: {colors.background_tertiary};
            color: {colors.foreground};
        }}
        QMenuBar::item:selected {{
            background-color: {colors.primary_light};
        }}
        QMenu {{
            background-color: {colors.background};
            color: {colors.foreground};
            border: 1px solid {colors.border};
        }}
        QMenu::item:selected {{
            background-color: {colors.primary_light};
        }}
        QToolTip {{
            background-color: {colors.background_tertiary};
            color: {colors.foreground};
            border: 1px solid {colors.border};
            padding: 4px;
        }}
        QStatusBar {{
            background-color: {colors.background_tertiary};
            color: {colors.foreground_secondary};
        }}
        {get_dynamic_input_style(colors)}
        {get_dynamic_table_style(colors)}
        {get_dynamic_tab_style(colors)}
        {get_dynamic_list_style(colors)}
        {get_dynamic_scrollbar_style(colors)}
        {get_dynamic_progress_style(colors)}
        {get_dynamic_groupbox_style(colors)}
    """
