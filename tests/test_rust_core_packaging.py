import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tomllib
import zipfile

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def evidence_manifest(report: Path, smoke_log: Path) -> str:
    return (
        f"{hashlib.sha256(report.read_bytes()).hexdigest()}  {report.name}\n"
        f"{hashlib.sha256(smoke_log.read_bytes()).hexdigest()}  {smoke_log.name}\n"
    )


def write_bundle_checksum(bundle: Path) -> Path:
    checksum = bundle.with_name(f"{bundle.name}.sha256")
    checksum.write_text(f"{hashlib.sha256(bundle.read_bytes()).hexdigest()}  {bundle.name}\n", encoding="utf-8")
    return checksum


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


def test_dev_dependencies_include_yaml_parser_for_workflow_tests():
    pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev_dependencies = pyproject["project"]["optional-dependencies"]["dev"]

    assert any(dependency.lower().startswith("pyyaml") for dependency in dev_dependencies)


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
    assert "notarization_env_count" in script
    assert "Apple notarization credentials are incomplete" in script
    assert "APPLE_CODESIGN_IDENTITY is required before notarization" in script
    assert "submit_for_notarization" in script
    assert "notarize_stapled_app_for_zip_distribution" in script
    assert "build/macos-notarization" in script
    assert 'ditto -c -k --keepParent "$APP_PATH" "$APP_NOTARIZATION_ZIP"' in script
    assert 'xcrun stapler staple "$APP_PATH"' in script
    assert 'xcrun stapler validate "$APP_PATH"' in script
    assert 'xcrun stapler validate "$DMG_PATH"' in script
    assert "create_dmg_with_retry" in script
    assert "hdiutil create failed" in script
    assert "hdiutil info || true" in script
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
    assert "MACOS_VALIDATION_APP_PATH" in script
    assert "macos-release-smoke-${TIMESTAMP}.log" in script
    assert "bash scripts/validate-macos-release.sh" in script
    assert "--check-complete <report>" in script
    assert "--bundle-evidence <report>" in script
    assert "--finalize <report>" in script
    assert "--skip-github" in script
    assert "--evidence-bundle <zip>" in script
    assert "MACOS_VALIDATION_EVIDENCE_BUNDLE" in script
    assert "Evidence bundle:" in script
    assert "PYTHON_BIN" in script
    assert "hashlib.sha256" in script
    assert "manifest_name" in script
    assert "checksum_path_for_bundle" in script
    assert "Evidence bundle checksum:" in script
    assert "check_complete_report" in script
    assert "create_evidence_bundle" in script
    assert "finalize_evidence" in script
    assert "scripts/check-macos-support-gate.py" in script
    assert "Final macOS validation evidence is ready" in script
    assert "Manual validation report still has unchecked items" in script
    assert "^[[:space:]]*- \\[ \\]" in script
    assert "^- Release smoke: passed[[:space:]]*$" in script
    assert "^- Overall result: (pass|passed|PASS|PASSED)[[:space:]]*$" in script
    assert "Manual validation report must include a validator name" in script
    assert "^- Validator:[[:space:]]*[^[:space:]].*$" in script
    assert "Smoke log is missing or empty" in script
    assert "macOS release package smoke checks passed." in script
    assert 'tee "$SMOKE_LOG_PATH"' in script
    assert "PIPESTATUS" in script
    assert "Smoke log:" in script
    assert "Final app path:" in script
    assert "Completion check:" in script
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


def test_macos_launchagent_smoke_script_validates_packaged_app_plist():
    script = (PROJECT_ROOT / "scripts" / "smoke-macos-launchagent.sh").read_text(encoding="utf-8")

    assert "This script must run on macOS." in script
    assert "build/install-smoke/TunnelForge.app" in script
    assert "StartupRegistrar" in script
    assert "sys.frozen = True" in script
    assert "io.sanghyun.tunnelforge" in script
    assert "ProgramArguments" in script
    assert "launchagent.out.log" in script
    assert "launchagent.err.log" in script
    assert "plutil -lint" in script
    assert "MACOS_LAUNCHAGENT_BOOTSTRAP" in script
    assert "launchctl bootstrap" in script
    assert "launchctl bootout" in script
    assert "macOS LaunchAgent smoke checks passed." in script


def test_macos_manual_validation_report_check_complete_accepts_completed_report(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-complete"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
                "- [x] Run `bash scripts/validate-macos-release.sh`",
                "- Overall result: passed",
                "- Validator: Codex",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/macos-manual-validation-report.sh",
            "--check-complete",
            report_arg,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "Manual validation report is complete" in result.stdout


def test_macos_manual_validation_report_check_complete_rejects_missing_smoke_log(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-missing"
    report_dir.mkdir(parents=True, exist_ok=True)
    report = report_dir / "macos-manual-validation-report.md"
    missing_log_arg = (report_dir / "missing.log").relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {missing_log_arg}",
                "- [x] Run `bash scripts/validate-macos-release.sh`",
                "- Overall result: passed",
                "- Validator: Codex",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/macos-manual-validation-report.sh",
            "--check-complete",
            report_arg,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "Smoke log is missing or empty" in result.stderr


def test_macos_manual_validation_report_bundle_evidence_creates_zip(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-bundle"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
                "- [x] Run `bash scripts/validate-macos-release.sh`",
                "- Overall result: passed",
                "- Validator: Codex",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/macos-manual-validation-report.sh",
            "--bundle-evidence",
            report_arg,
            "--evidence-bundle",
            bundle.relative_to(PROJECT_ROOT).as_posix(),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert bundle.exists()
    checksum = bundle.with_name(f"{bundle.name}.sha256")
    assert checksum.exists()
    assert checksum.read_text(encoding="utf-8") == (
        f"{hashlib.sha256(bundle.read_bytes()).hexdigest()}  {bundle.name}\n"
    )
    with zipfile.ZipFile(bundle) as archive:
        manifest_name = "macos-manual-validation-evidence-macos-manual-validation-report.sha256"
        assert sorted(archive.namelist()) == [
            manifest_name,
            "macos-manual-validation-report.md",
            "macos-release-smoke.log",
        ]
        assert archive.read(manifest_name).decode("utf-8") == evidence_manifest(report, smoke_log)


def test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-finalize"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
                "- [x] Run `bash scripts/validate-macos-release.sh`",
                "- Overall result: passed",
                "- Validator: Codex",
                "",
            ]
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/macos-manual-validation-report.sh",
            "--finalize",
            report_arg,
            "--evidence-bundle",
            bundle_arg,
            "--skip-github",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert bundle.exists()
    checksum = bundle.with_name(f"{bundle.name}.sha256")
    assert checksum.exists()
    with zipfile.ZipFile(bundle) as archive:
        assert "macos-manual-validation-evidence-macos-manual-validation-report.sha256" in archive.namelist()
    assert "macOS support gate checks passed" in result.stdout
    assert "Final macOS validation evidence is ready" in result.stdout
    assert f"- Report: {report_arg}" in result.stdout
    assert f"- Smoke log: {smoke_log_arg}" in result.stdout
    assert f"- Evidence bundle: {bundle_arg}" in result.stdout
    assert f"- Evidence bundle checksum: {bundle_arg}.sha256" in result.stdout


def test_macos_support_gate_script_checks_github_tracking_and_final_report():
    script = (PROJECT_ROOT / "scripts" / "check-macos-support-gate.py").read_text(encoding="utf-8")

    assert "MILESTONE_ISSUES" in script
    assert "110: \"CLOSED\"" in script
    assert "115: \"CLOSED\"" in script
    assert "FINAL_ISSUE = 116" in script
    assert "PR_NUMBER = 117" in script
    assert "macOS Support M6" in script
    assert "--final" in script
    assert "--report" in script
    assert "--bundle" in script
    assert "--skip-github" in script
    assert "--skip-pr-checks" in script
    assert "zipfile" in script
    assert "hashlib" in script
    assert "check_evidence_bundle" in script
    assert "evidence_manifest_name" in script
    assert "evidence_bundle_checksum_path" in script
    assert "evidence bundle is complete with manifest and checksum" in script
    assert "glob.has_magic" in script
    assert "selected newest" in script
    assert "PR merge state skipped by request" in script
    assert "def bash_path" in script
    assert "scripts/macos-manual-validation-report.sh" in script
    assert "statusCheckRollup" in script
    assert "mergeStateStatus" in script
    assert "check_manual_macos_validation_workflow" in script
    assert "workflow_dispatch" in script
    assert "macOS App Validation" in script
    assert "Verify signed and notarized artifacts" in script
    assert "manual macOS signing/notarization workflow passed" in script


def test_macos_support_gate_script_accepts_local_final_report(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-gate-complete"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
                "- [x] Run `bash scripts/validate-macos-release.sh`",
                "- Overall result: passed",
                "- Validator: Codex",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(report, report.name)
        archive.write(smoke_log, smoke_log.name)
        archive.writestr(
            "macos-manual-validation-evidence-macos-manual-validation-report.sha256",
            evidence_manifest(report, smoke_log),
        )
    write_bundle_checksum(bundle)

    result = subprocess.run(
        [
            "python",
            "scripts/check-macos-support-gate.py",
            "--final",
            "--skip-github",
            "--report",
            report_arg,
            "--bundle",
            bundle_arg,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "macOS support gate checks passed" in result.stdout


def test_macos_support_gate_script_accepts_globbed_final_report_and_bundle(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-gate-glob"
    report_dir.mkdir(parents=True, exist_ok=True)

    def write_completed_evidence(stamp: str, mtime: int) -> tuple[Path, Path]:
        smoke_log = report_dir / f"macos-release-smoke-{stamp}.log"
        smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
        report = report_dir / f"macos-manual-validation-report-{stamp}.md"
        bundle = report_dir / f"macos-manual-validation-evidence-macos-manual-validation-report-{stamp}.zip"
        smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
        report.write_text(
            "\n".join(
                [
                    "- Release smoke: passed",
                    f"- Smoke log: {smoke_log_arg}",
                    "- [x] Run `bash scripts/validate-macos-release.sh`",
                    "- Overall result: passed",
                    "- Validator: Codex",
                    "",
                ]
            ),
            encoding="utf-8",
        )
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.write(report, report.name)
            archive.write(smoke_log, smoke_log.name)
            archive.writestr(
                f"macos-manual-validation-evidence-{report.stem}.sha256",
                evidence_manifest(report, smoke_log),
            )
        write_bundle_checksum(bundle)
        os.utime(report, (mtime, mtime))
        os.utime(bundle, (mtime, mtime))
        os.utime(bundle.with_name(f"{bundle.name}.sha256"), (mtime, mtime))
        return report, bundle

    write_completed_evidence("20260101T000000Z", 100)
    write_completed_evidence("20260102T000000Z", 200)

    report_pattern = (report_dir / "macos-manual-validation-report-*.md").relative_to(PROJECT_ROOT).as_posix()
    bundle_pattern = (report_dir / "macos-manual-validation-evidence-*.zip").relative_to(PROJECT_ROOT).as_posix()
    result = subprocess.run(
        [
            "python",
            "scripts/check-macos-support-gate.py",
            "--final",
            "--skip-github",
            "--report",
            report_pattern,
            "--bundle",
            bundle_pattern,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "selected newest manual validation report" in result.stdout
    assert "selected newest evidence bundle" in result.stdout
    assert "macOS support gate checks passed" in result.stdout


def test_macos_support_gate_script_rejects_missing_final_report():
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    result = subprocess.run(
        [
            "python",
            "scripts/check-macos-support-gate.py",
            "--final",
            "--skip-github",
            "--report",
            "build/does-not-exist.md",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "manual validation report is incomplete" in result.stderr


def test_macos_support_gate_script_rejects_incomplete_evidence_bundle(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-gate-bad-bundle"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
                "- [x] Run `bash scripts/validate-macos-release.sh`",
                "- Overall result: passed",
                "- Validator: Codex",
                "",
            ]
        ),
        encoding="utf-8",
    )
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(report, report.name)
    write_bundle_checksum(bundle)

    result = subprocess.run(
        [
            "python",
            "scripts/check-macos-support-gate.py",
            "--final",
            "--skip-github",
            "--report",
            report_arg,
            "--bundle",
            bundle_arg,
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "evidence bundle must contain exactly" in result.stderr


def test_release_workflow_has_macos_app_job_and_assets():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    assert "build-macos-app:" in workflow
    assert "FORCE_JAVASCRIPT_ACTIONS_TO_NODE24: true" in workflow
    assert "macos-15-intel" in workflow
    assert "macos-14" in workflow
    assert "MACOS_PACKAGE_ARCH: ${{ matrix.arch }}" in workflow
    assert "Import Apple Developer ID certificate" in workflow
    assert "APPLE_CODESIGN_CERTIFICATE_P12_BASE64" in workflow
    assert "APPLE_CODESIGN_CERTIFICATE_PASSWORD" in workflow
    assert "APPLE_CODESIGN_IDENTITY" in workflow
    assert "security create-keychain" in workflow
    assert "security import build/apple-codesign.p12" in workflow
    assert "security set-key-partition-list" in workflow
    assert "APPLE_ID: ${{ secrets.APPLE_ID }}" in workflow
    assert "APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}" in workflow
    assert "APPLE_APP_SPECIFIC_PASSWORD: ${{ secrets.APPLE_APP_SPECIFIC_PASSWORD }}" in workflow
    assert "Cleanup Apple signing keychain" in workflow
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
    assert "Smoke LaunchAgent registration" in workflow
    assert "bash scripts/smoke-macos-launchagent.sh build/install-smoke/TunnelForge.app" in workflow
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
    assert "Smoke LaunchAgent registration" in workflow
    assert "bash scripts/smoke-macos-launchagent.sh build/install-smoke/TunnelForge.app" in workflow
    assert "Smoke ZIP package" in workflow
    assert "ditto -x -k" in workflow
    assert "build/zip-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in workflow
    assert "actions/upload-artifact" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg.sha256" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.zip.sha256" in workflow


def test_macos_validation_workflow_supports_manual_signed_notarized_run():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "macos-app.yml").read_text(encoding="utf-8")

    assert "workflow_dispatch:" in workflow
    assert "CHECKOUT_REF: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    assert 'git fetch --depth=1 origin "$CHECKOUT_REF"' in workflow
    assert "Import Apple Developer ID certificate" in workflow
    assert "github.event_name == 'workflow_dispatch'" in workflow
    assert "APPLE_CODESIGN_CERTIFICATE_P12_BASE64" in workflow
    assert "security create-keychain" in workflow
    assert "security import build/apple-codesign.p12" in workflow
    assert "security set-key-partition-list" in workflow
    assert "APPLE_CODESIGN_IDENTITY=$APPLE_CODESIGN_IDENTITY" in workflow
    assert "APPLE_ID: ${{ secrets.APPLE_ID }}" in workflow
    assert "APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}" in workflow
    assert "APPLE_APP_SPECIFIC_PASSWORD: ${{ secrets.APPLE_APP_SPECIFIC_PASSWORD }}" in workflow
    assert "Verify signed and notarized artifacts" in workflow
    assert 'codesign --verify --deep --strict --verbose=2 "$APP_PATH"' in workflow
    assert 'spctl --assess --type execute --verbose "$APP_PATH"' in workflow
    assert 'xcrun stapler validate "$APP_PATH"' in workflow
    assert 'xcrun stapler validate "$DMG_PATH"' in workflow
    assert "Cleanup Apple signing keychain" in workflow


def test_version_gate_runs_macos_validation_from_existing_pr_workflow():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "version-gate.yml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(workflow)
    jobs = parsed["jobs"]

    assert "macos-app-validation" in jobs
    assert "macos-support-tracking-gate" in jobs
    assert "version-bump" in jobs
    assert "issues: read" in workflow
    assert "checks: read" in workflow
    assert jobs["macos-app-validation"]["strategy"]["fail-fast"] is False
    assert jobs["version-bump"]["needs"] == "version-gate"
    assert "Check macOS support GitHub tracking gate" in workflow
    assert "python scripts/check-macos-support-gate.py" in workflow
    assert "--skip-pr-checks" in workflow
    version_gate_text = workflow.split("  version-gate:", 1)[1].split("\n  version-bump:", 1)[0]
    assert "actions/create-github-app-token" not in version_gate_text
    assert "git fetch --depth=1 origin" in workflow
    assert "git checkout --detach FETCH_HEAD" in workflow
    assert "token: ${{ steps.app-token.outputs.token }}" in workflow
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
    assert "Smoke LaunchAgent registration" in workflow
    assert "bash scripts/smoke-macos-launchagent.sh build/install-smoke/TunnelForge.app" in workflow
    assert "Smoke ZIP package" in workflow
    assert "Upload macOS validation artifacts" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg.sha256" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.zip.sha256" in workflow
