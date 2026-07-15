from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
import pytest

from src.core import i18n
from src.core.error_report_consent import PromptOutcome
from src.ui.dialogs.error_reporting_consent_dialog import ErrorReportingConsentDialog


APP = QApplication.instance() or QApplication([])


DISCLOSURES = {
    "ko": {
        "collected": "앱: 버전, 패키지 종류, UI 언어; 시스템: 운영 체제 종류와 버전, 아키텍처, 로캘, UTC 오프셋; 런타임: Python, Qt, Rust Core 버전; 작업: 종류, 데이터베이스 엔진, 서버 major.minor 버전, 단계; 오류: 예외 클래스, 코드, 정리된 메시지, 최대 20개의 앱 스택 프레임; 익명 설치 UUID; SHA-256 오류 지문",
        "excluded": "사용자 이름, 컴퓨터 이름, 이메일 주소; IP 주소, 호스트, 포트; 데이터베이스, 스키마, 테이블, 컬럼 이름; SQL과 데이터; 절대 경로와 UNC 경로; 환경 변수 값; 자격 증명과 토큰; 임의 첨부 파일",
    },
    "en": {
        "collected": "Application: version, package kind, and UI language; System: OS family and version, architecture, locale, and UTC offset; Runtime: Python, Qt, and Rust Core versions; Operation: kind, database engine, server major.minor version, and phase; Error: exception class, code, sanitized message, and up to 20 application stack frames; Anonymous installation UUID; SHA-256 error fingerprint",
        "excluded": "User, computer, and email names; IP addresses, hosts, and ports; database, schema, table, and column names; SQL and data; absolute and UNC paths; environment variable values; credentials and tokens; arbitrary attachments",
    },
}


@pytest.mark.parametrize("language", ["ko", "en"])
def test_dialog_discloses_exact_full_schema_boundaries_without_network_calls(
    monkeypatch,
    language,
):
    requests = []
    monkeypatch.setattr(
        "requests.sessions.Session.request",
        lambda *args, **kwargs: requests.append((args, kwargs)),
    )
    i18n.set_language(language)
    dialog = ErrorReportingConsentDialog()

    try:
        text = "\n".join(label.text() for label in dialog.findChildren(type(dialog.title_label)))

        assert dialog.collected_details_label.text() == DISCLOSURES[language]["collected"]
        assert dialog.excluded_details_label.text() == DISCLOSURES[language]["excluded"]
        assert DISCLOSURES[language]["collected"] in text
        assert DISCLOSURES[language]["excluded"] in text
        assert dialog.suppression_checkbox.isChecked() is False
        assert dialog.minimumWidth() >= 420
        assert dialog.maximumWidth() <= 640
        dialog.show()
        dialog.collected_expander.click()
        dialog.excluded_expander.click()
        APP.processEvents()
        assert requests == []
    finally:
        dialog.deleteLater()
        i18n.set_language("ko")


def test_dialog_preserves_public_issue_settings_actions_and_accessibility():
    i18n.set_language("en")
    dialog = ErrorReportingConsentDialog()

    try:
        text = "\n".join(label.text() for label in dialog.findChildren(type(dialog.title_label)))

        assert dialog.windowTitle() == "Help improve TunnelForge"
        assert "public GitHub issue" in text
        assert "Settings > General" in text
        assert dialog.collected_expander.text() == "What is collected"
        assert dialog.excluded_expander.text() == "What is not collected"
        assert dialog.later_button.text() == "Later"
        assert dialog.enable_button.text() == "Enable anonymous error reporting"
        assert dialog.enable_button.accessibleName() == "Enable anonymous error reporting"
        assert dialog.later_button.accessibleName() == "Decide later"
    finally:
        dialog.deleteLater()
        i18n.set_language("ko")


def test_dialog_enable_returns_enable_without_network_calls(monkeypatch):
    requests = []
    monkeypatch.setattr("requests.sessions.Session.request", lambda *args, **kwargs: requests.append((args, kwargs)))
    dialog = ErrorReportingConsentDialog()

    dialog.enable_button.click()

    assert dialog.get_outcome() == (PromptOutcome.ENABLE, False)
    assert requests == []


def test_dialog_later_close_and_escape_are_the_same_and_honor_suppression():
    from PyQt6.QtGui import QKeyEvent

    actions = (
        lambda dialog: dialog.later_button.click(),
        lambda dialog: dialog.close(),
        lambda dialog: dialog.keyPressEvent(
            QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
        ),
    )
    for close_dialog in actions:
        dialog = ErrorReportingConsentDialog()
        dialog.suppression_checkbox.setChecked(True)
        close_dialog(dialog)

        assert dialog.get_outcome() == (PromptOutcome.LATER, True)
        dialog.deleteLater()
