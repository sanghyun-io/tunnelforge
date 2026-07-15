from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_oneclick_readiness_documents_int_display_width_as_display_only_skip():
    doc = (PROJECT_ROOT / "docs" / "oneclick_readiness.md").read_text(encoding="utf-8")

    assert (
        "| `int_display_width` | display-only skip | `skip` |"
        in doc
    )
    assert "Rust Core live One-Click does not synthesize `int_display_width` issues" in doc
    assert "does not execute SQL" in doc


def test_oneclick_readiness_records_phase_a_apply_gate_and_prerequisites():
    doc = (PROJECT_ROOT / "docs" / "oneclick_readiness.md").read_text(encoding="utf-8")

    assert "ONECLICK_REAL_EXECUTION_ENABLED = False" in doc
    assert "ONECLICK_REAL_EXECUTION_ENABLED = True" not in doc
    assert "oneclick_apply_disabled" in doc
    assert (
        "Phase A disables One-Click real-execution evidence capture; exact-plan "
        "approval and TF-STATUS-098 are required before DB mutation."
    ) in doc
    assert "exact-plan approval" in doc
    assert "TF-STATUS-098" in doc
    assert "Phase A" in doc
    assert "Limited real execution is available" not in doc
    assert "No One-Click command, worker, dialog, or evidence capture helper" not in doc
    assert "The real-execution capture command does not support non-dry-run execution" in doc


def test_oneclick_readiness_marks_mutation_evidence_as_historical_archive():
    doc = (PROJECT_ROOT / "docs" / "oneclick_readiness.md").read_text(encoding="utf-8")

    assert "archived historical evidence" in doc
    assert "not current apply-readiness proof" in doc
    assert "Completed evidence proves Rust Core `oneclick.apply_fixes` can convert" not in doc
    assert "current evidence supports exposing One-Click with dry-run default and a" not in doc


def test_oneclick_evidence_readme_refreshes_only_dry_run_and_archives_mutation_evidence():
    readme = (
        PROJECT_ROOT / "reports" / "oneclick_readiness" / "README.md"
    ).read_text(encoding="utf-8")

    assert "archived historical evidence" in readme
    assert "not current live-success or apply-readiness proof" in readme
    assert "capture-oneclick-dry-run-evidence.py" in readme
    assert "Phase B" in readme
    assert "exact-plan approval" in readme
    assert "TF-STATUS-098" in readme

    mutation_capture_scripts = [
        "capture-oneclick-real-execution-evidence.py",
        "capture-oneclick-charset-evidence.py",
        "capture-oneclick-charset-derivation-evidence.py",
    ]

    for script_name in mutation_capture_scripts:
        assert script_name not in readme
