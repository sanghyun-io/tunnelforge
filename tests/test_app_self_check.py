import json
import subprocess

import main


def test_should_run_self_check_detects_flag():
    assert main.should_run_self_check(["TunnelForge", "--self-check"]) is True
    assert main.should_run_self_check(["TunnelForge", "--minimized"]) is False


def test_should_run_ui_smoke_check_detects_flag():
    assert main.should_run_ui_smoke_check(["TunnelForge", "--ui-smoke-check"]) is True
    assert main.should_run_ui_smoke_check(["TunnelForge", "--self-check"]) is False


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


def test_run_ui_smoke_check_builds_window_without_background(monkeypatch):
    created = {}

    class FakeApp:
        @staticmethod
        def instance():
            return None

        def __init__(self, argv):
            created["argv"] = argv

        def setWindowIcon(self, icon):
            created["icon"] = icon

        def quit(self):
            created["quit"] = True

    class FakeIcon:
        def __init__(self, path):
            created["icon_path"] = path

    class FakeConfigManager:
        pass

    class FakeTunnelEngine:
        pass

    class FakeWindow:
        def __init__(self, config_manager, tunnel_engine, start_background=True):
            created["start_background"] = start_background

        def windowTitle(self):
            return "TunnelForge"

        def dispose_for_smoke_check(self):
            created["disposed"] = True

    monkeypatch.setattr(main, "QApplication", lambda: FakeApp)
    monkeypatch.setattr(main, "QIcon", lambda: FakeIcon)
    monkeypatch.setattr(main, "ConfigManager", lambda: FakeConfigManager)
    monkeypatch.setattr(main, "TunnelEngine", lambda: FakeTunnelEngine)
    monkeypatch.setattr(main, "TunnelManagerUI", lambda: FakeWindow)
    monkeypatch.setattr(main, "app_icon_path", lambda: "icon")
    monkeypatch.setattr(main, "run_self_check", lambda: {"success": True})

    result = main.run_ui_smoke_check()

    assert result["success"] is True
    assert result["window_title"] == "TunnelForge"
    assert result["self_check"]["success"] is True
    assert created["start_background"] is False
    assert created["disposed"] is True


def test_ui_smoke_cli_writes_single_json_line_to_stdout(monkeypatch, capsys):
    monkeypatch.setattr(
        main,
        "run_ui_smoke_check",
        lambda: print("internal log") or {"success": True, "window_title": "TunnelForge"},
    )

    exit_code = main.run_ui_smoke_check_cli()

    captured = capsys.readouterr()
    assert exit_code == 0
    assert captured.err == "internal log\n"
    assert json.loads(captured.out)["success"] is True
