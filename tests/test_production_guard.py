from PyQt6.QtWidgets import QMessageBox

from src.core.production_guard import ProductionGuard


def test_staging_confirmation_includes_operation_and_schema_when_details_empty(monkeypatch):
    captured = {}

    def fake_warning(parent, title, text, buttons, default_button):
        captured["parent"] = parent
        captured["title"] = title
        captured["text"] = text
        captured["buttons"] = buttons
        captured["default_button"] = default_button
        return QMessageBox.StandardButton.Yes

    monkeypatch.setattr("src.core.production_guard.QMessageBox.warning", fake_warning)

    guard = ProductionGuard(parent=None)

    result = guard.confirm_dangerous_operation(
        {"environment": "staging"},
        "데이터 Import",
        "prod_db",
    )

    assert result is True
    assert captured["title"] == "🟠 STAGING 환경 - 데이터 Import"
    assert "<b>데이터 Import</b> 작업을 실행하시겠습니까?" in captured["text"]
    assert "스키마: <b>prod_db</b>" in captured["text"]
    assert captured["text"] != ""
    assert captured["buttons"] == (
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    assert captured["default_button"] == QMessageBox.StandardButton.No


def test_staging_confirmation_appends_details_when_present(monkeypatch):
    captured = {}

    def fake_warning(parent, title, text, buttons, default_button):
        captured["text"] = text
        return QMessageBox.StandardButton.No

    monkeypatch.setattr("src.core.production_guard.QMessageBox.warning", fake_warning)

    guard = ProductionGuard(parent=None)

    result = guard.confirm_dangerous_operation(
        {"environment": "staging"},
        "데이터 Import",
        "prod_db",
        "<p>추가 상세</p>",
    )

    assert result is False
    assert "<b>데이터 Import</b> 작업을 실행하시겠습니까?" in captured["text"]
    assert "스키마: <b>prod_db</b>" in captured["text"]
    assert captured["text"].endswith("<p>추가 상세</p>")
