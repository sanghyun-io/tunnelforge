from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _section(text: str, heading: str) -> str:
    marker = f"## {heading}\n"
    start = text.index(marker) + len(marker)
    next_heading = text.find("\n## ", start)
    if next_heading == -1:
        return text[start:]
    return text[start:next_heading]


def _check_row_commands(section: str) -> list[str]:
    check_rows = [
        line
        for line in section.splitlines()
        if line.startswith("| `") and "` |" in line
    ]
    return [line.split("`", maxsplit=2)[1] for line in check_rows]


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
    baseline = _section(doc, "Current Baseline Verification")

    assert "PASS, 47 passed" not in doc
    assert "PASS, 51 passed" not in baseline
    assert "PASS, 52 passed" not in baseline
    assert "PASS, 53 passed" in baseline
    assert "Current main macOS focused tests" in doc


def test_current_status_does_not_keep_stale_full_pytest_count():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "PASS, 1729 passed" not in doc
    assert "PASS, 1786 passed" not in doc
    assert "PASS, 1793 passed, 5 warnings" not in doc
    assert "PASS, 1794 passed, 5 warnings" not in doc
    assert "PASS, 1795 passed, 5 warnings" not in doc
    assert "PASS, 1796 passed, 5 warnings" not in doc
    assert "PASS, 1799 passed, 5 warnings" not in doc
    assert "PASS, 1801 passed, 5 warnings" not in doc
    assert "1801 passed, 5 warnings" not in doc
    assert "PASS, 1803 passed, 5 warnings" not in doc
    assert "PASS, 1805 passed, 5 warnings" not in doc
    assert "PASS, 1807 passed, 5 warnings" not in doc
    assert "PASS, 1808 passed, 5 warnings" not in doc
    assert "PASS, 1809 passed, 5 warnings" not in doc
    assert "PASS, 1811 passed, 5 warnings" not in doc
    assert "PASS, 1812 passed, 5 warnings" not in doc
    assert "PASS, 1815 passed, 5 warnings" not in doc
    assert "PASS, 1816 passed, 5 warnings" in doc
    assert "Current main full Python suite" in doc


def test_current_status_current_baseline_section_is_not_stale_dated():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    headings = [line for line in doc.splitlines() if line.startswith("## ")]

    assert "## Current Baseline Verification" in doc
    assert "## Verified On 2026-06-26" not in headings
    assert "Full-suite count refreshed on 2026-06-27" in doc


def test_current_status_records_export_table_selection_audit():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "Export table selection audit" in doc
    assert "RustDumpExportDialog" in doc
    assert "RustDumpExporter.export_tables" in doc
    assert "dump.run" in doc


def test_current_status_records_current_main_next_issue_reaudit():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-030" in doc
    assert "Current main next-issue re-audit" in doc
    assert "legacy connector names" in summary
    assert "Legacy Auto-Fix Wizard mutation path" in summary


def test_current_status_records_rust_core_export_import_menu_wording():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "TF-STATUS-034" in doc
    assert "Rust Core Export/Import menu wording" in doc
    assert "Rust DB Core Export" in doc
    assert "Rust DB Core Import" in doc
    assert "Shell Export" not in doc
    assert "Shell Import" not in doc


def test_current_status_records_oneclick_fallback_dry_run_tooltip_cleanup():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "TF-STATUS-035" in doc
    assert "One-Click fallback dry-run tooltip" in doc
    assert "disabled in this build" in doc
    assert "One-Click allows Dry-run only until the GitHub #138" not in doc


def test_current_status_records_oneclick_module_scope_docstring_cleanup():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "TF-STATUS-036" in doc
    assert "One-Click module scope docstring" in doc
    assert "Rust DB Core dry-run default and limited real execution" in doc
    assert "전체 마이그레이션 프로세스를 자동으로 실행합니다" not in doc


def test_current_status_records_build_doc_installer_version_cleanup():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")

    assert "TF-STATUS-037" in doc
    assert "BUILD installer version examples" in doc
    assert "TunnelForge-Setup-{version}.exe" in doc
    assert "TunnelForge-Setup-1.0.0.exe" not in doc


def test_current_status_records_macos_manual_workflow_head_policy():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())

    assert "TF-STATUS-038" in doc
    assert "macOS manual workflow head policy" in doc
    assert "manual workflow_dispatch artifact run now follows the same head policy" in normalized_doc


def test_current_status_records_post_merge_next_issue_external_reaudit():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())

    assert "TF-STATUS-039" in doc
    assert "Post-merge next-issue external re-audit" in doc
    assert "no new GitHub issue was created from that pass" in normalized_doc
    assert "SQL editor query execution also routes through the Rust connector shim" in normalized_doc


def test_current_status_tracks_legacy_python_auto_fix_wizard_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-040" in doc
    assert "GitHub #142" in doc
    assert "legacy Python Auto-Fix Wizard mutations" in doc
    assert "migration_fix_wizard.py" in doc
    assert "FixWizardWorker" in doc
    assert "GitHub #142 is fixed" in summary
    assert "Legacy Auto-Fix Wizard is dry-run/manual SQL only" in doc
    assert "No repo-side implementation issue is currently open" in order


def test_current_status_records_post_142_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())

    assert "Post-#142 next issue analysis" in doc
    assert "#116 is still the only open GitHub issue" in summary
    assert "python scripts\\check-macos-support-gate.py --final" in doc
    assert "no macOS manual validation report found under build" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc


def test_current_status_tracks_legacy_auto_fix_core_mutation_api_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-041" in doc
    assert "GitHub #143" in doc
    assert "legacy Auto-Fix core mutation APIs" in doc
    assert "BatchFixExecutor.execute_batch" in doc
    assert "FKSafeCharsetChanger.execute_safe_charset_change" in doc
    assert "GitHub #143 is fixed" in summary
    assert "dry-run/SQL generation remains available" in doc


def test_current_status_focused_verification_has_no_duplicate_check_rows():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    focused = _section(doc, "Focused Verification On 2026-06-27")
    commands = _check_row_commands(focused)

    assert len(commands) == len(set(commands))


def test_current_status_current_baseline_has_no_duplicate_check_rows():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    baseline = _section(doc, "Current Baseline Verification")
    commands = _check_row_commands(baseline)

    assert len(commands) == len(set(commands))
