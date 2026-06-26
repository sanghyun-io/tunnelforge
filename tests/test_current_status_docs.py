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


def test_current_status_top_handoff_does_not_present_closed_issues_as_current_work():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    top_handoff = " ".join(doc.split("## Issue Tracker", maxsplit=1)[0].split())

    stale_current_work_phrases = [
        "#140 remains the next actionable in-repo issue",
        "#139 is the next in-repo issue",
        "GitHub issue #139 now tracks the next",
    ]

    for phrase in stale_current_work_phrases:
        assert phrase not in top_handoff


def test_current_status_does_not_describe_old_gate_head_as_current():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    stale_phrases = [
        "#116 Current Evidence now points operators at current `main` / gate head `6da13f7`",
        "#116 Current Evidence now points operators at current `main` / gate head `c12e9b7`",
    ]

    for phrase in stale_phrases:
        assert phrase not in doc


def test_current_status_does_not_keep_stale_macos_focused_test_count():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "PASS, 47 passed" not in doc
    assert "Current main macOS focused tests" in doc


def test_current_status_does_not_keep_stale_full_pytest_count():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "PASS, 1729 passed" not in doc
    assert "PASS, 1786 passed" not in doc
    assert "PASS, 1793 passed, 5 warnings" in doc
    assert "Current main full Python suite" in doc


def test_current_status_records_export_table_selection_audit():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "Export table selection audit" in doc
    assert "RustDumpExportDialog" in doc
    assert "RustDumpExporter.export_tables" in doc
    assert "dump.run" in doc
