from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_macos_support_plan_documents_scope_and_validation_gates():
    doc = (PROJECT_ROOT / "docs" / "macos_support.md").read_text(encoding="utf-8")

    assert "macOS 13+" in doc
    assert "Apple Silicon" in doc
    assert "Intel" in doc
    assert "Final Manual Validation" in doc
    assert "Windows Regression Gates" in doc
    assert "Version Gate" in doc
    assert "macOS App Validation" in doc
    assert "scripts/validate-macos-release.sh" in doc
    assert "scripts/smoke-macos-launchagent.sh" in doc
    assert "scripts/macos-download-validation-artifacts.sh" in doc
    assert "scripts/macos-manual-validation-report.sh" in doc
    assert "scripts/check-macos-support-gate.py" in doc
    assert "--check-complete" in doc
    assert "--bundle-evidence" in doc
    assert "--finalize" in doc
    assert "--post-github-comment" in doc
    assert "--bundle" in doc
    assert "--evidence-bundle" in doc
    assert "--download-artifacts" in doc
    assert "--artifact-run-id" in doc
    assert "--artifact-output-dir" in doc
    assert "--artifact-arch" in doc
    assert "--write-env" in doc
    assert (
        "MACOS_RELEASE_SMOKE_APPLICATIONS=1 bash scripts/macos-manual-validation-report.sh "
        "--download-artifacts --run-smoke"
    ) in doc
    assert "Use `--artifact-arch <arm64|x86_64|all>` only when overriding the current Mac architecture." in doc
    assert "Use `--artifact-run-id <workflow-run-id>` only when you need to pin a specific workflow run." in doc
    assert "--skip-github" in doc
    assert "--skip-pr-checks" in doc
    assert "--final" in doc
    assert "macos-manual-validation-evidence" in doc
    assert "SHA256 manifest" in doc
    assert "Evidence bundle SHA256" in doc
    assert "*.zip.sha256" in doc
    assert "smoke log file" in doc
    assert "system evidence log" in doc
    assert "Evidence:" in doc
    assert "interactive section" in doc
    assert "placeholder/TODO text" in doc
    assert "GitHub evidence comment" in doc
    assert "Overall result" in doc
    assert "Validator" in doc
    assert "required sections" in doc
    assert "required checklist items" in doc
    assert "successful /Applications install smoke" in doc
    assert "pre-check" in doc
    assert "Git SHA" in doc
    assert "current PR head before merge, or the current merged main HEAD after PR #117 has merged" in doc
    assert "macOS version" in doc
    assert "Mac architecture" in doc
    assert "Artifact workflow run" in doc
    assert "Artifact head SHA" in doc
    assert "Artifact checksum verification" in doc
    assert "Final app path" in doc
    assert "python main.py --ui-smoke-check" in doc
    assert "copied DMG install" in doc
    assert "/Applications install smoke" in doc
    assert "MACOS_RELEASE_SMOKE_APPLICATIONS=1" in doc
    assert "--ui-smoke-check" in doc
    assert "SSH tunnel" in doc
    assert "Rust DB Core" in doc
    assert "Export/Import" in doc
    assert "Migration" in doc
    assert "launchagent.{out,err}.log" in doc
    assert "LaunchAgent plist" in doc
    assert "WorkingDirectory" in doc
    assert ".sha256" in doc
    assert "APPLE_CODESIGN_CERTIFICATE_P12_BASE64" in doc
    assert "APPLE_CODESIGN_CERTIFICATE_PASSWORD" in doc
    assert "APPLE_CODESIGN_IDENTITY" in doc
    assert "APPLE_APP_SPECIFIC_PASSWORD" in doc
    assert "stapled `.app`" in doc
    assert "ZIP distribution" in doc
    assert "workflow_dispatch" in doc
    assert "gh run download" in doc
    assert "signed/notarized macOS validation" in doc
    assert "spctl --assess" in doc


def test_macos_support_plan_references_github_tracking_issues():
    doc = (PROJECT_ROOT / "docs" / "macos_support.md").read_text(encoding="utf-8")

    for issue_number in ("#110", "#111", "#112", "#113", "#114", "#115", "#116"):
        assert issue_number in doc
