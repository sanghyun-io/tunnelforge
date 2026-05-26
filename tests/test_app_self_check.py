import json
import subprocess

import main


def test_should_run_self_check_detects_flag():
    assert main.should_run_self_check(["TunnelForge", "--self-check"]) is True
    assert main.should_run_self_check(["TunnelForge", "--minimized"]) is False


def test_run_self_check_reports_resource_and_core_status(monkeypatch, tmp_path):
    icon = tmp_path / "icon.png"
    icon.write_text("icon", encoding="utf-8")
    core = tmp_path / "tunnelforge-core"
    core.write_text("core", encoding="utf-8")

    monkeypatch.setattr(main, "app_icon_path", lambda: icon)
    monkeypatch.setattr(main, "db_core_executable", lambda: str(core))

    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args=args[0],
            returncode=0,
            stdout=json.dumps({
                "event": "result",
                "request_id": "self-check",
                "service": "tunnelforge-core",
                "success": True,
            }),
            stderr="",
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    result = main.run_self_check()

    assert result["success"] is True
    assert result["icon_exists"] is True
    assert result["core_exists"] is True
    assert result["core_hello"]["service"] == "tunnelforge-core"
