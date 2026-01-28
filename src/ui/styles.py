"""
UI 스타일시트 중앙화 모듈

모든 UI 파일에서 사용하는 CSS 스타일을 한 곳에서 관리하여
- CSS 파싱 중복 제거
- 유지보수성 향상
- 일관된 디자인 시스템 적용
"""


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

    # 아이콘 버튼 (작은 크기)
    ICON_SMALL = """
        QPushButton {
            background-color: transparent;
            padding: 2px 6px; border-radius: 3px; border: none;
        }
        QPushButton:hover { background-color: #ecf0f1; }
    """

    # 플랫 버튼 (배경 없음)
    FLAT = """
        QPushButton {
            background-color: transparent; color: #3498db;
            padding: 4px 8px; border: none;
        }
        QPushButton:hover { color: #2980b9; text-decoration: underline; }
    """


class LabelStyles:
    """라벨 스타일 정의"""

    # 제목 (큰 글씨)
    TITLE = "font-size: 20px; font-weight: bold; color: #333;"

    # 섹션 헤더
    SECTION_HEADER = "font-weight: bold; color: #2c3e50; margin-top: 15px;"

    # 성공 메시지
    SUCCESS = "color: #27ae60; font-weight: bold;"

    # 오류 메시지
    ERROR = "color: #e74c3c; font-weight: bold;"

    # 경고 메시지
    WARNING = "color: #f39c12; font-weight: bold;"

    # 정보 메시지
    INFO = "color: #3498db;"

    # 비활성화된 텍스트
    DISABLED = "color: #95a5a6;"

    # 작은 설명 텍스트
    CAPTION = "color: #7f8c8d; font-size: 11px;"

    # 강조 텍스트
    HIGHLIGHT = "color: #2c3e50; font-weight: bold;"


class InputStyles:
    """입력 필드 스타일 정의"""

    # 기본 입력 필드
    DEFAULT = """
        QLineEdit, QSpinBox, QComboBox {
            padding: 6px 10px;
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            background-color: white;
        }
        QLineEdit:focus, QSpinBox:focus, QComboBox:focus {
            border-color: #3498db;
        }
        QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {
            background-color: #ecf0f1;
            color: #95a5a6;
        }
    """

    # 오류 상태
    ERROR = """
        QLineEdit, QSpinBox {
            border: 1px solid #e74c3c;
            background-color: #fdf2f0;
        }
    """

    # 성공 상태
    SUCCESS = """
        QLineEdit, QSpinBox {
            border: 1px solid #27ae60;
            background-color: #f0fdf4;
        }
    """


class GroupBoxStyles:
    """그룹박스 스타일 정의"""

    DEFAULT = """
        QGroupBox {
            font-weight: bold;
            border: 1px solid #bdc3c7;
            border-radius: 6px;
            margin-top: 12px;
            padding-top: 10px;
        }
        QGroupBox::title {
            subcontrol-origin: margin;
            left: 10px;
            padding: 0 5px;
            color: #2c3e50;
        }
    """


class TableStyles:
    """테이블 스타일 정의"""

    DEFAULT = """
        QTableWidget {
            border: 1px solid #bdc3c7;
            gridline-color: #ecf0f1;
            selection-background-color: #d5f5e3;
        }
        QTableWidget::item {
            padding: 5px;
        }
        QHeaderView::section {
            background-color: #ecf0f1;
            padding: 8px;
            border: none;
            border-bottom: 1px solid #bdc3c7;
            font-weight: bold;
        }
    """


class ProgressStyles:
    """프로그레스바 스타일 정의"""

    DEFAULT = """
        QProgressBar {
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            text-align: center;
            height: 20px;
        }
        QProgressBar::chunk {
            background-color: #3498db;
            border-radius: 3px;
        }
    """

    SUCCESS = """
        QProgressBar::chunk {
            background-color: #27ae60;
        }
    """

    WARNING = """
        QProgressBar::chunk {
            background-color: #f1c40f;
        }
    """


class TextEditStyles:
    """텍스트 에디터 스타일 정의"""

    # 로그 출력용 (모노스페이스)
    LOG = """
        QTextEdit {
            font-family: Consolas, 'Courier New', monospace;
            font-size: 12px;
            background-color: #1e1e1e;
            color: #d4d4d4;
            border: 1px solid #3c3c3c;
            border-radius: 4px;
            padding: 8px;
        }
    """

    # 일반 텍스트 에디터
    DEFAULT = """
        QTextEdit {
            border: 1px solid #bdc3c7;
            border-radius: 4px;
            padding: 8px;
        }
        QTextEdit:focus {
            border-color: #3498db;
        }
    """


class TabStyles:
    """탭 위젯 스타일 정의"""

    DEFAULT = """
        QTabWidget::pane {
            border: 1px solid #bdc3c7;
            border-radius: 4px;
        }
        QTabBar::tab {
            background-color: #ecf0f1;
            padding: 8px 16px;
            margin-right: 2px;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
        }
        QTabBar::tab:selected {
            background-color: white;
            border: 1px solid #bdc3c7;
            border-bottom: none;
        }
        QTabBar::tab:hover:!selected {
            background-color: #d5dbdb;
        }
    """


class DialogStyles:
    """다이얼로그 스타일 정의"""

    # 다이얼로그 기본 배경
    DEFAULT = "background-color: #f8f9f9;"


# 자주 사용하는 색상 상수
class Colors:
    """색상 상수"""

    PRIMARY = "#3498db"
    PRIMARY_DARK = "#2980b9"

    SUCCESS = "#27ae60"
    SUCCESS_LIGHT = "#2ecc71"

    DANGER = "#e74c3c"
    DANGER_DARK = "#c0392b"

    WARNING = "#f1c40f"
    WARNING_DARK = "#d4ac0d"

    INFO = "#3498db"

    GRAY_LIGHT = "#ecf0f1"
    GRAY = "#bdc3c7"
    GRAY_DARK = "#95a5a6"

    TEXT_PRIMARY = "#2c3e50"
    TEXT_SECONDARY = "#7f8c8d"

    WHITE = "#ffffff"
    BLACK = "#333333"


def apply_button_style(button, style: str):
    """버튼에 스타일 적용 헬퍼 함수

    Args:
        button: QPushButton 인스턴스
        style: ButtonStyles 클래스의 스타일 상수
    """
    button.setStyleSheet(style)


def apply_label_style(label, style: str):
    """라벨에 스타일 적용 헬퍼 함수

    Args:
        label: QLabel 인스턴스
        style: LabelStyles 클래스의 스타일 상수
    """
    label.setStyleSheet(style)
