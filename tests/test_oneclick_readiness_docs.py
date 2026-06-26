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
    ]

    for phrase in stale_phrases:
        assert phrase not in doc
