from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_build_docs_do_not_show_stale_windows_installer_version():
    doc = (PROJECT_ROOT / "BUILD.md").read_text(encoding="utf-8")

    assert "TunnelForge-Setup-1.0.0.exe" not in doc
    assert "AppVersion=1.0.0" not in doc
    assert "TunnelForge-Setup-{version}.exe" in doc
    assert "AppVersion={#MyAppVersion}" in doc


def test_contributing_drops_deleted_release_scripts_and_documents_tests():
    doc = (PROJECT_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

    assert "smart-release.ps1" not in doc
    assert "bump-version.ps1" not in doc
    assert "## Running Tests" in doc


def test_quick_start_uses_current_connection_flow_labels():
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    readme_ko = (PROJECT_ROOT / "README.ko.md").read_text(encoding="utf-8")

    assert "Add Tunnel" not in readme
    assert "터널 추가" not in readme_ko

    for label in (
        "Add Connection",
        "Name (alias)",
        "Endpoint",
        "DB Port",
        "SSH tunnel",
        "Direct connection",
        "Start",
        "SQL Editor",
        "Orphan Record Analysis",
        "Tools",
        "Schema Diff",
        "MySQL 8.4 Upgrade Check",
        "DB Transition",
    ):
        assert label in readme

    for label in (
        "연결 추가",
        "이름(별칭)",
        "Endpoint",
        "DB Port",
        "SSH 터널 (Bastion 경유)",
        "직접 연결 (로컬/외부 DB)",
        "시작",
        "SQL 에디터",
        "고아 레코드 분석",
        "도구",
        "스키마 비교",
        "MySQL 8.4 업그레이드 점검",
        "DB 전환",
    ):
        assert label in readme_ko
