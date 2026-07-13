import pytest

from PyQt6.QtWidgets import QMessageBox

from src.core.production_guard import ProductionGuard


@pytest.mark.parametrize(
    "tunnel_config",
    [{}, {"environment": None}, {"environment": "invalid"}],
)
def test_unknown_environment_requires_default_no_confirmation(monkeypatch, tunnel_config):
    captured = {}

    def fake_warning(parent, title, text, buttons, default_button):
        captured.update(
            parent=parent,
            title=title,
            text=text,
            buttons=buttons,
            default_button=default_button,
        )
        return QMessageBox.StandardButton.No

    monkeypatch.setattr("src.core.production_guard.QMessageBox.warning", fake_warning)

    result = ProductionGuard(parent=None).confirm_dangerous_operation(
        tunnel_config,
        "데이터 Import",
        "target_schema",
        "Import 상세 정보",
    )

    assert result is False
    assert captured["default_button"] == QMessageBox.StandardButton.No
    assert captured["buttons"] == (
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
    )
    assert "데이터 Import" in captured["text"]
    assert "target_schema" in captured["text"]
    assert "Import 상세 정보" in captured["text"]
    assert "미분류" in captured["title"]


def test_explicit_development_environment_remains_permissive(monkeypatch):
    message_box_calls = []

    def record_message_box(*args, **kwargs):
        message_box_calls.append((args, kwargs))
        return QMessageBox.StandardButton.No

    monkeypatch.setattr("src.core.production_guard.QMessageBox.warning", record_message_box)
    monkeypatch.setattr("src.core.production_guard.QMessageBox.question", record_message_box)

    result = ProductionGuard(parent=None).confirm_dangerous_operation(
        {"environment": "development"},
        "데이터 Import",
        "dev_schema",
    )

    assert result is True
    assert message_box_calls == []


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
