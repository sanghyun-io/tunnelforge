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
    assert "scripts/macos-manual-validation-report.sh" in doc
    assert "scripts/check-macos-support-gate.py" in doc
    assert "--check-complete" in doc
    assert "--bundle-evidence" in doc
    assert "--finalize" in doc
    assert "--bundle" in doc
    assert "--evidence-bundle" in doc
    assert "--skip-github" in doc
    assert "--skip-pr-checks" in doc
    assert "--final" in doc
    assert "macos-manual-validation-evidence" in doc
    assert "SHA256 manifest" in doc
    assert "*.zip.sha256" in doc
    assert "smoke log file" in doc
    assert "Overall result" in doc
    assert "Validator" in doc
    assert "python main.py --ui-smoke-check" in doc
    assert "copied DMG install" in doc
    assert "--ui-smoke-check" in doc
    assert "SSH tunnel" in doc
    assert "Rust DB Core" in doc
    assert "Export/Import" in doc
    assert "Migration" in doc
    assert "launchagent.{out,err}.log" in doc
    assert ".sha256" in doc


def test_macos_support_plan_references_github_tracking_issues():
    doc = (PROJECT_ROOT / "docs" / "macos_support.md").read_text(encoding="utf-8")

    for issue_number in ("#110", "#111", "#112", "#113", "#114", "#115", "#116"):
        assert issue_number in doc
