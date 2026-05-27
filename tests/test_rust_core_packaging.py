from pathlib import Path
import re


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pyinstaller_spec_includes_core_service_binaries_cross_platform():
    spec = (PROJECT_ROOT / "tunnel-manager.spec").read_text(encoding="utf-8")

    assert "core_suffix = '.exe' if os.name == 'nt' else ''" in spec
    assert "tunnelforge-core{core_suffix}" in spec
    assert "binaries=binaries" in spec
    assert "raise SystemExit" in spec
    assert "Run `cargo build --manifest-path migration_core/Cargo.toml --release` first." in spec


def test_windows_installer_builds_and_checks_core_service_binaries():
    script = (PROJECT_ROOT / "scripts" / "build-installer.ps1").read_text(encoding="utf-8")

    assert "cargo build --manifest-path migration_core\\Cargo.toml --release" in script
    assert "migration_core\\target\\release\\tunnelforge-core.exe" in script
    assert "tunnelforge-core DB service 빌드 완료" in script


def test_release_workflow_builds_core_before_pyinstaller():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    core_build_index = workflow.index("Build tunnelforge-core (Rust)")
    pyinstaller_index = workflow.index("Build with PyInstaller")

    assert core_build_index < pyinstaller_index
    assert "uses: dtolnay/rust-toolchain@stable" in workflow
    assert "cargo build --manifest-path migration_core\\Cargo.toml --release" in workflow
    assert "migration_core\\target\\release\\tunnelforge-core.exe" in workflow


def test_release_version_files_are_in_sync():
    version_py = (PROJECT_ROOT / "src" / "version.py").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    installer = (PROJECT_ROOT / "installer" / "TunnelForge.iss").read_text(encoding="utf-8")

    app_version = re.search(r'__version__\s*=\s*"([^"]+)"', version_py).group(1)
    package_version = re.search(r'^version\s*=\s*"([^"]+)"', pyproject, re.MULTILINE).group(1)
    installer_version = re.search(r'#define MyAppVersion "([^"]+)"', installer).group(1)

    assert package_version == app_version
    assert installer_version == app_version
