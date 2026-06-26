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


def test_oneclick_readiness_does_not_present_closed_issues_as_current_tracking():
    doc = (PROJECT_ROOT / "docs" / "oneclick_readiness.md").read_text(encoding="utf-8")

    stale_phrases = [
        "GitHub #138 tracks real execution",
        "Real execution and automatic fix coverage: GitHub #138.",
        "Charset/collation automatic fix coverage: GitHub #139.",
        "Recommended next repo-side change:",
    ]

    for phrase in stale_phrases:
        assert phrase not in doc

    assert "Standing One-Click follow-up policy:" in doc
    assert "No repo-side One-Click follow-up issue is currently open" in doc


def test_oneclick_readiness_distinguishes_limited_real_execution_from_broad_production_support():
    doc = (PROJECT_ROOT / "docs" / "oneclick_readiness.md").read_text(encoding="utf-8")

    assert "- Production database usage." not in doc
    assert "backup-confirmed `deprecated_engine -> engine_innodb`" in doc
    assert "Broad production automatic remediation is not supported" in doc
    assert "Production charset/collation execution" in doc


def test_oneclick_evidence_readme_does_not_describe_completed_evidence_as_future():
    readme = (
        PROJECT_ROOT / "reports" / "oneclick_readiness" / "README.md"
    ).read_text(encoding="utf-8")

    stale_phrases = [
        "for a future controlled local non-dry-run",
        "shape for future controlled local",
        "Validate future charset/collation evidence",
    ]

    for phrase in stale_phrases:
        assert phrase not in readme

    assert "`oneclick-real-execution-evidence.json` was captured" in readme
    assert "`oneclick-charset-evidence.json` is captured" in readme
