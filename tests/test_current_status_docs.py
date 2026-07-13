import re
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
    assert "PASS, 1852 passed, 5 warnings" not in doc
    assert "PASS, 1865 passed, 5 warnings" not in doc
    assert "PASS, 1867 passed, 5 warnings" not in doc
    assert "PASS, 1869 passed, 5 warnings" not in doc
    assert "PASS, 1870 passed, 5 warnings" not in doc
    assert "PASS, 1871 passed, 5 warnings" not in doc
    assert "PASS, 1872 passed, 5 warnings" not in doc
    assert "PASS, 1873 passed, 5 warnings" not in doc
    assert "PASS, 1874 passed, 5 warnings" not in doc
    assert "PASS, 1875 passed, 5 warnings" not in doc
    assert "PASS, 1876 passed, 5 warnings" in doc
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
        "current full Python suite is `1865 passed, 5 warnings`",
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
        "current full Python suite is now `1852 passed, 5 warnings`",
        "current `1852 passed, 5 warnings` evidence",
        "now reports `1852 passed, 5 warnings`",
        "current full Python suite is `1852 passed, 5 warnings`",
        "current full Python suite is now `1857 passed, 5 warnings`",
        "current `1857 passed, 5 warnings` evidence",
        "now reports `1857 passed, 5 warnings`",
        "current full Python suite is `1857 passed, 5 warnings`",
        "current full Python suite is now `1860 passed, 5 warnings`",
        "current `1860 passed, 5 warnings` evidence",
        "now reports `1860 passed, 5 warnings`",
        "current full Python suite is `1860 passed, 5 warnings`",
        "current full Python suite is now `1861 passed, 5 warnings`",
        "current `1861 passed, 5 warnings` evidence",
        "now reports `1861 passed, 5 warnings`",
        "current full Python suite is `1861 passed, 5 warnings`",
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


def test_current_status_records_post_156_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-058" in doc
    assert "Post-#156 main merge and next issue analysis" in doc
    assert "`main` was already aligned with `origin/main`" in normalized_doc
    assert "#116 was still the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passed" in normalized_doc
    assert "older manual-workflow portion of that finding is superseded" in summary
    assert "current blocker is missing real-Mac report evidence" in summary
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "not a repo-side implementation issue" in normalized_doc


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


def test_current_status_tracks_postgresql_rust_dump_engine_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-063" in doc
    assert "GitHub #161" in doc
    assert "PostgreSQL Rust dump endpoint engine" in doc
    assert "RustDumpConfig" in doc
    assert "PostgresConnector" in doc
    assert "dump.run" in doc
    assert "dump.import" in doc
    assert "GitHub #161 is fixed" in summary
    assert "PostgreSQL Export/Import" in normalized_doc


def test_current_status_tracks_postgresql_import_timezone_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-064" in doc
    assert "GitHub #162" in doc
    assert "PostgreSQL Import timezone SQL" in doc
    assert "mysql.time_zone_name" in doc
    assert "SET SESSION time_zone" in doc
    assert "SET TIME ZONE" in doc
    assert "GitHub #162 is fixed" in summary
    assert "default auto timezone mode skips MySQL timezone table detection" in normalized_doc


def test_current_status_tracks_postgresql_import_timezone_core_validation_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-065" in doc
    assert "GitHub #163" in doc
    assert "PostgreSQL Import timezone Core validation" in doc
    assert "validated_timezone_sql" in doc
    assert "SET TIME ZONE" in doc
    assert "SET SESSION time_zone" in doc
    assert "GitHub #163 is fixed" in summary
    assert "Rust Core `dump.import` accepts PostgreSQL `SET TIME ZONE` timezone SQL" in normalized_doc


def test_current_status_tracks_postgresql_dump_wrapper_engine_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-066" in doc
    assert "GitHub #164" in doc
    assert "PostgreSQL dump wrapper engine" in doc
    assert "export_schema" in doc
    assert "export_tables" in doc
    assert "import_dump" in doc
    assert "RustDumpConfig" in doc
    assert "GitHub #164 is fixed" in summary
    assert "convenience wrappers preserve PostgreSQL engine" in normalized_doc


def test_current_status_tracks_scheduled_backup_postgresql_engine_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-067" in doc
    assert "GitHub #165" in doc
    assert "Scheduled PostgreSQL backup engine" in doc
    assert "_execute_backup" in doc
    assert "RustDumpConfig" in doc
    assert "SCHEDULE_FEATURE_ENABLED = False" in summary
    assert "GitHub #165 is fixed" in summary
    assert "preserves PostgreSQL tunnel engine metadata into `RustDumpConfig`" in normalized_doc
    assert "scheduled Rust dump backups now normalize tunnel `db_engine` metadata" in normalized_doc


def test_current_status_tracks_scheduled_backup_tuple_connection_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-068" in doc
    assert "GitHub #166" in doc
    assert "Scheduled backup tuple connection info" in doc
    assert "TunnelEngine.get_connection_info()" in doc
    assert "`(host, port)` tuple shape" in doc
    assert "config_manager.get_tunnel_credentials" in doc
    assert "RustDumpConfig" in doc
    assert "GitHub #166 is fixed" in summary
    assert "scheduled Rust dump backups now accept real `TunnelEngine.get_connection_info()` tuple output" in normalized_doc


def test_current_status_records_post_166_next_issue_reaudit():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-069" in doc
    assert "Post-#166 next issue re-audit" in doc
    assert "#116 was the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passed" in normalized_doc
    assert "older manual-workflow portion of that finding is superseded" in summary
    assert "current blocker is missing real-Mac report evidence" in summary
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "not a repo-side implementation issue" in normalized_doc


def test_current_status_records_manual_macos_workflow_evidence():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-070" in doc
    assert "manual macOS workflow evidence" in normalized_doc
    assert "then-current main HEAD" in summary
    assert "That evidence is historical in this document" in summary
    assert "including both `arm64` and `x86_64` jobs" in doc
    assert "no macOS manual validation report found under build/" in doc
    assert "GitHub #116 remains external" in summary
    assert "Do not hard-code exact current-head workflow run IDs or SHAs" in order


def test_current_status_tracks_non_self_stale_macos_workflow_evidence_policy():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-071" in doc
    assert "GitHub #167" in doc
    assert "current-head manual workflow evidence is tracked on GitHub #116 comments" in summary
    assert "scripts\\check-macos-support-gate.py --final" in summary
    assert "28264164795" not in summary
    assert "6ad09590bf14d678a568fd64ac74765fd1eff0c9" not in summary
    assert "Do not hard-code exact current-head workflow run IDs or SHAs" in order


def test_current_status_focused_final_gate_reason_matches_current_workflow_evidence():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    focused = _section(doc, "Focused Verification On 2026-06-27")
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-072" in doc
    assert "GitHub #168" in doc
    assert "current focused final-gate row now fails only for missing real-Mac report" in summary
    assert (
        "| `python scripts\\check-macos-support-gate.py --final` | "
        "EXPECTED FAIL, missing real-Mac report only |"
    ) in focused
    assert "current-HEAD manual workflow_dispatch evidence" not in focused


def test_current_status_summary_does_not_keep_superseded_missing_manual_workflow_wording():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-073" in doc
    assert "GitHub #169" in doc
    assert "missing real operator Mac validation report evidence" in summary
    assert "no successful manual macOS App Validation workflow_dispatch" not in summary
    assert "no successful manual `macOS App Validation` `workflow_dispatch`" not in summary
    assert "workflow_dispatch run exists for the current merged main HEAD" not in summary


def test_current_status_records_post_169_next_issue_reaudit():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-074" in doc
    assert "Post-#169 next issue re-audit" in doc
    assert "GitHub #116 is still the only open issue" in summary
    assert "no new repo-side implementation issue was found" in summary
    assert "Rust Core boundary and stale handoff scans" in summary
    assert "Current-head manual workflow evidence remains tracked on #116 comments" in summary


def test_current_status_records_macos_final_validation_tooling_recheck():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-075" in doc
    assert "macOS final validation tooling recheck" in doc
    assert "GitHub #116 final validation tooling was rechecked" in summary
    assert "macOS focused tests still pass at 53 passed" in summary
    assert "bash -n scripts/macos-manual-validation-report.sh" in doc
    assert "python scripts\\check-macos-support-gate.py --final" in doc
    assert "EXPECTED FAIL for `--final` only" in doc


def test_current_status_records_post_151_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-052" in doc
    assert "Post-#151 main merge and next issue analysis" in doc
    assert "main was aligned with origin/main before that status update" in normalized_doc
    assert "the status update was pushed to origin/main" in normalized_doc
    assert "#116 was still the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passed" in normalized_doc
    assert "older manual-workflow portion of that finding is superseded" in summary
    assert "current blocker is missing real-Mac report evidence" in summary
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "not a repo-side implementation issue" in normalized_doc


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


def test_current_status_records_post_142_next_issue_analysis():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())

    assert "Post-#142 next issue analysis" in doc
    assert "#116 was still the only open GitHub issue" in summary
    assert "older manual-workflow portion of that finding is superseded" in summary
    assert "current blocker is missing real-Mac report evidence" in summary
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
    assert "#116 was the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passed" in normalized_doc
    assert "older manual-workflow portion of that finding is superseded" in summary
    assert "current blocker is missing real-Mac report evidence" in summary
    assert "no macOS manual validation report found under build/" in doc
    assert "no successful manual macOS App Validation workflow_dispatch run found for current merged main HEAD" in doc
    assert "rather than a new repo-side implementation issue" in normalized_doc


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
    assert "#116 was the only open GitHub issue" in summary
    assert "normal repository-side #116 gate passed" in normalized_doc
    assert "older manual-workflow portion of that finding is superseded" in summary
    assert "current blocker is missing real-Mac report evidence" in summary
    assert "current merged main HEAD" in doc
    assert (
        "missing successful manual `macOS App Validation` workflow_dispatch evidence "
        "for the current merged main HEAD"
    ) in doc
    assert "not a repo-side implementation issue" in normalized_doc


def test_current_status_tracks_post_v217_version_drift_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    normalized_doc = " ".join(doc.split())
    summary = " ".join(_section(doc, "Summary").split())
    baseline = _section(doc, "Current Baseline Verification")
    version_source = (PROJECT_ROOT / "src" / "version.py").read_text(encoding="utf-8")
    current_version = re.search(r'__version__\s*=\s*"([^"]+)"', version_source).group(1)

    assert "TF-STATUS-049" in doc
    assert "GitHub #149" in doc
    assert "post-v2.1.7 version drift" in normalized_doc
    assert "v2.1.7" in doc
    assert "2.1.8" in doc
    assert "GitHub #149 is fixed" in summary
    assert f"Version references are aligned at `{current_version}`" in baseline


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


def test_current_status_distinguishes_open_170_from_remaining_implementation_work():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-078" in tracker
    assert "GitHub #170 remains open for issue hygiene only" in summary
    assert "PR #171" in summary
    assert "a4c7a06" in summary
    assert "close #170 after confirming the merged fix" in order


def test_current_status_records_post_round3_reconciliation_full_suite():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    baseline = _section(doc, "Current Baseline Verification")
    verification = _section(doc, "Verification Log")
    sessions = _section(doc, "Session Log")

    assert "post-strategy-review full Python suite at 1827 passed / 6 warnings" in summary
    assert "| `pytest -q` | PASS, 1827 passed, 6 warnings |" in baseline
    assert "full Python suite passed at 1827 passed / 6 warnings" in verification
    assert "full pytest 1827 passed / 6 warnings" in sessions


def test_current_status_records_231_release_candidate_verification_evidence():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    baseline = _section(doc, "Current Baseline Verification")
    verification = _section(doc, "Verification Log")
    sessions = _section(doc, "Session Log")

    assert "historical 1955-pass release-review snapshot is preserved" in summary
    assert "| `pytest -q` | PASS, 1955 passed, 1 skipped, 4 warnings, 60.38s, exit 0 |" in baseline
    assert "Rust gate: 1.4s" in verification
    assert "Cargo test: 216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored" in verification
    assert "Rust gate exit 0 in 1.4s" in sessions
    assert "Cargo test exit 0 in 4.1s" in sessions


def test_current_status_session_log_has_one_header_delimiter():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    sessions = _section(doc, "Session Log")

    assert sessions.count("| --- | --- | --- | --- |") == 1


def test_current_status_records_strategy_review_findings_and_priority():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    for issue_id in range(79, 84):
        assert f"TF-STATUS-{issue_id:03d}" in tracker

    assert "TF-STATUS-008 | Low | open" in tracker
    assert "Downloaded update packages are executed without an application-level" in summary
    assert "unset environments allow dangerous SQL without confirmation" in summary
    assert "unreleased post-release commits while still declaring the published version" in summary
    assert "scheduled backups while the UI feature flag is disabled" in summary
    assert "manual workflow evidence and the real-Mac report" in summary

    priorities = [
        "TF-STATUS-079",
        "TF-STATUS-080",
        "TF-STATUS-083",
        "TF-STATUS-082",
        "TF-STATUS-081",
        "TF-STATUS-008",
        "TF-STATUS-078",
    ]
    positions = [order.index(issue_id) for issue_id in priorities]
    assert positions == sorted(positions)


def test_current_status_records_231_release_candidate_handoff():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    version_source = (PROJECT_ROOT / "src" / "version.py").read_text(encoding="utf-8")
    source_version = re.search(r'__version__\s*=\s*"([^"]+)"', version_source).group(1)
    summary = " ".join(_section(doc, "Summary").split())
    baseline = " ".join(_section(doc, "Current Baseline Verification").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    verification = " ".join(_section(doc, "Verification Log").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())
    sessions = " ".join(_section(doc, "Session Log").split())

    assert source_version == "2.3.1"
    assert f"`{source_version}` release candidate" in summary
    assert f"Version references are aligned at `{source_version}`" in baseline

    assert "TF-STATUS-079 | High | closed" in tracker
    assert "TF-STATUS-080 | Medium | closed" in tracker
    assert "TF-STATUS-082 | Medium | closed" in tracker
    assert "TF-STATUS-081 | High | fixed_pending_full_verify" in tracker
    assert "TF-STATUS-083 | Medium | fixed_pending_full_verify" in tracker
    assert "TF-STATUS-008 | Low | open" in tracker
    assert "TF-STATUS-078 | Low | open" in tracker

    for phrase in [
        "GitHub Release asset `digest` verification",
        "unknown-environment confirmation",
        "`python-regression`",
        "bilingual Schedule correction",
        f"`{source_version}` release candidate",
    ]:
        assert phrase in verification

    assert "fixed_pending_full_verify" in order
    assert "RC merge/tag" in order
    assert "stable required-check promotion" in order
    assert "TF-STATUS-008" in order
    assert "TF-STATUS-078" in order
    assert "GitHub Release asset `digest` verification" in sessions
    assert "unknown-environment confirmation" in sessions


def test_current_status_closes_final_review_update_boundary_after_fresh_verification():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    baseline = " ".join(_section(doc, "Current Baseline Verification").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    verification = " ".join(_section(doc, "Verification Log").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())
    sessions = " ".join(_section(doc, "Session Log").split())

    assert "TF-STATUS-084 | High | closed" in tracker
    for finding in [
        "verification-to-launch lease",
        "owned cleanup/no-clobber",
        "cancellation generation",
        "streaming bound",
    ]:
        assert finding in tracker

    assert (
        "verified RC code baseline `c52f60e` "
        "on `feat/trust-release-sprint`; status-only history remains historical "
        "and does not alter the verified code baseline"
    ) in baseline
    assert "current HEAD `b35dde6`" not in baseline
    assert "b35dde6" not in baseline
    assert "automatic installer execution is disabled/reveal-only" in summary
    external_non_completion = (
        "This local verification does not claim completion of live Actions, "
        "branch protection promotion, tag/release, GitHub issue closure, or "
        "Mac hardware validation."
    )
    assert external_non_completion in summary
    assert "TF-STATUS-084" in verification
    assert "Fix E secure child creation/name validation" in verification
    assert "bootstrapper cancel-before-entry" in verification
    assert "319 passed, 1 skipped in 48.27s" in verification
    assert "2028 passed, 1 skipped, 4 warnings in 61.83s" in verification
    assert "Rust Core regression gate pass" in verification
    assert "216 lib, 2 JSONL CLI, 9 live, 2 stress passed / 1 ignored" in verification
    assert "Release build: 0.30s" in verification
    assert "Version sync: 1 passed in 0.09s" in verification
    assert "final diff check passed" in verification
    assert "| `git status --short --branch` | verified RC code baseline `c52f60e`" in baseline
    assert "| update/security/status/version focused pytest | PASS, 319 passed, 1 skipped in 48.27s, exit 0 |" in baseline
    assert "| `pytest -q` | PASS, 2028 passed, 1 skipped, 4 warnings, 61.83s, exit 0 |" in baseline
    assert "| `cargo build --manifest-path migration_core\\Cargo.toml --release` | PASS, 0.30s, exit 0 |" in baseline
    assert "| `pytest tests\\test_rust_core_packaging.py::test_release_version_files_are_in_sync -q` | PASS, 1 passed in 0.09s, exit 0 |" in baseline
    assert "TF-STATUS-084" in order
    assert "focused 319 passed / 1 skipped" in sessions
    assert "standalone full Python 2028 passed / 1 skipped / 4 warnings" in sessions


def test_current_status_closes_bootstrapper_cancel_publication_race():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    verification = " ".join(_section(doc, "Verification Log").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())
    sessions = " ".join(_section(doc, "Session Log").split())

    assert "TF-STATUS-086 is `closed`" in summary
    assert "TF-STATUS-086 | High | closed" in tracker
    assert "RED reproduced zero discard calls" in verification
    assert "PASS, 63 passed, exit 0; diff check pass" in verification
    assert "Keep TF-STATUS-086 closed" in order
    assert "synchronized bootstrapper abandonment/result publication" in sessions
    assert "final status suite 63 passed" in sessions


def test_current_status_closes_non_windows_reveal_only_wording_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-087 is `closed`" in summary
    assert "TF-STATUS-087 | Medium | closed" in tracker
    assert "Keep TF-STATUS-087 closed" in order
    assert "PASS, 64 passed, exit 0; diff check pass" in doc


def test_current_status_closes_cross_platform_update_cleanup_after_broad_verification():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    baseline = " ".join(_section(doc, "Current Baseline Verification").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    verification = " ".join(_section(doc, "Verification Log").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())
    sessions = " ".join(_section(doc, "Session Log").split())

    assert "TF-STATUS-085 | High | closed" in tracker
    assert "verified RC code baseline `c52f60e`" in baseline
    assert "verified code baseline `87d9021`" in verification
    assert "TUNNELFORGE_WEBSETUP_SELF_CHECK_OK" in verification
    assert "PASS, 62 passed, exit 0; diff check pass" in verification
    assert "Keep TF-STATUS-085 closed" in order
    assert "Closed TF-STATUS-085" in sessions
    assert "final status suite 62 passed" in sessions


def test_current_status_refresh_preserves_historical_test_snapshots_and_statuses():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    tracker = " ".join(_section(doc, "Issue Tracker").split())

    for snapshot in [
        "1827 passed, 6 warnings",
        "1870 passed, 4 warnings",
        "1955 passed, 1 skipped, 4 warnings",
    ]:
        assert snapshot in doc

    for status in [
        "TF-STATUS-079 | High | closed",
        "TF-STATUS-080 | Medium | closed",
        "TF-STATUS-081 | High | fixed_pending_full_verify",
        "TF-STATUS-082 | Medium | closed",
        "TF-STATUS-083 | Medium | fixed_pending_full_verify",
        "TF-STATUS-008 | Low | open",
        "TF-STATUS-078 | Low | open",
    ]:
        assert status in tracker


def test_current_status_closes_version_gate_trust_boundary_issue():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    verification = " ".join(_section(doc, "Verification Log").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-088 is `closed`" in summary
    assert "TF-STATUS-088 | High | closed" in tracker
    assert "Commit-message bypass is removed" in verification
    assert "Keep TF-STATUS-088 closed" in order
    assert "PASS, 65 passed, exit 0; diff check pass" in verification


def test_current_status_tracks_release_approval_and_external_release_blockers():
    doc = (PROJECT_ROOT / "docs" / "current_status.md").read_text(encoding="utf-8")
    summary = " ".join(_section(doc, "Summary").split())
    tracker = " ".join(_section(doc, "Issue Tracker").split())
    verification = " ".join(_section(doc, "Verification Log").split())
    order = " ".join(_section(doc, "Recommended Execution Order").split())

    assert "TF-STATUS-089 | High | fixed_pending_full_verify" in tracker
    assert "TF-STATUS-090 | High | blocked" in tracker
    assert "TF-STATUS-091 | Medium | blocked" in tracker
    assert "required reviewer, admin bypass disabled" in summary
    assert "active ruleset prevents `v*` tag update/deletion/non-fast-forward" in summary
    assert "2028 passed, 1 skipped, 4 warnings in 61.83s" in verification
    assert "Security re-review: SECURE / APPROVE" in doc
    assert order.index("TF-STATUS-090") < order.index("TF-STATUS-089")
    assert order.index("TF-STATUS-089") < order.index("TF-STATUS-091")
