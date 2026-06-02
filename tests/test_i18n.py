import ast
from pathlib import Path

import pytest

from src.core import i18n


@pytest.fixture(autouse=True)
def reset_language():
    i18n.set_language(i18n.DEFAULT_LANGUAGE)
    yield
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


class FakeConfig:
    def __init__(self, language=None):
        self.settings = {}
        if language is not None:
            self.settings["language"] = language
        self.saved = []

    def get_app_setting(self, key, default=None):
        return self.settings.get(key, default)

    def set_app_setting(self, key, value):
        self.settings[key] = value
        self.saved.append((key, value))


def test_normalize_language_supports_korean_and_english():
    assert i18n.normalize_language("ko-KR") == "ko"
    assert i18n.normalize_language("en_US") == "en"
    assert i18n.normalize_language("fr") == "ko"


def test_configure_language_prefers_cli_arg_and_persists():
    config = FakeConfig("ko")

    result = i18n.configure_language(config, ["TunnelForge", "--language=en"])

    assert result == "en"
    assert i18n.current_language() == "en"
    assert config.settings["language"] == "en"


def test_configure_language_uses_installer_hint_before_system_locale(monkeypatch, tmp_path):
    hint_path = tmp_path / i18n.INSTALLER_LANGUAGE_HINT_FILE
    hint_path.write_text("en", encoding="utf-8")
    monkeypatch.setattr(i18n, "installer_language_hint_path", lambda: hint_path)
    monkeypatch.setattr(i18n, "detect_system_language", lambda: "ko")
    config = FakeConfig()

    result = i18n.configure_language(config, ["TunnelForge"])

    assert result == "en"
    assert config.settings["language"] == "en"
    assert not hint_path.exists()


def test_translation_falls_back_to_korean():
    i18n.set_language("en")
    assert i18n.tr("common.save") == "Save"
    assert i18n.tr("missing.key") == "missing.key"

    i18n.set_language("unsupported")
    assert i18n.tr("common.save") == "저장"


def test_translate_text_handles_common_hardcoded_ui_phrases():
    i18n.set_language("en")

    assert i18n.translate_text("삭제 확인") == "Delete Confirmation"
    assert i18n.translate_text("터널 연결에 실패했습니다.\n\n원인: timeout") == (
        "Failed to connect tunnel.\n\nCause: timeout"
    )
    assert i18n.translate_text("실행 중인 터널은 삭제할 수 없습니다.") == (
        "Running tunnels cannot be deleted."
    )
    assert i18n.translate_text("그룹 없음") == "Ungrouped"
    assert i18n.translate_text("새로운 버전") != "New운 버전"
    assert i18n.translate_text("'운영' 그룹을 삭제하시겠습니까?\n\n그룹에 속한 터널은 '그룹 없음'으로 이동됩니다.") == (
        "Do you want to delete group '운영'?\n\nTunnels in this group will move to 'Ungrouped'."
    )
    assert i18n.translate_text("새로운 버전 v2.1.0이 사용 가능합니다.\n설정에서 다운로드할 수 있습니다.") == (
        "New version v2.1.0 is available.\nYou can download it in Settings."
    )
    assert i18n.translate_text("3개 테이블") == "3 tables"
    assert i18n.translate_text("선택: 3개") == "Select: 3"
    assert i18n.translate_text("DB 변경 실행을 사용할 수 있습니다.") == "Run DB Changes can be used."
    assert i18n.translate_text("SQL 파일 (*.sql);;모든 파일 (*.*)") == "SQL files (*.sql);;All files (*.*)"
    assert i18n.translate_text("Mismatch: {}개") == "Mismatch: {}"
    assert i18n.translate_text("Rust Core 검사 완료: {}개 테이블") == "Rust Core inspection complete: {} tables"
    assert i18n.translate_text("✅ {}개 테이블 로드됨 (MySQL {})") == "✅ {} tables loaded (MySQL {})"
    assert i18n.translate_text("이 Dump는 권장 Import 경로로 진행할 수 없습니다.\n\n{}") == (
        "This dump cannot proceed through the recommended import path.\n\n{}"
    )
    assert i18n.translate_text("이 Dump는 Import할 수 없습니다.\n\n{}") == (
        "This dump cannot be imported.\n\n{}"
    )
    assert i18n.translate_text("제한적 복원 Import 확인") == (
        "Limited Restore Import Confirmation"
    )
    assert i18n.translate_text("일관성 모드:") == "Consistency mode:"


def test_qt_i18n_hooks_translate_hardcoded_widget_text(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import (
        QApplication,
        QComboBox,
        QFormLayout,
        QLabel,
        QLineEdit,
        QPushButton,
        QTableWidget,
        QTableWidgetItem,
        QTextEdit,
        QPlainTextEdit,
        QWidget,
    )

    app = QApplication.instance() or QApplication([])
    assert app is not None

    i18n.set_language("en")
    i18n.install_qt_i18n()

    label = QLabel("삭제 확인")
    button = QPushButton("취소")
    form = QFormLayout()
    field = QLineEdit()
    form.addRow("이름:", field)
    combo = QComboBox()
    combo.addItem("삭제")
    combo.setItemText(0, "취소")
    table = QTableWidget(1, 2)
    table.setHorizontalHeaderLabels(["시간", "메시지"])
    user_data_item = QTableWidgetItem("사용자 테이블 데이터")
    text_edit = QTextEdit()
    text_edit.append("검증 실패: 새 검증 결과를 받지 못했습니다.")
    plain_text = QPlainTextEdit()
    plain_text.appendPlainText("실행 로그가 없습니다.")

    assert label.text() == "Delete Confirmation"
    assert button.text() == "Cancel"
    assert form.labelForField(field).text() == "Name:"
    assert combo.itemText(0) == "Cancel"
    assert table.horizontalHeaderItem(0).text() == "Time"
    assert table.horizontalHeaderItem(1).text() == "Message"
    assert user_data_item.text() == "사용자 테이블 데이터"
    assert text_edit.toPlainText() == "Verification failed: no new verification result was received."
    assert plain_text.toPlainText() == "There are no execution logs."

    container = QWidget()
    container.setWindowTitle("터널 상태")
    assert container.windowTitle() == "Tunnel Status"


def test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation():
    ui_functions = {
        "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox",
        "QAction", "QMenu", "QListWidgetItem", "QTableWidgetItem",
        "QTreeWidgetItem",
    }
    ui_methods = {
        "setWindowTitle", "setText", "setTitle", "setToolTip", "setStatusTip",
        "setWhatsThis", "setPlaceholderText", "addTab", "setTabText",
        "addRow", "addItem", "insertItem", "addItems", "setItemText",
        "setHeaderLabels", "setHorizontalHeaderLabels", "setVerticalHeaderLabels",
        "showMessage", "information", "warning", "critical", "question",
        "getOpenFileName", "getSaveFileName", "getExistingDirectory",
        "setButtonText", "setSubTitle", "showText", "setFormat", "append",
        "appendPlainText", "setHtml", "setPlainText",
    }
    project_root = Path(__file__).resolve().parents[1]
    paths = list((project_root / "src" / "ui").rglob("*.py"))
    paths.append(project_root / "src" / "core" / "production_guard.py")

    def call_name(func):
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
        return ""

    def has_hangul(value):
        return any("\uac00" <= char <= "\ud7a3" for char in value)

    def text_template(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    parts.append(value.value)
                elif isinstance(value, ast.FormattedValue):
                    parts.append("{}")
            return "".join(parts)
        return None

    i18n.set_language("en")
    untranslated = []
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = call_name(node.func)
            if name not in ui_functions and name not in ui_methods:
                continue
            candidates = []
            for arg in [*node.args, *(keyword.value for keyword in node.keywords)]:
                template = text_template(arg)
                if template is not None:
                    candidates.append(template)
                elif isinstance(arg, (ast.List, ast.Tuple)):
                    candidates.extend(
                        template
                        for elt in arg.elts
                        if (template := text_template(elt)) is not None
                    )
            for value in candidates:
                if has_hangul(value) and has_hangul(i18n.translate_text(value)):
                    untranslated.append(f"{path.relative_to(project_root)}:{node.lineno}: {value}")

    assert untranslated == []
