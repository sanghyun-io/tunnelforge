from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pyinstaller_spec_includes_core_service_binaries_cross_platform():
    spec = (PROJECT_ROOT / "tunnel-manager.spec").read_text(encoding="utf-8")

    assert "core_suffix = '.exe' if os.name == 'nt' else ''" in spec
    assert "tunnelforge-core{core_suffix}" in spec
    assert "binaries=binaries" in spec


def test_windows_installer_builds_and_checks_core_service_binaries():
    script = (PROJECT_ROOT / "scripts" / "build-installer.ps1").read_text(encoding="utf-8")

    assert "cargo build --manifest-path migration_core\\Cargo.toml --release" in script
    assert "migration_core\\target\\release\\tunnelforge-core.exe" in script
    assert "tunnelforge-core DB service 빌드 완료" in script
