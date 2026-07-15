import importlib.util
from pathlib import Path

import pytest


def _load_capture():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture-oneclick-real-execution-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_oneclick_real_execution_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_oneclick_real_capture_rejects_unsafe_seed_identifiers():
    capture = _load_capture()

    with pytest.raises(ValueError, match="unsafe One-Click schema"):
        capture._require_safe_oneclick_identifier("prod", "schema")

    with pytest.raises(ValueError, match="unsafe One-Click table"):
        capture._require_safe_oneclick_identifier("legacy_engine_table;DROP", "table")


def test_oneclick_real_capture_fails_closed_before_facade_or_db(monkeypatch):
    capture = _load_capture()
    facade_constructions = []
    expected_message = (
        "Phase A disables One-Click real-execution evidence capture; exact-plan approval "
        "and TF-STATUS-098 are required before DB mutation."
    )
    monkeypatch.setattr(capture, "ONECLICK_REAL_EXECUTION_ENABLED", True)
    monkeypatch.setattr(
        capture,
        "DbCoreFacade",
        lambda: facade_constructions.append(True),
    )

    with pytest.raises(capture.OneClickRealExecutionCaptureDisabled) as exc_info:
        capture.capture_oneclick_real_execution(
            host="127.0.0.1",
            port=3406,
            user="root",
            password="test",
            schema="tf_oneclick_real_execution",
            table="tf_oneclick_legacy_engine_table",
        )

    assert exc_info.value.code == "oneclick_apply_disabled"
    assert str(exc_info.value) == expected_message
    assert "exact-plan approval" in str(exc_info.value)
    assert "TF-STATUS-098" in str(exc_info.value)
    assert facade_constructions == []


def test_oneclick_real_capture_command_fails_before_seed_or_capture(
    monkeypatch,
    capsys,
    tmp_path,
):
    capture = _load_capture()
    calls = []
    expected_error = (
        "oneclick_apply_disabled: Phase A disables One-Click real-execution evidence "
        "capture; exact-plan approval and TF-STATUS-098 are required before DB mutation."
    )
    monkeypatch.setattr(
        capture,
        "seed_local_mysql_container",
        lambda **_kwargs: calls.append("seed"),
    )
    monkeypatch.setattr(
        capture,
        "capture_oneclick_real_execution",
        lambda **_kwargs: calls.append("capture") or {"must_not_be_used": True},
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "capture-oneclick-real-execution-evidence.py",
            "--seed-local-container",
            "--output",
            str(tmp_path / "must-not-exist.json"),
        ],
    )

    exit_code = capture.main()
    output = capsys.readouterr()

    assert exit_code == 2
    assert output.out == ""
    assert output.err.strip() == expected_error
    assert calls == []
    assert not (tmp_path / "must-not-exist.json").exists()
