from pathlib import Path

from src.core import platform_paths


def test_app_support_dir_uses_macos_application_support(tmp_path):
    result = platform_paths.app_support_dir(
        platform_name="Darwin",
        home=tmp_path,
        environ={},
    )

    assert result == tmp_path / "Library" / "Application Support" / "TunnelForge"


def test_log_dir_uses_macos_library_logs(tmp_path):
    result = platform_paths.log_dir(
        platform_name="Darwin",
        home=tmp_path,
        environ={},
    )

    assert result == tmp_path / "Library" / "Logs" / "TunnelForge"


def test_app_support_dir_uses_windows_localappdata(tmp_path):
    result = platform_paths.app_support_dir(
        platform_name="Windows",
        home=Path("C:/Users/Test"),
        environ={"LOCALAPPDATA": str(tmp_path)},
    )

    assert result == tmp_path / "TunnelForge"


def test_config_file_uses_app_support_dir(tmp_path):
    result = platform_paths.config_file(
        platform_name="Darwin",
        home=tmp_path,
        environ={},
    )

    assert result == tmp_path / "Library" / "Application Support" / "TunnelForge" / "config.json"


def test_ssh_host_trust_file_uses_app_support_dir(tmp_path):
    result = platform_paths.ssh_host_trust_file(
        platform_name="Linux",
        home=tmp_path,
        environ={},
    )

    assert result == tmp_path / ".config" / "tunnelforge" / "ssh_host_trust.json"
