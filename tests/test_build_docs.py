from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_docs_do_not_show_stale_windows_installer_version():
    doc = (PROJECT_ROOT / "BUILD.md").read_text(encoding="utf-8")

    assert "TunnelForge-Setup-1.0.0.exe" not in doc
    assert "AppVersion=1.0.0" not in doc
    assert "TunnelForge-Setup-{version}.exe" in doc
    assert "AppVersion={#MyAppVersion}" in doc
