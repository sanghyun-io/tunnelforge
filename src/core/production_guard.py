"""Production 환경 보호 유틸리티

Production 환경에서 위험 작업 실행 시 추가 확인 절차를 통해
실수로 인한 데이터 손실을 방지합니다.

환경 타입:
- PRODUCTION: 스키마명 직접 입력 필요
- STAGING: Yes/No 확인 다이얼로그
- DEVELOPMENT: 확인 없이 바로 실행
- UNKNOWN: 미분류 환경으로 기본 No 확인
"""
from enum import Enum
from typing import Optional, Tuple
import re

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QMessageBox, QFrame
)
from PyQt6.QtCore import Qt

from src.core.i18n import translate_text


class Environment(Enum):
    """환경 타입 Enum"""
    PRODUCTION = "production"    # 🔴 스키마명 직접 입력 필요
    STAGING = "staging"          # 🟠 Yes/No 확인 다이얼로그
    DEVELOPMENT = "development"  # 🟢 확인 없이 바로 실행
    UNKNOWN = None               # 미설정 (기본 No 확인)

    @classmethod
    def from_string(cls, value: Optional[str]) -> 'Environment':
        """문자열에서 Environment enum으로 변환"""
        if value is None:
            return cls.UNKNOWN
        for env in cls:
            if env.value == value:
                return env
        return cls.UNKNOWN


# SchemaConfirmDialog 스타일 상수 (ENV_COLORS와 별개로 중립적인 UI 색상)
_NEUTRAL_BG = "#f8f9fa"
_NEUTRAL_BORDER = "#dee2e6"
_INPUT_BORDER = "#bdc3c7"
_INPUT_FOCUS_BORDER = "#3498db"


class SchemaConfirmDialog(QDialog):
    """스키마명 직접 입력 확인 다이얼로그

    Production 환경에서 위험 작업 실행 시 사용자가 스키마명을
    정확히 입력해야만 진행할 수 있는 확인 다이얼로그입니다.
    """

    # 환경별 색상 정의
    ENV_COLORS = {
        Environment.PRODUCTION: ("#c0392b", "#fadbd8"),   # 빨강 (텍스트, 배경)
        Environment.STAGING: ("#d35400", "#fdebd0"),      # 주황 (텍스트, 배경)
        Environment.DEVELOPMENT: ("#27ae60", "#d5f5e3"), # 초록 (텍스트, 배경)
    }

    ENV_LABELS = {
        Environment.PRODUCTION: "🔴 PRODUCTION",
        Environment.STAGING: "🟠 STAGING",
        Environment.DEVELOPMENT: "🟢 DEVELOPMENT",
    }

    def __init__(self, parent, operation: str, schema_name: str,
                 environment: Environment, details: str = ""):
        """
        Args:
            parent: 부모 위젯
            operation: 작업 설명 (예: "데이터 Import", "DELETE 쿼리 실행")
            schema_name: 확인이 필요한 스키마명
            environment: 현재 환경
            details: 추가 상세 정보 (HTML 가능)
        """
        super().__init__(parent)
        self.schema_name = schema_name
        self.environment = environment
        self._init_ui(operation, details)

    def _init_ui(self, operation: str, details: str):
        """UI 초기화 (조립만 담당 — 각 섹션은 포커스된 빌더에서 생성)"""
        self.setWindowTitle(f"⚠️ {self.ENV_LABELS.get(self.environment, 'UNKNOWN')} 환경")
        self.setMinimumWidth(450)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(15)

        # 환경 색상 가져오기
        text_color, bg_color = self.ENV_COLORS.get(
            self.environment, ("#333", "#fff")
        )

        layout.addWidget(self._build_header_frame(text_color, bg_color))

        # 작업 설명
        operation_label = QLabel(f"<b>{operation}</b> 작업을 실행하려 합니다.")
        operation_label.setWordWrap(True)
        layout.addWidget(operation_label)

        # 경고 메시지
        warning_label = QLabel("⚠️ 이 작업은 되돌릴 수 없습니다.")
        warning_label.setStyleSheet(f"color: {text_color}; font-weight: bold;")
        layout.addWidget(warning_label)

        details_label = self._build_details_section(details)
        if details_label is not None:
            layout.addWidget(details_label)

        for widget in self._build_schema_input_section(text_color, bg_color):
            layout.addWidget(widget)

        layout.addLayout(self._build_button_row(text_color))

    def _build_header_frame(self, text_color: str, bg_color: str) -> QFrame:
        """환경 라벨을 담은 헤더 프레임 (배경색 포함) 생성"""
        header_frame = QFrame()
        header_frame.setStyleSheet(f"""
            QFrame {{
                background-color: {bg_color};
                border: 2px solid {text_color};
                border-radius: 8px;
                padding: 10px;
            }}
        """)
        header_layout = QVBoxLayout(header_frame)

        env_label = QLabel(self.ENV_LABELS.get(self.environment, "UNKNOWN"))
        env_label.setStyleSheet(f"color: {text_color}; font-size: 18px; font-weight: bold;")
        env_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(env_label)

        return header_frame

    def _build_details_section(self, details: str) -> Optional[QLabel]:
        """상세 정보 라벨 생성 (details가 없으면 None)"""
        if not details:
            return None

        details_label = QLabel(details)
        details_label.setWordWrap(True)
        details_label.setStyleSheet(f"""
            background-color: {_NEUTRAL_BG};
            border: 1px solid {_NEUTRAL_BORDER};
            border-radius: 4px;
            padding: 8px;
        """)
        return details_label

    def _build_schema_input_section(self, text_color: str, bg_color: str):
        """구분선 + 안내 문구 + 타겟 스키마 표시 + 입력 필드를 순서대로 생성

        Returns:
            layout에 순서대로 addWidget할 위젯 리스트
        """
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setFrameShadow(QFrame.Shadow.Sunken)

        instruction_label = QLabel("계속하려면 <b>스키마명을 정확히 입력</b>하세요:")

        schema_display = QLabel(self.schema_name)
        schema_display.setStyleSheet(f"""
            background-color: {bg_color};
            border: 2px solid {text_color};
            border-radius: 4px;
            padding: 10px;
            font-family: 'Consolas', 'Courier New', monospace;
            font-size: 14px;
            font-weight: bold;
            color: {text_color};
        """)
        schema_display.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.input_schema = QLineEdit()
        self.input_schema.setPlaceholderText("스키마명 입력...")
        self.input_schema.setStyleSheet(f"""
            QLineEdit {{
                padding: 10px;
                font-size: 14px;
                border: 2px solid {_INPUT_BORDER};
                border-radius: 4px;
            }}
            QLineEdit:focus {{
                border-color: {_INPUT_FOCUS_BORDER};
            }}
        """)
        self.input_schema.textChanged.connect(self._on_text_changed)

        return [line, instruction_label, schema_display, self.input_schema]

    def _build_button_row(self, text_color: str) -> QHBoxLayout:
        """취소/실행 버튼 레이아웃 생성 (초기에는 실행 버튼 비활성화)"""
        button_layout = QHBoxLayout()
        button_layout.addStretch()

        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.setMinimumWidth(100)
        self.btn_cancel.clicked.connect(self.reject)
        button_layout.addWidget(self.btn_cancel)

        self.btn_execute = QPushButton("실행")
        self.btn_execute.setMinimumWidth(100)
        self.btn_execute.setEnabled(False)
        self.btn_execute.setStyleSheet(f"""
            QPushButton {{
                background-color: {text_color};
                color: white;
                font-weight: bold;
                padding: 8px 16px;
                border: none;
                border-radius: 4px;
            }}
            QPushButton:disabled {{
                background-color: #bdc3c7;
            }}
            QPushButton:hover:enabled {{
                opacity: 0.9;
            }}
        """)
        self.btn_execute.clicked.connect(self.accept)
        button_layout.addWidget(self.btn_execute)

        return button_layout

    def _on_text_changed(self, text: str):
        """입력 텍스트 변경 시 버튼 활성화 상태 업데이트"""
        # 입력값과 스키마명이 정확히 일치할 때만 버튼 활성화
        self.btn_execute.setEnabled(text.strip() == self.schema_name)


class ProductionGuard:
    """Production 환경 보호 유틸리티 클래스

    환경별로 다른 확인 절차를 제공하여 실수로 인한 데이터 손실을 방지합니다.

    사용 예시:
        guard = ProductionGuard(parent_widget)
        if guard.confirm_dangerous_operation(tunnel_config, "데이터 Import", "my_db"):
            # 작업 진행
    """

    # 위험 SQL 키워드
    DANGEROUS_KEYWORDS = ['DELETE', 'UPDATE', 'DROP', 'ALTER', 'TRUNCATE', 'INSERT']

    def __init__(self, parent=None):
        """
        Args:
            parent: 다이얼로그의 부모 위젯
        """
        self.parent = parent

    @staticmethod
    def get_environment(tunnel_config: dict) -> Environment:
        """터널 설정에서 환경 정보 추출

        Args:
            tunnel_config: 터널 설정 딕셔너리

        Returns:
            Environment enum 값
        """
        return Environment.from_string(tunnel_config.get('environment'))

    @staticmethod
    def is_production(tunnel_config: dict) -> bool:
        """Production 환경인지 확인

        Args:
            tunnel_config: 터널 설정 딕셔너리

        Returns:
            Production이면 True
        """
        return ProductionGuard.get_environment(tunnel_config) == Environment.PRODUCTION

    @staticmethod
    def is_dangerous_query(query: str) -> Tuple[bool, Optional[str]]:
        """위험 쿼리인지 확인

        SQL 주석을 제거한 후 위험 키워드를 체크합니다.

        Args:
            query: SQL 쿼리 문자열

        Returns:
            (위험 여부, 발견된 키워드 또는 None)
        """
        if not query:
            return False, None

        # SQL 주석 제거
        # 1. 한 줄 주석 (-- 또는 #)
        clean_query = re.sub(r'--[^\n]*', '', query)
        clean_query = re.sub(r'#[^\n]*', '', clean_query)
        # 2. 블록 주석 (/* */)
        clean_query = re.sub(r'/\*.*?\*/', '', clean_query, flags=re.DOTALL)

        # 대문자로 변환하여 키워드 체크
        upper_query = clean_query.upper()

        for keyword in ProductionGuard.DANGEROUS_KEYWORDS:
            # 단어 경계를 확인하여 정확한 키워드 매칭
            pattern = r'\b' + keyword + r'\b'
            if re.search(pattern, upper_query):
                return True, keyword

        return False, None

    def confirm_dangerous_operation(
        self,
        tunnel_config: dict,
        operation: str,
        schema_name: str,
        details: str = ""
    ) -> bool:
        """위험 작업 확인 다이얼로그 표시

        환경에 따라 다른 확인 방식을 사용합니다:
        - Production: 스키마명 직접 입력 확인
        - Staging: Yes/No 확인 (기본값 No)
        - Unknown: 미분류 환경 경고 (기본값 No)
        - Development: 확인 없이 바로 진행 (True 반환)

        Args:
            tunnel_config: 터널 설정 딕셔너리
            operation: 작업 설명 (예: "데이터 Import")
            schema_name: 확인이 필요한 스키마명
            details: 추가 상세 정보 (HTML 가능)

        Returns:
            사용자가 확인하면 True, 취소하면 False
        """
        environment = self.get_environment(tunnel_config)

        if environment == Environment.PRODUCTION:
            # Production: 스키마명 입력 확인
            dialog = SchemaConfirmDialog(
                self.parent,
                operation,
                schema_name,
                environment,
                details
            )
            return dialog.exec() == QDialog.DialogCode.Accepted

        elif environment == Environment.STAGING:
            # Staging: Yes/No 확인 (기본값 No)
            message = (
                f"<b>{operation}</b> 작업을 실행하시겠습니까?<br><br>"
                f"스키마: <b>{schema_name}</b><br><br>"
            )
            if details:
                message += details

            reply = QMessageBox.warning(
                self.parent,
                f"🟠 STAGING 환경 - {operation}",
                message,
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No  # 기본값: No
            )
            return reply == QMessageBox.StandardButton.Yes

        elif environment == Environment.UNKNOWN:
            # 환경 metadata가 없거나 분류할 수 없으면 fail-closed로 확인합니다.
            message = (
                f"<b>{operation}</b> 작업을 실행하시겠습니까?<br><br>"
                "환경: <b>미분류 (UNKNOWN)</b><br>"
                f"스키마: <b>{schema_name}</b><br><br>"
            )
            if details:
                message += details

            reply = QMessageBox.warning(
                self.parent,
                translate_text(f"⚪ 미분류 환경 - {operation}"),
                translate_text(message),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            return reply == QMessageBox.StandardButton.Yes

        # 명시적으로 Development인 경우에만 확인 없이 진행합니다.
        return environment == Environment.DEVELOPMENT

    def confirm_dangerous_query(
        self,
        tunnel_config: dict,
        query: str,
        schema_name: str
    ) -> bool:
        """위험 SQL 쿼리 실행 확인

        쿼리가 위험 키워드를 포함하는지 확인하고,
        포함한다면 환경에 맞는 확인 다이얼로그를 표시합니다.

        Args:
            tunnel_config: 터널 설정 딕셔너리
            query: 실행할 SQL 쿼리
            schema_name: 대상 스키마명

        Returns:
            안전하거나 사용자가 확인하면 True, 취소하면 False
        """
        is_dangerous, keyword = self.is_dangerous_query(query)

        if not is_dangerous:
            return True  # 안전한 쿼리는 바로 진행

        # 쿼리 미리보기 (200자 제한)
        preview = query[:200] + "..." if len(query) > 200 else query

        return self.confirm_dangerous_operation(
            tunnel_config,
            f"{keyword} 쿼리 실행",
            schema_name,
            f"<pre style='background: #f5f5f5; padding: 8px; border-radius: 4px;'>{preview}</pre>"
        )
