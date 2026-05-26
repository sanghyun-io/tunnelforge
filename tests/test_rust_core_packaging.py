from pathlib import Path

import yaml


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
    assert "'LSMinimumSystemVersion': '13.0'" in spec


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
    assert "/usr/libexec/PlistBuddy" in script
    assert ":LSMinimumSystemVersion" in script
    assert 'Minimum macOS version must be 13.0' in script
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
    assert 'ditto "$APP_PATH" "$DMG_STAGING/TunnelForge.app"' in script
    assert 'cp -R "$APP_PATH" "$DMG_STAGING/"' not in script
    assert 'shasum -a 256 "$ZIP_PATH" > "$ZIP_PATH.sha256"' in script
    assert 'shasum -a 256 "$DMG_PATH" > "$DMG_PATH.sha256"' in script


def test_macos_release_validation_script_smokes_app_dmg_and_zip():
    script = (PROJECT_ROOT / "scripts" / "validate-macos-release.sh").read_text(encoding="utf-8")

    assert "This script must run on macOS." in script
    assert "Smoke testing source-run app" in script
    assert "python main.py --ui-smoke-check" in script
    assert "bash scripts/build-macos.sh" in script
    assert "bash scripts/package-macos.sh" in script
    assert 'test -f "$DMG_PATH.sha256"' in script
    assert 'test -f "$ZIP_PATH.sha256"' in script
    assert 'shasum -a 256 -c "$DMG_PATH.sha256"' in script
    assert 'shasum -a 256 -c "$ZIP_PATH.sha256"' in script
    assert "dist/TunnelForge.app/Contents/MacOS/TunnelForge" in script
    assert "build/dmg-smoke-mount" in script
    assert "build/install-smoke-mount" in script
    assert '"$DMG_SMOKE_MOUNT/TunnelForge.app/Contents/MacOS/TunnelForge"' in script
    assert "Smoke testing copied DMG install" in script
    assert 'ditto "$INSTALL_SMOKE_MOUNT/TunnelForge.app" "build/install-smoke/TunnelForge.app"' in script
    assert "build/install-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in script
    assert "build/zip-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in script
    assert "--ui-smoke-check" in script
    assert "window_title" in script
    assert "tunnelforge-core" in script
    assert "Manual validation still required" in script


def test_macos_manual_validation_report_script_records_remaining_gates():
    script = (PROJECT_ROOT / "scripts" / "macos-manual-validation-report.sh").read_text(encoding="utf-8")

    assert "This script must run on macOS." in script
    assert "MACOS_VALIDATION_REPORT" in script
    assert "MACOS_VALIDATION_SMOKE_LOG" in script
    assert "macos-release-smoke-${TIMESTAMP}.log" in script
    assert "bash scripts/validate-macos-release.sh" in script
    assert 'tee "$SMOKE_LOG_PATH"' in script
    assert "PIPESTATUS" in script
    assert "Smoke log:" in script
    assert "SSH tunnel" in script
    assert "MySQL" in script
    assert "PostgreSQL" in script
    assert "Export/Import" in script
    assert "Migration" in script
    assert "LaunchAgent" in script
    assert "Gatekeeper" in script
    assert "spctl" in script
    assert "codesign" in script
    assert "notarization" in script


def test_release_workflow_has_macos_app_job_and_assets():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "build-macos-app:" in workflow
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true" in workflow
    assert "macos-15-intel" in workflow
    assert "macos-14" in workflow
    assert "MACOS_PACKAGE_ARCH: ${{ matrix.arch }}" in workflow
    assert "scripts/build-macos.sh" in workflow
    assert "scripts/package-macos.sh" in workflow
    assert "rustc --version" in workflow
    assert "cargo --version" in workflow
    assert "dtolnay/rust-toolchain" not in workflow
    assert "tests/test_app_self_check.py" in workflow
    assert "tests/test_settings_update_actions.py" in workflow
    assert "Smoke source-run TunnelForge app" in workflow
    assert "python main.py --ui-smoke-check" in workflow
    assert "Smoke packaged TunnelForge app" in workflow
    assert "Smoke DMG package" in workflow
    assert "Smoke copied DMG install" in workflow
    assert "build/dmg-smoke-mount" in workflow
    assert "build/install-smoke-mount" in workflow
    assert 'ditto "$INSTALL_SMOKE_MOUNT/TunnelForge.app" "build/install-smoke/TunnelForge.app"' in workflow
    assert "build/install-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in workflow
    assert "Smoke ZIP package" in workflow
    assert "--ui-smoke-check" in workflow
    assert 'data["window_title"] == "TunnelForge"' in workflow
    assert 'data["core_hello"]["service"] == "tunnelforge-core"' in workflow
    assert "TunnelForge-macOS-${{ steps.get_version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.get_version.outputs.version }}-${{ matrix.arch }}.zip" in workflow
    assert "TunnelForge-macOS-${{ steps.get_version.outputs.version }}-${{ matrix.arch }}.dmg.sha256" in workflow
    assert "TunnelForge-macOS-${{ steps.get_version.outputs.version }}-${{ matrix.arch }}.zip.sha256" in workflow
    assert "macOS 앱 이미지" in workflow
    assert "macOS SHA-256 체크섬" in workflow
    assert "shasum -a 256 -c" in workflow
    assert r"3. 선택적으로 \`.sha256\` 파일로 다운로드를 검증" in workflow


def test_release_workflow_creates_release_after_all_platform_artifacts():
    workflow_path = PROJECT_ROOT / ".github" / "workflows" / "release.yml"
    workflow_text = workflow_path.read_text(encoding="utf-8")
    workflow = yaml.safe_load(workflow_text)
    jobs = workflow["jobs"]

    windows_job_text = workflow_text.split("  build-windows-installer:", 1)[1].split("\n  build-macos-app:", 1)[0]
    macos_job_text = workflow_text.split("  build-macos-app:", 1)[1].split("\n  create-release:", 1)[0]

    assert "create-release" in jobs
    assert jobs["create-release"]["needs"] == ["build-windows-installer", "build-macos-app"]
    assert "actions/download-artifact" in workflow_text
    assert "merge-multiple: true" in workflow_text
    assert "softprops/action-gh-release@v3" in workflow_text
    assert "Create GitHub Release" in workflow_text
    assert "release-artifacts/TunnelForge-Setup-*.exe" in workflow_text
    assert "release-artifacts/TunnelForge-WebSetup.exe" in workflow_text
    assert "release-artifacts/TunnelForge-macOS-*.dmg" in workflow_text
    assert "release-artifacts/TunnelForge-macOS-*.zip" in workflow_text
    assert "release-artifacts/TunnelForge-macOS-*.dmg.sha256" in workflow_text
    assert "release-artifacts/TunnelForge-macOS-*.zip.sha256" in workflow_text
    assert "actions/upload-artifact@v4" in windows_job_text
    assert "actions/upload-artifact@v4" in macos_job_text
    assert "softprops/action-gh-release@v3" not in windows_job_text
    assert "softprops/action-gh-release@v3" not in macos_job_text


def test_macos_validation_workflow_builds_pr_artifacts():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "macos-app.yml").read_text(encoding="utf-8")

    assert "name: macOS App Validation" in workflow
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true" in workflow
    assert "pull_request:" in workflow
    assert "workflow_dispatch:" in workflow
    assert '"scripts/validate-macos-release.sh"' in workflow
    assert '"scripts/macos-manual-validation-report.sh"' in workflow
    assert "tests/test_app_self_check.py" in workflow
    assert "tests/test_settings_update_actions.py" in workflow
    assert "macos-14" in workflow
    assert "macos-15-intel" in workflow
    assert "MACOS_PACKAGE_ARCH: ${{ matrix.arch }}" in workflow
    assert "Smoke source-run TunnelForge app" in workflow
    assert "python main.py --ui-smoke-check" in workflow
    assert 'data["self_check"]["core_hello"]["service"] == "tunnelforge-core"' in workflow
    assert "bash scripts/build-macos.sh" in workflow
    assert "rustc --version" in workflow
    assert "cargo --version" in workflow
    assert "dtolnay/rust-toolchain" not in workflow
    assert "Smoke packaged TunnelForge app" in workflow
    assert 'APP_EXECUTABLE="dist/TunnelForge.app/Contents/MacOS/TunnelForge"' in workflow
    assert "--ui-smoke-check" in workflow
    assert 'data["window_title"] == "TunnelForge"' in workflow
    assert 'data["core_hello"]["service"] == "tunnelforge-core"' in workflow
    assert "bash scripts/package-macos.sh" in workflow
    assert "Smoke DMG package" in workflow
    assert "hdiutil attach" in workflow
    assert "build/dmg-smoke-mount" in workflow
    assert "build/install-smoke-mount" in workflow
    assert '"$DMG_SMOKE_MOUNT/TunnelForge.app/Contents/MacOS/TunnelForge"' in workflow
    assert "Smoke copied DMG install" in workflow
    assert 'ditto "$INSTALL_SMOKE_MOUNT/TunnelForge.app" "build/install-smoke/TunnelForge.app"' in workflow
    assert "build/install-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in workflow
    assert "Smoke ZIP package" in workflow
    assert "ditto -x -k" in workflow
    assert "build/zip-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in workflow
    assert "actions/upload-artifact" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg.sha256" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.zip.sha256" in workflow


def test_version_gate_runs_macos_validation_from_existing_pr_workflow():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "version-gate.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)
    jobs = parsed["jobs"]

    assert "macos-app-validation" in jobs
    assert "version-bump" in jobs
    assert jobs["macos-app-validation"]["strategy"]["fail-fast"] is False
    assert jobs["version-bump"]["needs"] == "version-gate"
    version_gate_text = workflow.split("  version-gate:", 1)[1].split("\n  version-bump:", 1)[0]
    assert "actions/create-github-app-token" not in version_gate_text
    assert "token: \"\"" in workflow
    assert "persist-credentials: false" in workflow
    assert "macos-14" in workflow
    assert "macos-15-intel" in workflow
    assert "MACOS_PACKAGE_ARCH: ${{ matrix.arch }}" in workflow
    assert "Run focused macOS tests" in workflow
    assert "Smoke source-run TunnelForge app" in workflow
    assert "python main.py --ui-smoke-check" in workflow
    assert "bash scripts/build-macos.sh" in workflow
    assert "rustc --version" in workflow
    assert "cargo --version" in workflow
    assert "dtolnay/rust-toolchain" not in workflow
    assert "Smoke packaged TunnelForge app" in workflow
    assert "bash scripts/package-macos.sh" in workflow
    assert "Smoke DMG package" in workflow
    assert "Smoke copied DMG install" in workflow
    assert "Smoke ZIP package" in workflow
    assert "Upload macOS validation artifacts" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg.sha256" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.zip.sha256" in workflow
