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
    assert "PASS, 1816 passed, 5 warnings" not in doc
    assert "PASS, 1820 passed, 5 warnings" not in doc
    assert "PASS, 1822 passed, 5 warnings" not in doc
    assert "PASS, 1823 passed, 5 warnings" not in doc
    assert "PASS, 1825 passed, 5 warnings" not in doc
    assert "PASS, 1826 passed, 5 warnings" not in doc
    assert "PASS, 1827 passed, 5 warnings" not in doc
    assert "PASS, 1830 passed, 5 warnings" not in doc
    assert "PASS, 1832 passed, 5 warnings" not in doc
    assert "PASS, 1834 passed, 5 warnings" not in doc
    assert "PASS, 1835 passed, 5 warnings" not in doc
    assert "PASS, 1837 passed, 5 warnings" not in doc
    assert "PASS, 1845 passed, 5 warnings" not in doc
    assert "PASS, 1846 passed, 5 warnings" not in doc
    assert "PASS, 1847 passed, 5 warnings" not in doc
    assert "PASS, 1849 passed, 5 warnings" not in doc
    assert "PASS, 1850 passed, 5 warnings" not in doc
    assert "PASS, 1852 passed, 5 warnings" in doc
    assert "Current main full Python suite" in doc


def test_current_status_does_not_describe_stale_full_pytest_count_as_current():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())

    stale_current_phrases = [
        "current full Python suite is now `1830 passed, 5 warnings`",
        "current `1830 passed, 5 warnings` evidence",
        "now reports `1830 passed, 5 warnings`",
        "current full Python suite is now `1832 passed, 5 warnings`",
        "current `1832 passed, 5 warnings` evidence",
        "now reports `1832 passed, 5 warnings`",
        "current full Python suite is now `1834 passed, 5 warnings`",
        "current `1834 passed, 5 warnings` evidence",
        "now reports `1834 passed, 5 warnings`",
        "current full Python suite is now `1835 passed, 5 warnings`",
        "current `1835 passed, 5 warnings` evidence",
        "now reports `1835 passed, 5 warnings`",
        "current full Python suite is now `1837 passed, 5 warnings`",
        "current `1837 passed, 5 warnings` evidence",
        "now reports `1837 passed, 5 warnings`",
        "current full Python suite is now `1839 passed, 5 warnings`",
        "current `1839 passed, 5 warnings` evidence",
        "now reports `1839 passed, 5 warnings`",
        "current full Python suite is now `1843 passed, 5 warnings`",
        "current `1843 passed, 5 warnings` evidence",
        "now reports `1843 passed, 5 warnings`",
        "current full Python suite is now `1845 passed, 5 warnings`",
        "current `1845 passed, 5 warnings` evidence",
        "now reports `1845 passed, 5 warnings`",
        "current full Python suite is now `1846 passed, 5 warnings`",
        "current `1846 passed, 5 warnings` evidence",
        "now reports `1846 passed, 5 warnings`",
        "current full Python suite is now `1847 passed, 5 warnings`",
        "current `1847 passed, 5 warnings` evidence",
        "now reports `1847 passed, 5 warnings`",
        "current full Python suite is now `1849 passed, 5 warnings`",
        "current `1849 passed, 5 warnings` evidence",
        "now reports `1849 passed, 5 warnings`",
        "current full Python suite is `1849 passed, 5 warnings`",
        "current full Python suite is now `1850 passed, 5 warnings`",
        "current `1850 passed, 5 warnings` evidence",
        "now reports `1850 passed, 5 warnings`",
        "current full Python suite is `1850 passed, 5 warnings`",
    ]

    for phrase in stale_current_phrases:
        assert phrase not in normalized_doc


def test_current_status_tracks_post_151_full_pytest_refresh_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-053" in doc
    assert "GitHub #152" in doc
    assert "post-#151 full-suite evidence refresh" in normalized_doc
    assert "1839-test suite evidence" in doc
    assert "GitHub #152 is fixed" in summary
    assert "that count is now superseded by TF-STATUS-057 full-suite evidence" in normalized_doc


def test_current_status_tracks_rust_core_dml_rowcount_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-054" in doc
    assert "GitHub #153" in doc
    assert "Rust Core DML affected row counts" in doc
    assert "rows_affected" in doc
    assert "RustDbCursor.rowcount" in doc
    assert "GitHub #153 is fixed" in summary
    assert "scheduled SQL and SQL editor DML reporting" in normalized_doc


def test_current_status_tracks_call_local_rowcount_metadata_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-055" in doc
    assert "GitHub #154" in doc
    assert "call-local affected-row metadata" in normalized_doc
    assert "execute_on_connection_result" in doc
    assert "execute_query_result" in doc
    assert "GitHub #154 is fixed" in summary
    assert "shared facade state" in normalized_doc


def test_current_status_tracks_sql_statement_parser_mismatch_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-056" in doc
    assert "GitHub #155" in doc
    assert "SQL statement parser mismatch" in doc
    assert "SQL Editor" in doc
    assert "SQLExecutionWorker._parse_sql_statements" in doc
    assert "PostgreSQL dollar quote" in normalized_doc
    assert "MySQL DELIMITER" in normalized_doc
    assert "GitHub #155 is fixed" in summary
    assert "src/core/sql_statement_parser.py" in doc
    assert "find_sql_statement_at_position" in doc
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_tracks_dollar_quote_helper_guard_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-057" in doc
    assert "GitHub #156" in doc
    assert "SQL dollar quote helper guard" in doc
    assert "read_dollar_quote" in doc
    assert "out-of-range" in normalized_doc
    assert "GitHub #156 is fixed" in summary
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_records_post_156_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-058" in doc
    assert "Post-#156 main merge and next issue analysis" in doc
    assert "`main` was already aligned with `origin/main`" in normalized_doc
    assert "#116 is still the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passes" in normalized_doc
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "not a repo-side implementation issue" in normalized_doc
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_tracks_oneclick_next_action_wording_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-059" in doc
    assert "GitHub #157" in doc
    assert "One-Click readiness next-action wording" in doc
    assert "Recommended next repo-side change" in normalized_doc
    assert "GitHub #157 is fixed" in summary
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_tracks_dollar_quote_none_input_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-060" in doc
    assert "GitHub #158" in doc
    assert "SQL dollar quote helper None input" in doc
    assert "read_dollar_quote(None, 0)" in doc
    assert "SQLExecutionWorker._read_dollar_quote(None, 0)" in doc
    assert "GitHub #158 is fixed" in summary
    assert "fail-closed" in normalized_doc
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_tracks_partial_export_fk_parent_rust_inspect_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-062" in doc
    assert "GitHub #160" in doc
    assert "Partial export FK parent resolution" in doc
    assert "RustDumpExporter.export_tables" in doc
    assert "schema.inspect" in doc
    assert "MySQLConnector" in doc
    assert "GitHub #160 is fixed" in summary
    assert "Rust Core-owned schema inspection" in normalized_doc
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_records_post_151_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-052" in doc
    assert "Post-#151 main merge and next issue analysis" in doc
    assert "main was aligned with origin/main before this status update" in normalized_doc
    assert "this status update was pushed to origin/main" in normalized_doc
    assert "#116 is still the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passes" in normalized_doc
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "not a repo-side implementation issue" in normalized_doc
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_current_baseline_section_is_not_stale_dated():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    headings = [line for line in doc.splitlines() if line.startswith("## ")]

    assert "## Current Baseline Verification" in doc
    assert "## Verified On 2026-06-26" not in headings
    assert "Full-suite count refreshed on 2026-06-27" in doc


def test_current_status_baseline_provenance_uses_latest_status_update():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    top_handoff = doc.split("## Issue Tracker", maxsplit=1)[0]
    baseline = _section(doc, "Current Baseline Verification")

    assert "post-#156 next-issue analysis regression coverage" not in top_handoff
    assert "no local changes before post-#156 re-analysis" not in baseline
    assert "latest status update" in baseline
    assert "TF-STATUS-061" in doc
    assert "GitHub #159" in doc


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
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


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


def test_current_status_tracks_legacy_migration_analyzer_cleanup_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-042" in doc
    assert "GitHub #144" in doc
    assert "legacy MigrationAnalyzer cleanup mutations" in normalized_doc
    assert "MigrationAnalyzer.execute_cleanup" in doc
    assert "CleanupWorker" in doc
    assert "GitHub #144 is fixed" in summary
    assert "Dry-Run and SQL preview remain available" in doc


def test_current_status_tracks_legacy_cleanup_worker_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-043" in doc
    assert "GitHub #145" in doc
    assert "legacy CleanupWorker actual cleanup mode" in normalized_doc
    assert "CleanupWorker(..., dry_run=False)" in doc
    assert "GitHub #145 is fixed" in summary
    assert "Dry-run cleanup worker construction remains available" in doc


def test_current_status_tracks_legacy_execute_many_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-044" in doc
    assert "GitHub #146" in doc
    assert "legacy MySQLConnector execute_many mutation helper" in normalized_doc
    assert "MySQLConnector.execute_many" in doc
    assert "GitHub #146 is fixed" in summary
    assert "read/query helper behavior is unchanged" in doc


def test_current_status_records_post_146_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-045" in doc
    assert "Post-#146 next issue analysis" in doc
    assert "#116 is still the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passes" in normalized_doc
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "not a new repo-side implementation issue" in normalized_doc
    assert "No repo-side implementation issue is currently open after TF-STATUS-062" in order


def test_current_status_tracks_post_release_version_drift_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-046" in doc
    assert "GitHub #147" in doc
    assert "post-release version drift" in normalized_doc
    assert "v2.1.6" in doc
    assert "2.1.7" in doc
    assert "GitHub #147 is fixed" in summary
    assert "next unreleased source version to `2.1.7`" in summary


def test_current_status_tracks_v217_release_publication_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-047" in doc
    assert "GitHub #148" in doc
    assert "v2.1.7 release publication" in normalized_doc
    assert "28255274238" in doc
    assert "TunnelForge-Setup-2.1.7.exe" in doc
    assert "TunnelForge-macOS-2.1.7-arm64.dmg" in doc
    assert "TunnelForge-macOS-2.1.7-x86_64.dmg" in doc
    assert "GitHub #148 is fixed" in summary
    assert "#116 remains external" in order


def test_current_status_records_post_148_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-048" in doc
    assert "Post-#148 next issue analysis" in doc
    assert "#116 is the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passes" in normalized_doc
    assert "current merged main HEAD" in doc
    assert "no successful manual `macOS App Validation` `workflow_dispatch` run" in doc
    assert "not a repo-side implementation issue" in normalized_doc


def test_current_status_tracks_post_v217_version_drift_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-049" in doc
    assert "GitHub #149" in doc
    assert "post-v2.1.7 version drift" in normalized_doc
    assert "v2.1.7" in doc
    assert "2.1.8" in doc
    assert "GitHub #149 is fixed" in summary
    assert "Version references are aligned at `2.1.8`" in doc


def test_current_status_tracks_rust_db_cursor_executemany_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())

    assert "TF-STATUS-050" in doc
    assert "GitHub #150" in doc
    assert "RustDbCursor executemany batch helper" in normalized_doc
    assert "RustDbCursor.executemany" in doc
    assert "GitHub #150 is fixed" in summary
    assert "batch db operations must be modeled as explicit rust core commands" in normalized_doc.lower()


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
