import plistlib
import subprocess

import pytest

from src.core import platform_integration


def test_detached_process_kwargs_are_empty_on_macos():
    assert platform_integration.detached_process_kwargs(platform_name="Darwin") == {}


def test_detached_process_kwargs_include_flags_on_windows():
    if not hasattr(subprocess, "DETACHED_PROCESS"):
        pytest.skip("Windows detached process flags are only available on Windows Python")

    kwargs = platform_integration.detached_process_kwargs(platform_name="Windows")

    assert kwargs["creationflags"] & getattr(subprocess, "DETACHED_PROCESS", 0)
    assert kwargs["creationflags"] & getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)


def test_startup_registration_creates_and_removes_macos_launch_agent(monkeypatch, tmp_path):
    executable = "/Applications/TunnelForge.app/Contents/MacOS/TunnelForge"
    monkeypatch.setattr(platform_integration.sys, "frozen", True, raising=False)
    registrar = platform_integration.StartupRegistrar(
        platform_name="Darwin",
        home=tmp_path,
        executable=executable,
    )

    assert registrar.is_supported is True
    assert registrar.is_registered() is False

    assert registrar.set_registered(True) == (True, "")
    assert registrar.is_registered() is True
    plist = tmp_path / "Library" / "LaunchAgents" / "io.sanghyun.tunnelforge.plist"
    content = plist.read_text(encoding="utf-8")
    assert "io.sanghyun.tunnelforge" in content
    assert executable in content
    assert "--minimized" in content
    launch_agent = plistlib.loads(plist.read_bytes())
    assert launch_agent["StandardOutPath"] == str(
        tmp_path / "Library" / "Logs" / "TunnelForge" / "launchagent.out.log"
    )
    assert launch_agent["StandardErrorPath"] == str(
        tmp_path / "Library" / "Logs" / "TunnelForge" / "launchagent.err.log"
    )
    assert launch_agent["WorkingDirectory"] == "/Applications/TunnelForge.app/Contents/MacOS"
    assert (tmp_path / "Library" / "Logs" / "TunnelForge").is_dir()

    assert registrar.set_registered(False) == (True, "")
    assert registrar.is_registered() is False


def test_macos_startup_registration_ignores_stale_launch_agent(monkeypatch, tmp_path):
    current_executable = "/Applications/TunnelForge.app/Contents/MacOS/TunnelForge"
    old_executable = "/Volumes/TunnelForge/TunnelForge.app/Contents/MacOS/TunnelForge"
    monkeypatch.setattr(platform_integration.sys, "frozen", True, raising=False)
    registrar = platform_integration.StartupRegistrar(
        platform_name="Darwin",
        home=tmp_path,
        executable=current_executable,
    )

    plist = tmp_path / "Library" / "LaunchAgents" / "io.sanghyun.tunnelforge.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(
        plistlib.dumps(
            {
                "Label": "io.sanghyun.tunnelforge",
                "ProgramArguments": [old_executable, "--minimized"],
                "RunAtLoad": True,
                "WorkingDirectory": "/Volumes/TunnelForge/TunnelForge.app/Contents/MacOS",
            }
        )
    )

    assert registrar.is_registered() is False

    assert registrar.set_registered(True) == (True, "")
    assert registrar.is_registered() is True
    launch_agent = plistlib.loads(plist.read_bytes())
    assert launch_agent["ProgramArguments"] == [current_executable, "--minimized"]
    assert launch_agent["WorkingDirectory"] == "/Applications/TunnelForge.app/Contents/MacOS"


def test_macos_startup_registration_ignores_malformed_launch_agent(monkeypatch, tmp_path):
    monkeypatch.setattr(platform_integration.sys, "frozen", True, raising=False)
    registrar = platform_integration.StartupRegistrar(
        platform_name="Darwin",
        home=tmp_path,
        executable="/Applications/TunnelForge.app/Contents/MacOS/TunnelForge",
    )

    plist = tmp_path / "Library" / "LaunchAgents" / "io.sanghyun.tunnelforge.plist"
    plist.parent.mkdir(parents=True)
    plist.write_text("not a plist", encoding="utf-8")

    assert registrar.is_registered() is False


def test_restore_window_to_front_is_noop_on_macos():
    assert platform_integration.restore_window_to_front(1234, platform_name="Darwin") is False


def test_update_package_launch_strategy_opens_macos_packages():
    assert platform_integration.update_package_launch_strategy("Darwin") == "open"
    assert platform_integration.update_package_launch_strategy("Windows") == "execute"
