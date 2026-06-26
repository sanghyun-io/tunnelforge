from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = text.index(marker) + len(marker)
    next_heading = text.find("\n## ", start)
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


def test_current_status_summary_does_not_point_to_closed_oneclick_issue_as_next_work():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())

    assert "Open GitHub issue #116 remains external" in summary
    assert "GitHub issues #137 through #141 closed" in summary
    assert "No repo-side One-Click follow-up issue is currently open" in summary
    assert "GitHub issue #139 now tracks the next" not in summary
