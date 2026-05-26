from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_pyinstaller_spec_includes_core_service_binaries_cross_platform():
    spec = (PROJECT_ROOT / "tunnel-manager.spec").read_text(encoding="utf-8")

    assert "core_suffix = '.exe' if os.name == 'nt' else ''" in spec
    assert "tunnelforge-core{core_suffix}" in spec
    assert "binaries=binaries" in spec
    assert "app_bundle = BUNDLE(" in spec
    assert "coll = COLLECT(" in spec
    assert "exclude_binaries=os.name != 'nt'" in spec
    assert "assets/icon.icns" in spec
    assert "if os.name == 'nt' else app_bundle" in spec
    assert "from src.version import __version__" in spec
    assert "'CFBundleShortVersionString': __version__" in spec


def test_windows_installer_builds_and_checks_core_service_binaries():
    script = (PROJECT_ROOT / "scripts" / "build-installer.ps1").read_text(encoding="utf-8")

    assert "cargo build --manifest-path migration_core\\Cargo.toml --release" in script
    assert "migration_core\\target\\release\\tunnelforge-core.exe" in script
    assert "tunnelforge-core DB service 빌드 완료" in script


def test_macos_build_script_builds_core_and_pyinstaller_app():
    script = (PROJECT_ROOT / "scripts" / "build-macos.sh").read_text(encoding="utf-8")

    assert 'export MACOSX_DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"' in script
    assert "cargo build --manifest-path migration_core/Cargo.toml --release" in script
    assert "python -m PyInstaller tunnel-manager.spec --noconfirm" in script
    assert "dist/TunnelForge.app" in script
    assert "tunnelforge-core" in script
    assert "icon_512x512@2x.png" not in script


def test_macos_package_script_creates_dmg_and_supports_signing_notarization():
    script = (PROJECT_ROOT / "scripts" / "package-macos.sh").read_text(encoding="utf-8")

    assert "hdiutil create" in script
    assert "ARCH_NAME=\"${MACOS_PACKAGE_ARCH:-$(uname -m)}\"" in script
    assert "HOST_ARCH=\"$(uname -m)\"" in script
    assert "does not match requested package architecture" in script
    assert "TunnelForge-macOS-${VERSION}-${ARCH_NAME}.dmg" in script
    assert "APPLE_CODESIGN_IDENTITY" in script
    assert "notarytool submit" in script
    assert "stapler staple" in script


def test_release_workflow_has_macos_app_job_and_assets():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "build-macos-app:" in workflow
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true" in workflow
    assert "macos-15-intel" in workflow
    assert "macos-14" in workflow
    assert "MACOS_PACKAGE_ARCH: ${{ matrix.arch }}" in workflow
    assert "scripts/build-macos.sh" in workflow
    assert "scripts/package-macos.sh" in workflow
    assert "tests/test_app_self_check.py" in workflow
    assert "tests/test_settings_update_actions.py" in workflow
    assert "Smoke packaged TunnelForge app" in workflow
    assert "Smoke DMG package" in workflow
    assert "Smoke ZIP package" in workflow
    assert "--ui-smoke-check" in workflow
    assert 'data["window_title"] == "TunnelForge"' in workflow
    assert 'data["core_hello"]["service"] == "tunnelforge-core"' in workflow
    assert "TunnelForge-macOS-${{ steps.get_version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.get_version.outputs.version }}-${{ matrix.arch }}.zip" in workflow
    assert "macOS 앱 이미지" in workflow


def test_macos_validation_workflow_builds_pr_artifacts():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "macos-app.yml").read_text(encoding="utf-8")

    assert "name: macOS App Validation" in workflow
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert "tests/test_app_self_check.py" in workflow
    assert "tests/test_settings_update_actions.py" in workflow
    assert "macos-14" in workflow
    assert "macos-15-intel" in workflow
    assert "MACOS_PACKAGE_ARCH: ${{ matrix.arch }}" in workflow
    assert "bash scripts/build-macos.sh" in workflow
    assert "Smoke packaged TunnelForge app" in workflow
    assert 'APP_EXECUTABLE="dist/TunnelForge.app/Contents/MacOS/TunnelForge"' in workflow
    assert "--ui-smoke-check" in workflow
    assert 'data["window_title"] == "TunnelForge"' in workflow
    assert 'data["core_hello"]["service"] == "tunnelforge-core"' in workflow
    assert "bash scripts/package-macos.sh" in workflow
    assert "Smoke DMG package" in workflow
    assert "hdiutil attach" in workflow
    assert "/Volumes/TunnelForge/TunnelForge.app/Contents/MacOS/TunnelForge" in workflow
    assert "Smoke ZIP package" in workflow
    assert "ditto -x -k" in workflow
    assert "build/zip-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in workflow
    assert "actions/upload-artifact" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
