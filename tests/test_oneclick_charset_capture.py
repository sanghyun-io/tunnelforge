import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


DISABLED_MESSAGE = (
    "Phase A disables One-Click charset mutation evidence capture; exact-plan approval "
    "and TF-STATUS-098 are required before DB mutation."
)


def _write_runtime_dependency_blocker(tmp_path):
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        """import builtins

_original_import = builtins.__import__
_blocked = {
    "src.core.db_core_service",
    "src.ui.dialogs.migration_dialogs",
    "src.ui.dialogs.oneclick_migration_dialog",
}

def _blocking_import(name, *args, **kwargs):
    if name in _blocked:
        raise RuntimeError(f"runtime dependency imported before Phase A gate: {name}")
    return _original_import(name, *args, **kwargs)

builtins.__import__ = _blocking_import
""",
        encoding="utf-8",
    )


def _load_capture():
    script = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "capture-oneclick-charset-evidence.py"
    )
    spec = importlib.util.spec_from_file_location("capture_oneclick_charset_evidence", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_oneclick_charset_capture_rejects_unsafe_seed_identifiers():
    capture = _load_capture()

    with pytest.raises(ValueError, match="unsafe One-Click schema"):
        capture._require_safe_oneclick_identifier("prod", "schema")

    with pytest.raises(ValueError, match="unsafe One-Click table"):
        capture._require_safe_oneclick_identifier("tf_oneclick_child;DROP", "table")


def test_oneclick_charset_capture_fails_before_scope_facade_or_endpoint(monkeypatch):
    capture = _load_capture()
    constructions = []
    monkeypatch.setattr(
        capture,
        "_load_db_core_types",
        lambda: constructions.append("runtime dependencies"),
    )

    with pytest.raises(capture.OneClickCharsetCaptureDisabled) as exc_info:
        capture.capture_oneclick_charset(
            host="127.0.0.1",
            port=3406,
            user="root",
            password="test",
            schema="prod",
            tables=["tf_oneclick_parent", "tf_oneclick_child"],
        )

    assert exc_info.value.code == "oneclick_apply_disabled"
    assert str(exc_info.value) == DISABLED_MESSAGE
    assert constructions == []


def test_oneclick_charset_capture_cli_fails_before_seed_capture_or_output(
    monkeypatch,
    capsys,
    tmp_path,
):
    capture = _load_capture()
    calls = []
    output_path = tmp_path / "must-not-exist.json"
    monkeypatch.setattr(
        capture,
        "seed_local_mysql_container",
        lambda **_kwargs: calls.append("seed"),
    )
    monkeypatch.setattr(
        capture,
        "capture_oneclick_charset",
        lambda **_kwargs: calls.append("capture") or {"must_not_be_used": True},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture-oneclick-charset-evidence.py",
            "--seed-local-container",
            "--output",
            str(output_path),
        ],
    )

    assert capture.main() == 2

    output = capsys.readouterr()
    assert output.out == ""
    assert output.err.strip() == f"oneclick_apply_disabled: {DISABLED_MESSAGE}"
    assert calls == []
    assert not output_path.exists()


def test_oneclick_charset_capture_cli_gates_before_runtime_dependency_imports(tmp_path):
    _write_runtime_dependency_blocker(tmp_path)
    project_root = Path(__file__).resolve().parents[1]
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        value for value in [str(tmp_path), env.get("PYTHONPATH", "")] if value
    )

    result = subprocess.run(
        [sys.executable, "scripts/capture-oneclick-charset-evidence.py"],
        cwd=project_root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr.strip() == f"oneclick_apply_disabled: {DISABLED_MESSAGE}"
