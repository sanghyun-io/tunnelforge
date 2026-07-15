import ast
from pathlib import Path

import pytest

from src.core import i18n
from src.core.i18n import keys as i18n_keys


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
    # configure_language resolves these helpers inside the keys module namespace,
    # so the package-level attribute must be patched on src.core.i18n.keys.
    monkeypatch.setattr(i18n_keys, "installer_language_hint_path", lambda: hint_path)
    monkeypatch.setattr(i18n_keys, "detect_system_language", lambda: "ko")
    config = FakeConfig()

    result = i18n.configure_language(config, ["TunnelForge"])

    assert result == "en"
    assert config.settings["language"] == "en"
    assert not hint_path.exists()


def test_translation_falls_back_to_korean():
    i18n.set_language("en")
    assert i18n.tr("common.save") == "Save"
    assert i18n.tr("missing.key") == "missing.key"


def test_error_reporting_consent_strings_are_available_in_korean_and_english():
    i18n.set_language("ko")
    assert i18n.tr("error_reporting_consent.enable") == "익명 오류 보고 활성화"
    assert i18n.tr("error_reporting_consent.public_issue") == (
        "전송되는 정리된 보고서는 공개 GitHub 이슈가 됩니다."
    )
    assert i18n.tr("error_reporting_consent.collected_details") == (
        "앱: 버전, 패키지 종류, UI 언어; 시스템: 운영 체제 종류와 버전, 아키텍처, 로캘, UTC 오프셋; 런타임: Python, Qt, Rust Core 버전; 작업: 종류, 데이터베이스 엔진, 서버 major.minor 버전, 단계; 오류: 예외 클래스, 코드, 정리된 메시지, 최대 20개의 앱 스택 프레임; 익명 설치 UUID; SHA-256 오류 지문"
    )
    assert i18n.tr("error_reporting_consent.excluded_details") == (
        "사용자 이름, 컴퓨터 이름, 이메일 주소; IP 주소, 호스트, 포트; 데이터베이스, 스키마, 테이블, 컬럼 이름; SQL과 데이터; 절대 경로와 UNC 경로; 환경 변수 값; 자격 증명과 토큰; 임의 첨부 파일"
    )

    i18n.set_language("en")
    assert i18n.tr("error_reporting_consent.enable") == "Enable anonymous error reporting"
    assert i18n.tr("error_reporting_consent.public_issue") == (
        "Sanitized reports become public GitHub issues."
    )
    assert i18n.tr("error_reporting_consent.collected_details") == (
        "Application: version, package kind, and UI language; System: OS family and version, architecture, locale, and UTC offset; Runtime: Python, Qt, and Rust Core versions; Operation: kind, database engine, server major.minor version, and phase; Error: exception class, code, sanitized message, and up to 20 application stack frames; Anonymous installation UUID; SHA-256 error fingerprint"
    )
    assert i18n.tr("error_reporting_consent.excluded_details") == (
        "User, computer, and email names; IP addresses, hosts, and ports; database, schema, table, and column names; SQL and data; absolute and UNC paths; environment variable values; credentials and tokens; arbitrary attachments"
    )
    i18n.set_language("ko")

    i18n.set_language("unsupported")
    assert i18n.tr("common.save") == "저장"


def test_error_reporting_settings_strings_are_available_in_korean_and_english():
    i18n.set_language("ko")
    assert i18n.tr("settings.error_reporting.title") == "익명 오류 보고"
    assert i18n.tr("settings.error_reporting.settings_path") == (
        "Settings > General > Anonymous Error Reporting"
    )
    assert "App" not in i18n.tr("settings.error_reporting.disclosure")

    i18n.set_language("en")
    assert i18n.tr("settings.error_reporting.title") == "Anonymous Error Reporting"
    assert i18n.tr("settings.error_reporting.health") == "Test relay connection"
    assert i18n.tr("settings.error_reporting.settings_path") == (
        "Settings > General > Anonymous Error Reporting"
    )


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
    assert i18n.translate_text("항목") == "Item"
    assert i18n.translate_text("자동 커밋") == "Auto Commit"
    assert i18n.translate_text("사용 중") == "In Use"


def test_ssh_host_trust_dialog_strings_have_exact_english_translations():
    i18n.set_language("en")

    expected = {
        "SSH 호스트 키 확인": "SSH Host Key Verification",
        "처음 연결하는 SSH 서버입니다.": "This is the first connection to this SSH server.",
        "서버:": "Server:",
        "키 알고리즘:": "Key algorithm:",
        "SHA256 지문:": "SHA256 fingerprint:",
        "이 지문을 서버 관리자 또는 신뢰할 수 있는 채널로 확인한 후 계속하세요.": (
            "Verify this fingerprint with the server administrator or through a trusted channel before continuing."
        ),
        "신뢰하고 계속": "Trust and Continue",
        "SSH 호스트 키 변경 감지": "SSH Host Key Change Detected",
        "SSH 서버의 호스트 키가 이전에 승인한 키와 다릅니다.": (
            "The SSH server host key differs from the previously approved key."
        ),
        "이전 SHA256 지문:": "Previous SHA256 fingerprint:",
        "새 SHA256 지문:": "New SHA256 fingerprint:",
        "보안을 위해 연결을 차단했습니다. 서버 관리자를 통해 변경 사유를 확인하세요.": (
            "The connection was blocked for security. Confirm the reason for the change with the server administrator."
        ),
        "SSH 호스트 키 확인 실패": "SSH Host Key Verification Failed",
        "SSH 서버의 호스트 키를 안전하게 확인할 수 없어 연결을 중단했습니다.": (
            "The connection was stopped because the SSH server host key could not be verified safely."
        ),
        "SSH 호스트 키 승인이 완료되지 않았습니다.": (
            "SSH host key approval was not completed."
        ),
    }

    for korean, english in expected.items():
        assert i18n.translate_text(korean) == english


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
    combo.addItem("백업")
    assert combo.itemText(0) == "백업"
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


def test_qcombobox_inserted_identity_text_is_not_translated(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication, QComboBox

    app = QApplication.instance() or QApplication([])
    assert app is not None

    i18n.set_language("en")
    i18n.install_qt_i18n()

    combo = QComboBox()
    combo.addItem("백업")
    combo.insertItem(0, "로그")
    combo.addItems(["스키마", "데이터베이스"])
    combo.addItem("원본", "백업")

    assert combo.itemText(0) == "로그"
    assert combo.itemText(1) == "백업"
    assert combo.itemText(2) == "스키마"
    assert combo.itemText(3) == "데이터베이스"
    assert combo.itemText(4) == "원본"
    assert combo.itemData(4) == "백업"


def test_en_phrase_translations_have_no_duplicate_source_keys():
    project_root = Path(__file__).resolve().parents[1]
    source_path = project_root / "src" / "core" / "i18n" / "legacy_translate.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"))

    found = False
    duplicates = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "_EN_PHRASE_TRANSLATIONS" for target in node.targets):
            continue

        found = True
        seen = {}
        for key_node in node.value.keys:
            if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
                continue
            key = key_node.value
            if key in seen:
                duplicates.append((key, seen[key], key_node.lineno))
            else:
                seen[key] = key_node.lineno

    assert found, "_EN_PHRASE_TRANSLATIONS assignment not found in legacy_translate.py"
    assert duplicates == []


def test_direct_hardcoded_qt_ui_strings_have_english_runtime_translation():
    ui_functions = {
        "QLabel", "QPushButton", "QCheckBox", "QRadioButton", "QGroupBox",
        "QAction", "QMenu", "QListWidgetItem", "QTableWidgetItem",
        "QTreeWidgetItem",
    }
    ui_methods = {
        "setWindowTitle", "setText", "setTitle", "setToolTip", "setStatusTip",
        "setWhatsThis", "setPlaceholderText", "addTab", "setTabText",
        "addRow", "setItemText",
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
