import hashlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import shlex
import subprocess
import tomllib
import zipfile

import pytest
import yaml


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SUCCESSFUL_MACOS_SMOKE_LOG = (
    "macOS /Applications install smoke checks passed.\n"
    "macOS release package smoke checks passed.\n"
)
SUCCESSFUL_MACOS_SYSTEM_EVIDENCE_LOG = (
    "== sw_vers ==\n"
    "ProductVersion: 14.7.1\n"
    "BuildVersion: 23H222\n"
    "== uname ==\n"
    "Darwin validator.local 23.6.0 Darwin Kernel Version\n"
    "== architecture ==\n"
    "arm64\n"
    "== final app ==\n"
    "/Applications/TunnelForge.app\n"
    "== codesign verify ==\n"
    "exit: 0\n"
    "== spctl assess ==\n"
    "exit: 0\n"
)


REQUIRED_MANUAL_REPORT_SECTIONS = [
    "## Automated Smoke",
    "## Interactive App Launch",
    "## SSH Tunnel",
    "## Database Connections",
    "## Export/Import",
    "## Migration",
    "## Settings And User Paths",
    "## LaunchAgent",
    "## Updates",
    "## Signing, Notarization, And Gatekeeper",
    "## Result",
]


REQUIRED_MANUAL_REPORT_CHECK_ITEMS = [
    "Run `bash scripts/validate-macos-release.sh`",
    "Confirm source `python main.py --ui-smoke-check` passed",
    "Download signed/notarized GitHub Actions macOS artifacts",
    "Confirm downloaded macOS artifact checksums passed",
    "Confirm built app smoke passed",
    "Confirm mounted DMG smoke passed",
    "Confirm copied DMG install smoke passed",
    "Confirm ZIP extracted app smoke passed",
    "Launch `python main.py`",
    "Launch `dist/TunnelForge.app`",
    "Install from DMG into `/Applications` and launch `/Applications/TunnelForge.app`",
    "Confirm `tunnelforge-core` starts from inside the app",
    "Create an SSH tunnel",
    "Confirm tunnel monitoring updates",
    "Close the SSH tunnel cleanly",
    "Test MySQL connection through Rust DB Core",
    "Test PostgreSQL connection through Rust DB Core",
    "Run Export/Import on a disposable MySQL database",
    "Run Export/Import on a disposable PostgreSQL database",
    "Confirm exported files and imported rows are correct",
    "Run inspect",
    "Run preflight",
    "Run plan",
    "Run migrate",
    "Run verify",
    "Run resume after an interrupted disposable migration",
    "Confirm config files use macOS user directories",
    "Confirm logs use macOS user directories",
    "Confirm SQL history uses macOS user directories",
    "Confirm migration state, analysis, and rollback files use macOS user directories",
    "Enable startup in settings",
    "Confirm `~/Library/LaunchAgents/io.sanghyun.tunnelforge.plist` exists",
    "Confirm LaunchAgent points to the expected app path",
    "Confirm LaunchAgent WorkingDirectory points to the app executable directory",
    "Confirm LaunchAgent writes stdout to `~/Library/Logs/TunnelForge/launchagent.out.log`",
    "Confirm LaunchAgent writes stderr to `~/Library/Logs/TunnelForge/launchagent.err.log`",
    "Disable startup in settings",
    "Confirm LaunchAgent is removed",
    "Confirm macOS update selection prefers the current architecture DMG",
    "Confirm the update UI opens the downloaded package",
    "Confirm the update UI does not execute DMG or ZIP as a program",
    "Run `codesign --verify --deep --strict --verbose=2 /Applications/TunnelForge.app`",
    "Run `spctl --assess --type execute --verbose /Applications/TunnelForge.app`",
    "Confirm notarization status if distributing outside internal testing",
    "Confirm first launch behavior after download/install",
]

REQUIRED_MANUAL_REPORT_EVIDENCE_SECTIONS = [
    "## Interactive App Launch",
    "## SSH Tunnel",
    "## Database Connections",
    "## Export/Import",
    "## Migration",
    "## Settings And User Paths",
    "## LaunchAgent",
    "## Updates",
    "## Signing, Notarization, And Gatekeeper",
]


def completed_manual_report_lines(
    smoke_log_arg: str,
    artifact_dir_arg: str = "build/macos-validation-artifacts",
    system_evidence_arg: str = "",
) -> list[str]:
    lines = [
        f"- Git SHA: {current_git_sha()}",
        "- macOS: 14.7.1 (23H222)",
        "- Architecture: arm64",
        "- Artifact workflow run: 26476324046",
        f"- Artifact directory: {artifact_dir_arg}",
        "- Artifact checksum verification: passed",
        "- Final app path: /Applications/TunnelForge.app",
        "- Final app executable: /Applications/TunnelForge.app/Contents/MacOS/TunnelForge",
        "- Release smoke: passed",
        f"- Smoke log: {smoke_log_arg}",
    ]
    if system_evidence_arg:
        lines.append(f"- System evidence log: {system_evidence_arg}")
    section_lines = []
    for section in REQUIRED_MANUAL_REPORT_SECTIONS:
        section_lines.append(section)
        if section in REQUIRED_MANUAL_REPORT_EVIDENCE_SECTIONS:
            section_lines.append(f"- Evidence: {section.removeprefix('## ')} validated on operator Mac")
    return [
        *lines,
        *section_lines,
        *(f"- [x] {item}" for item in REQUIRED_MANUAL_REPORT_CHECK_ITEMS),
        "- Overall result: passed",
        "- Validator: Codex",
        "",
    ]


def current_git_sha() -> str:
    return subprocess.run(
        ["git", "rev-parse", "--short", "HEAD"],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=True,
    ).stdout.strip()


def evidence_manifest(report: Path, smoke_log: Path) -> str:
    return (
        f"{hashlib.sha256(report.read_bytes()).hexdigest()}  {report.name}\n"
        f"{hashlib.sha256(smoke_log.read_bytes()).hexdigest()}  {smoke_log.name}\n"
    )


def evidence_manifest_with_system_log(report: Path, smoke_log: Path, system_log: Path) -> str:
    return (
        f"{hashlib.sha256(report.read_bytes()).hexdigest()}  {report.name}\n"
        f"{hashlib.sha256(smoke_log.read_bytes()).hexdigest()}  {smoke_log.name}\n"
        f"{hashlib.sha256(system_log.read_bytes()).hexdigest()}  {system_log.name}\n"
    )


def write_successful_system_evidence_log(report_dir: Path, name: str = "macos-system-evidence.log") -> Path:
    system_log = report_dir / name
    system_log.write_text(SUCCESSFUL_MACOS_SYSTEM_EVIDENCE_LOG, encoding="utf-8")
    return system_log


def completed_manual_report_lines_with_system(
    report_dir: Path,
    smoke_log_arg: str,
    artifact_dir_arg: str = "build/macos-validation-artifacts",
    system_log_name: str = "macos-system-evidence.log",
) -> list[str]:
    system_log = write_successful_system_evidence_log(report_dir, system_log_name)
    return completed_manual_report_lines(
        smoke_log_arg,
        artifact_dir_arg=artifact_dir_arg,
        system_evidence_arg=system_log.relative_to(PROJECT_ROOT).as_posix(),
    )


def write_bundle_checksum(bundle: Path) -> Path:
    checksum = bundle.with_name(f"{bundle.name}.sha256")
    checksum.write_text(f"{hashlib.sha256(bundle.read_bytes()).hexdigest()}  {bundle.name}\n", encoding="utf-8")
    return checksum


def load_macos_support_gate_module():
    script_path = PROJECT_ROOT / "scripts" / "check-macos-support-gate.py"
    spec = importlib.util.spec_from_file_location("check_macos_support_gate", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


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
    assert "raise SystemExit" in spec
    assert "Run `cargo build --manifest-path migration_core/Cargo.toml --release` first." in spec


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
    assert "MACOS_RELEASE_SMOKE_APPLICATIONS" in script
    assert "Smoke testing /Applications install" in script
    assert 'MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM=1 bash scripts/smoke-macos-applications-install.sh "$DMG_PATH"' in script
    assert "build/zip-smoke/TunnelForge.app/Contents/MacOS/TunnelForge" in script
    assert "--ui-smoke-check" in script
    assert "window_title" in script
    assert "tunnelforge-core" in script
    assert "Manual validation still required" in script


def test_macos_applications_install_smoke_script_validates_real_applications_path():
    script = (PROJECT_ROOT / "scripts" / "smoke-macos-applications-install.sh").read_text(encoding="utf-8")

    assert "This script must run on macOS." in script
    assert "MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM=1" in script
    assert "/Applications/TunnelForge.app" in script
    assert "hdiutil attach" in script
    assert "ditto" in script
    assert "python - <<'PY'" in script
    assert "--ui-smoke-check" in script
    assert "tunnelforge-core" in script
    assert "scripts/smoke-macos-launchagent.sh" in script
    assert "macOS /Applications install smoke checks passed." in script


def test_macos_manual_validation_report_script_records_remaining_gates():
    script = (PROJECT_ROOT / "scripts" / "macos-manual-validation-report.sh").read_text(encoding="utf-8")

    assert "This script must run on macOS." in script
    assert "MACOS_VALIDATION_REPORT" in script
    assert "MACOS_VALIDATION_SMOKE_LOG" in script
    assert "MACOS_VALIDATION_SYSTEM_EVIDENCE_LOG" in script
    assert "MACOS_VALIDATION_APP_PATH" in script
    assert "macos-release-smoke-${TIMESTAMP}.log" in script
    assert "bash scripts/validate-macos-release.sh" in script
    assert "MACOS_RELEASE_SMOKE_APPLICATIONS=1" in script
    assert "--check-complete <report>" in script
    assert "--bundle-evidence <report>" in script
    assert "--finalize <report>" in script
    assert "--post-github-comment" in script
    assert "--skip-github" in script
    assert "--evidence-bundle <zip>" in script
    assert "--download-artifacts" in script
    assert "--artifact-run-id <id>" in script
    assert "--artifact-output-dir <dir>" in script
    assert "--artifact-arch <arch>" in script
    assert "macos-download-validation-artifacts.sh" in script
    assert 'ARTIFACT_ARCH="${ARTIFACT_ARCH_ARG:-${ARCH}}"' in script
    assert "defaults to the latest successful manual run" in script
    assert "Default: current Mac arch." in script
    assert "--arch ${ARTIFACT_ARCH} --output-dir ${ARTIFACT_DIR}" in script
    assert "Add \\`--run-id ${ARTIFACT_WORKFLOW_RUN:-<workflow-run-id>}\\` only when pinning a specific run." in script
    assert "SMOKE_CHECK_MARK" in script
    assert "ARTIFACT_CHECK_MARK" in script
    assert "- [${SMOKE_CHECK_MARK}] Run \\`bash scripts/validate-macos-release.sh\\`" in script
    assert "- [${ARTIFACT_CHECK_MARK}] Download signed/notarized GitHub Actions macOS artifacts" in script
    assert "MACOS_VALIDATION_EVIDENCE_BUNDLE" in script
    assert "Evidence bundle:" in script
    assert "PYTHON_BIN" in script
    assert "hashlib.sha256" in script
    assert "extract_report_value" in script
    assert "sha256_file" in script
    assert "manifest_name" in script
    assert "checksum_path_for_bundle" in script
    assert "Evidence bundle checksum:" in script
    assert "Evidence bundle SHA256:" in script
    assert "check_complete_report" in script
    assert "section_has_evidence_note" in script
    assert "required_evidence_sections" in script
    assert "forbidden_fragments" in script
    assert "placeholder_token" in script
    assert "concrete non-placeholder observations" in script
    assert "Evidence:" in script
    assert "Fill every - Evidence: note with concrete observed behavior or file/log paths." in script
    assert "required_sections" in script
    assert "required_check_items" in script
    assert "Manual validation report is missing required section" in script
    assert "Manual validation report is missing required checklist item" in script
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
    assert "Artifact workflow run:" in script
    assert "Artifact directory:" in script
    assert "Artifact checksum verification:" in script
    assert "Download signed/notarized GitHub Actions macOS artifacts" in script
    assert "Confirm downloaded macOS artifact checksums passed" in script
    assert "Smoke log is missing or empty" in script
    assert "macOS release package smoke checks passed." in script
    assert "macOS /Applications install smoke checks passed." in script
    assert 'tee "$SMOKE_LOG_PATH"' in script
    assert "PIPESTATUS" in script
    assert "Smoke log:" in script
    assert "System evidence log:" in script
    assert "sw_vers" in script
    assert "uname -a" in script
    assert "Final app path:" in script
    assert "Completion check:" in script
    assert "SSH tunnel" in script
    assert "- Optional: record reconnect behavior if applicable" in script
    assert "- [ ] Confirm reconnect behavior if applicable" not in script
    assert "MySQL" in script
    assert "PostgreSQL" in script
    assert "- Optional: record direct connection mode if applicable" in script
    assert "- [ ] Test direct connection mode if applicable" not in script
    assert "Export/Import" in script
    assert "Migration" in script
    assert "LaunchAgent" in script
    assert "Gatekeeper" in script
    assert "spctl" in script
    assert "codesign" in script
    assert "notarization" in script


def test_macos_validation_artifact_download_script_fetches_manual_run_artifacts():
    script = (PROJECT_ROOT / "scripts" / "macos-download-validation-artifacts.sh").read_text(encoding="utf-8")

    assert "macOS App Validation" in script
    assert "workflow_dispatch" in script
    assert "macos-app.yml" in script
    assert '"$GH_BIN" run download' in script
    assert '"$GH_BIN" api' in script
    assert "PR_NUMBER=117" in script
    assert "TunnelForge-macOS-*-${ARCH_FILTER}" in script
    assert "verify_downloaded_checksums" in script
    assert "Checksum verified" in script
    assert "clean_existing_artifacts" in script
    assert 'find "$output_dir" -mindepth 1 -maxdepth 1 -name "$artifact_pattern" -print0' in script
    assert "--write-env <file>" in script
    assert "MACOS_VALIDATION_ARTIFACT_RUN_ID" in script
    assert "MACOS_VALIDATION_ARTIFACT_DIR" in script
    assert "MACOS_VALIDATION_ARTIFACT_CHECKSUMS" in script


def test_macos_validation_artifact_download_script_writes_env_file(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    fake_bin = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-fake-gh-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "run" && "$2" == "download" ]]; then
  output_dir=""
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "--dir" ]]; then
      next=$((i + 1))
      output_dir="${!next}"
    fi
  done
  artifact_dir="${output_dir}/TunnelForge-macOS-2.0.5-arm64"
  mkdir -p "$artifact_dir"
  printf fake-dmg > "${artifact_dir}/TunnelForge-macOS-2.0.5-arm64.dmg"
  shasum -a 256 "${artifact_dir}/TunnelForge-macOS-2.0.5-arm64.dmg" > "${artifact_dir}/TunnelForge-macOS-2.0.5-arm64.dmg.sha256"
  exit 0
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_gh.chmod(0o755)

    output_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-artifacts"
    env_file = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-macos-validation-artifacts.env"
    output_dir_arg = output_dir.relative_to(PROJECT_ROOT).as_posix()
    env_file_arg = env_file.relative_to(PROJECT_ROOT).as_posix()
    fake_gh_arg = shlex.quote(fake_gh.relative_to(PROJECT_ROOT).as_posix())

    result = subprocess.run(
        [
            "bash",
            "-c",
            "GH="
            + fake_gh_arg
            + " bash scripts/macos-download-validation-artifacts.sh"
            + " --run-id 26477946208"
            + " --repo sanghyun-io/tunnelforge"
            + " --arch arm64"
            + f" --output-dir {shlex.quote(output_dir_arg)}"
            + f" --write-env {shlex.quote(env_file_arg)}",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    env_text = env_file.read_text(encoding="utf-8")
    assert "MACOS_VALIDATION_ARTIFACT_RUN_ID=26477946208" in env_text
    assert f"MACOS_VALIDATION_ARTIFACT_DIR={output_dir_arg}" in env_text
    assert "MACOS_VALIDATION_ARTIFACT_CHECKSUMS=passed" in env_text


def test_macos_validation_artifact_download_script_cleans_stale_artifact_before_retry(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    fake_bin = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-fake-gh-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    fake_gh = fake_bin / "gh"
    fake_gh.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "$1" == "run" && "$2" == "download" ]]; then
  output_dir=""
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "--dir" ]]; then
      next=$((i + 1))
      output_dir="${!next}"
    fi
  done
  artifact_dir="${output_dir}/TunnelForge-macOS-2.0.5-arm64"
  if [[ -e "${artifact_dir}/stale-partial-download" ]]; then
    echo "stale artifact was not cleaned" >&2
    exit 9
  fi
  mkdir -p "$artifact_dir"
  printf fake-dmg > "${artifact_dir}/TunnelForge-macOS-2.0.5-arm64.dmg"
  shasum -a 256 "${artifact_dir}/TunnelForge-macOS-2.0.5-arm64.dmg" > "${artifact_dir}/TunnelForge-macOS-2.0.5-arm64.dmg.sha256"
  exit 0
fi
echo "unexpected gh invocation: $*" >&2
exit 1
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_gh.chmod(0o755)

    output_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-retry-artifacts"
    shutil.rmtree(output_dir, ignore_errors=True)
    stale_artifact_dir = output_dir / "TunnelForge-macOS-2.0.5-arm64"
    stale_artifact_dir.mkdir(parents=True)
    (stale_artifact_dir / "stale-partial-download").write_text("stale", encoding="utf-8")
    output_dir_arg = output_dir.relative_to(PROJECT_ROOT).as_posix()
    fake_gh_arg = shlex.quote(fake_gh.relative_to(PROJECT_ROOT).as_posix())

    result = subprocess.run(
        [
            "bash",
            "-c",
            "GH="
            + fake_gh_arg
            + " bash scripts/macos-download-validation-artifacts.sh"
            + " --run-id 26477946208"
            + " --repo sanghyun-io/tunnelforge"
            + " --arch arm64"
            + f" --output-dir {shlex.quote(output_dir_arg)}",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (stale_artifact_dir / "stale-partial-download").exists()
    assert (stale_artifact_dir / "TunnelForge-macOS-2.0.5-arm64.dmg").exists()


def test_macos_validation_artifact_download_script_verifies_flat_downloaded_checksums(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    artifact_dir = tmp_path / "TunnelForge-macOS-2.0.5-arm64"
    artifact_dir.mkdir()
    dmg = artifact_dir / "TunnelForge-macOS-2.0.5-arm64.dmg"
    dmg.write_bytes(b"fake-dmg")
    checksum = hashlib.sha256(dmg.read_bytes()).hexdigest()
    (artifact_dir / "TunnelForge-macOS-2.0.5-arm64.dmg.sha256").write_text(
        f"{checksum}  dist/{dmg.name}\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "bash",
            "scripts/macos-download-validation-artifacts.sh",
            "--verify-only",
            artifact_dir.as_posix(),
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert f"Checksum verified:" in result.stdout
    assert dmg.name in result.stdout


def test_macos_launchagent_smoke_script_validates_packaged_app_plist():
    script = (PROJECT_ROOT / "scripts" / "smoke-macos-launchagent.sh").read_text(encoding="utf-8")

    assert "This script must run on macOS." in script
    assert "build/install-smoke/TunnelForge.app" in script
    assert "StartupRegistrar" in script
    assert "sys.frozen = True" in script
    assert "io.sanghyun.tunnelforge" in script
    assert "ProgramArguments" in script
    assert "WorkingDirectory" in script
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
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")

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


def test_macos_manual_validation_report_check_complete_rejects_missing_required_sections(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-sections"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
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
    assert "Manual validation report is missing required section" in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_missing_required_check_items(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-items"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text(
        "\n".join(
            [
                "- Release smoke: passed",
                f"- Smoke log: {smoke_log_arg}",
                *REQUIRED_MANUAL_REPORT_SECTIONS,
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
    assert "Manual validation report is missing required checklist item" in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_deleted_export_result_check(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    missing_item = "Confirm exported files and imported rows are correct"
    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-export-result"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [line for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg) if missing_item not in line]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    assert f"Manual validation report is missing required checklist item: {missing_item}" in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_missing_interactive_evidence_notes(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-evidence-notes"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        line
        for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
        if not line.startswith("- Evidence:")
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    assert "Manual validation report must include evidence note under section: ## Interactive App Launch" in result.stderr
    assert "Manual validation report must include evidence note under section: ## Migration" in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_placeholder_evidence_notes(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-placeholder-evidence"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        "- Evidence: Gate rehearsal placeholder; real operator Mac report must record observed behavior."
        if line.startswith("- Evidence:")
        else line
        for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    assert "concrete non-placeholder observations" in result.stderr
    assert "Manual validation report must include evidence note under section: ## Interactive App Launch" in result.stderr


def test_macos_manual_validation_report_check_complete_accepts_concrete_evidence_with_arrows(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-concrete-evidence"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        "- Evidence: Validated MySQL -> PostgreSQL disposable migration on arm64; logs saved under build/operator-mac.log."
        if line.startswith("- Evidence:")
        else line
        for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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

    assert result.returncode == 0
    assert f"Manual validation report is complete: {report_arg}" in result.stdout


def test_macos_manual_validation_report_check_complete_rejects_missing_applications_smoke(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-applications-smoke"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text("macOS release package smoke checks passed.\n", encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")

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
    assert "Smoke log must include the successful /Applications install smoke completion message." in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_missing_real_macos_metadata(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-macos-metadata"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        line
        for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
        if not line.startswith("- macOS:") and not line.startswith("- Architecture:")
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    assert "Manual validation report must include a real macOS version." in result.stderr
    assert "Manual validation report must include a supported Mac architecture." in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_missing_artifact_metadata(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-artifact-metadata"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        line
        for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
        if not line.startswith("- Artifact workflow run:")
        and not line.startswith("- Artifact directory:")
        and not line.startswith("- Artifact checksum verification:")
        and "Download signed/notarized GitHub Actions macOS artifacts" not in line
        and "Confirm downloaded macOS artifact checksums passed" not in line
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    assert "Manual validation report must include a GitHub Actions artifact workflow run id." in result.stderr
    assert "Manual validation report must include a downloaded artifact directory." in result.stderr
    assert "Manual validation report must record '- Artifact checksum verification: passed'." in result.stderr
    assert (
        "Manual validation report is missing required checklist item: "
        "Download signed/notarized GitHub Actions macOS artifacts"
    ) in result.stderr
    assert (
        "Manual validation report is missing required checklist item: "
        "Confirm downloaded macOS artifact checksums passed"
    ) in result.stderr


def test_macos_manual_validation_report_check_complete_rejects_missing_applications_path_metadata(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-app-path"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        line
        for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
        if not line.startswith("- Final app path:") and not line.startswith("- Final app executable:")
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    assert "Manual validation report must record final app path under /Applications." in result.stderr
    assert "Manual validation report must record final app executable under /Applications." in result.stderr


def test_macos_manual_validation_report_bundle_evidence_creates_zip(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-bundle"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")

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
            "macos-system-evidence.log",
        ]
        assert archive.read(manifest_name).decode("utf-8") == evidence_manifest_with_system_log(
            report, smoke_log, system_log
        )


def test_macos_manual_validation_report_bundle_evidence_includes_system_evidence_log(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-bundle-system"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    report_lines = completed_manual_report_lines_with_system(report_dir, smoke_log_arg)
    report.write_text("\n".join(report_lines), encoding="utf-8")

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
    with zipfile.ZipFile(bundle) as archive:
        manifest_name = "macos-manual-validation-evidence-macos-manual-validation-report.sha256"
        assert sorted(archive.namelist()) == [
            manifest_name,
            "macos-manual-validation-report.md",
            "macos-release-smoke.log",
            "macos-system-evidence.log",
        ]
        assert archive.read(manifest_name).decode("utf-8") == evidence_manifest_with_system_log(
            report, smoke_log, system_log
        )


def test_macos_manual_validation_report_finalize_creates_zip_and_runs_local_gate(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-finalize"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")

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
    assert f"- System evidence log: {system_log.relative_to(PROJECT_ROOT).as_posix()}" in result.stdout
    comment_arg = (
        report_dir / "macos-final-validation-github-comment-macos-manual-validation-report.md"
    ).relative_to(PROJECT_ROOT).as_posix()
    assert f"- GitHub evidence comment: {comment_arg}" in result.stdout
    assert f"- Evidence bundle: {bundle_arg}" in result.stdout
    assert f"- Evidence bundle checksum: {bundle_arg}.sha256" in result.stdout
    comment = PROJECT_ROOT / comment_arg
    assert comment.exists()
    comment_text = comment.read_text(encoding="utf-8")
    assert "Final macOS validation evidence for #116" in comment_text
    assert report_arg in comment_text
    assert smoke_log_arg in comment_text
    assert system_log.relative_to(PROJECT_ROOT).as_posix() in comment_text
    assert bundle_arg in comment_text
    assert f"{bundle_arg}.sha256" in comment_text
    assert f"Git SHA: `{current_git_sha()}`" in comment_text
    assert "Artifact workflow run: `26476324046`" in comment_text
    assert f"Evidence bundle SHA256: `{hashlib.sha256(bundle.read_bytes()).hexdigest()}`" in comment_text
    assert "gh issue comment 116 --body-file" in comment_text
    assert "Keep #116 open until these files are attached" in comment_text


def test_macos_manual_validation_report_finalize_can_post_github_comment(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-finalize-post"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")

    fake_gh = report_dir / "gh"
    gh_log = report_dir / "gh.log"
    gh_log.unlink(missing_ok=True)
    fake_gh.write_text(
        f"""#!/usr/bin/env bash
set -euo pipefail
printf '%s\\n' "$*" >> {shlex.quote(gh_log.relative_to(PROJECT_ROOT).as_posix())}
body_file="${{@:$#}}"
test -s "$body_file"
""",
        encoding="utf-8",
        newline="\n",
    )
    fake_gh.chmod(0o755)

    result = subprocess.run(
        [
            "bash",
            "-c",
            f"GH={shlex.quote(fake_gh.relative_to(PROJECT_ROOT).as_posix())} bash scripts/macos-manual-validation-report.sh"
            f" --finalize {shlex.quote(report_arg)}"
            f" --evidence-bundle {shlex.quote(bundle_arg)}"
            " --skip-github"
            " --post-github-comment",
        ],
        cwd=PROJECT_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    comment_arg = (
        report_dir / "macos-final-validation-github-comment-macos-manual-validation-report.md"
    ).relative_to(PROJECT_ROOT).as_posix()
    assert "Posted GitHub evidence comment to issue #116 and PR #117" in result.stdout
    assert gh_log.read_text(encoding="utf-8").splitlines() == [
        f"issue comment 116 --body-file {comment_arg}",
        f"pr comment 117 --body-file {comment_arg}",
    ]


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
    assert "check_report_git_sha" in script
    assert "workflow_dispatch" in script
    assert "macOS App Validation" in script
    assert "Verify signed and notarized artifacts" in script
    assert "manual macOS signing/notarization workflow passed" in script
    assert "check_report_artifact_workflow_run" in script
    assert "manual validation report Artifact workflow run matches" in script


def test_macos_support_gate_script_checks_report_artifact_workflow_run(tmp_path, capsys):
    gate = load_macos_support_gate_module()
    report = tmp_path / "macos-manual-validation-report.md"
    report.write_text("- Artifact workflow run: 26477209665\n", encoding="utf-8")

    assert gate.check_report_artifact_workflow_run(report, "26477209665", "manual macOS workflow run") is True
    assert "manual validation report Artifact workflow run matches manual macOS workflow run: 26477209665" in capsys.readouterr().out

    assert gate.check_report_artifact_workflow_run(report, "111", "manual macOS workflow run") is False
    assert "manual validation report Artifact workflow run 26477209665 does not match manual macOS workflow run 111" in capsys.readouterr().err


def test_macos_support_gate_script_accepts_local_final_report(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-gate-complete"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(report, report.name)
        archive.write(smoke_log, smoke_log.name)
        archive.write(system_log, system_log.name)
        archive.writestr(
            "macos-manual-validation-evidence-macos-manual-validation-report.sha256",
            evidence_manifest_with_system_log(report, smoke_log, system_log),
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
        smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
        system_log = report_dir / f"macos-system-evidence-{stamp}.log"
        report = report_dir / f"macos-manual-validation-report-{stamp}.md"
        bundle = report_dir / f"macos-manual-validation-evidence-macos-manual-validation-report-{stamp}.zip"
        smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
        report.write_text(
            "\n".join(
                completed_manual_report_lines_with_system(
                    report_dir,
                    smoke_log_arg,
                    system_log_name=system_log.name,
                )
            ),
            encoding="utf-8",
        )
        with zipfile.ZipFile(bundle, "w") as archive:
            archive.write(report, report.name)
            archive.write(smoke_log, smoke_log.name)
            archive.write(system_log, system_log.name)
            archive.writestr(
                f"macos-manual-validation-evidence-{report.stem}.sha256",
                evidence_manifest_with_system_log(report, smoke_log, system_log),
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
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report.write_text("\n".join(completed_manual_report_lines_with_system(report_dir, smoke_log_arg)), encoding="utf-8")
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


def test_macos_support_gate_script_rejects_report_from_different_git_sha(tmp_path):
    if shutil.which("bash") is None:
        pytest.skip("bash is required for shell script validation")

    report_dir = PROJECT_ROOT / "build" / f"pytest-{tmp_path.name}-gate-wrong-sha"
    report_dir.mkdir(parents=True, exist_ok=True)
    smoke_log = report_dir / "macos-release-smoke.log"
    smoke_log.write_text(SUCCESSFUL_MACOS_SMOKE_LOG, encoding="utf-8")
    system_log = report_dir / "macos-system-evidence.log"
    report = report_dir / "macos-manual-validation-report.md"
    bundle = report_dir / "macos-manual-validation-evidence.zip"
    smoke_log_arg = smoke_log.relative_to(PROJECT_ROOT).as_posix()
    report_arg = report.relative_to(PROJECT_ROOT).as_posix()
    bundle_arg = bundle.relative_to(PROJECT_ROOT).as_posix()
    report_lines = [
        "- Git SHA: deadbeef",
        *[line for line in completed_manual_report_lines_with_system(report_dir, smoke_log_arg) if not line.startswith("- Git SHA:")],
    ]
    report.write_text("\n".join(report_lines), encoding="utf-8")
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.write(report, report.name)
        archive.write(smoke_log, smoke_log.name)
        archive.write(system_log, system_log.name)
        archive.writestr(
            "macos-manual-validation-evidence-macos-manual-validation-report.sha256",
            evidence_manifest_with_system_log(report, smoke_log, system_log),
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

    assert result.returncode == 1
    assert "manual validation report Git SHA deadbeef does not match local HEAD" in result.stderr


def test_release_workflow_has_macos_app_job_and_assets():
    workflow = (PROJECT_ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    macos_job = workflow.split("  build-macos-app:", 1)[1].split("\n  create-release:", 1)[0]

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
    assert "dtolnay/rust-toolchain" not in macos_job
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
    assert "Smoke /Applications install" in workflow
    assert "MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM: \"1\"" in workflow
    assert "bash scripts/smoke-macos-applications-install.sh" in workflow
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
    assert "macOS DMG/ZIP 설치파일은 아직 최종 실제 Mac 운영자 검증 전의 베타 배포물입니다." in workflow
    assert "운영 환경 사용은 사용자 책임입니다" in workflow
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
    assert "Normalize release artifacts" in workflow_text
    assert "find release-artifacts -type f" in workflow_text
    assert "softprops/action-gh-release@v3" in workflow_text
    assert "Create GitHub Release" in workflow_text
    assert "release-upload/TunnelForge-Setup-*.exe" in workflow_text
    assert "release-upload/TunnelForge-WebSetup.exe" in workflow_text
    assert "release-upload/TunnelForge-macOS-*.dmg" in workflow_text
    assert "release-upload/TunnelForge-macOS-*.zip" in workflow_text
    assert "release-upload/TunnelForge-macOS-*.dmg.sha256" in workflow_text
    assert "release-upload/TunnelForge-macOS-*.zip.sha256" in workflow_text
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
    assert "Smoke /Applications install" in workflow
    assert "MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM: \"1\"" in workflow
    assert "bash scripts/smoke-macos-applications-install.sh" in workflow
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
    assert "Smoke /Applications install" in workflow
    assert "MACOS_APPLICATIONS_SMOKE_ALLOW_SYSTEM: \"1\"" in workflow
    assert "bash scripts/smoke-macos-applications-install.sh" in workflow
    assert "Smoke LaunchAgent registration" in workflow
    assert "bash scripts/smoke-macos-launchagent.sh build/install-smoke/TunnelForge.app" in workflow
    assert "Smoke ZIP package" in workflow
    assert "Upload macOS validation artifacts" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.dmg.sha256" in workflow
    assert "TunnelForge-macOS-${{ steps.version.outputs.version }}-${{ matrix.arch }}.zip.sha256" in workflow


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


def test_windows_installer_captures_selected_app_language():
    installer = (PROJECT_ROOT / "installer" / "TunnelForge.iss").read_text(encoding="utf-8")

    assert 'Name: "korean"; MessagesFile: "compiler:Languages\\Korean.isl"' in installer
    assert 'Name: "english"; MessagesFile: "compiler:Default.isl"' in installer
    assert "installer-language.txt" in installer
    assert "ActiveLanguage = 'english'" in installer
    assert "LanguageCode := 'en'" in installer
    assert "LanguageCode := 'ko'" in installer
    assert "SaveStringToFile(HintPath, LanguageCode, False)" in installer
    assert "english.RecoveryShortcut=Recovery and Update" in installer
