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
