"""
migration_report.py 단위 테스트

ReportExporter 검증 — JSON, CSV, MySQL Shell, SQL, HTML 출력 및 파일 저장.
DB 의존성 없음. CompatibilityIssue 객체를 직접 생성하여 테스트.
"""
import csv
import io
import json
from pathlib import Path

import pytest

from src.core.migration_constants import IssueType, CompatibilityIssue
from src.core.migration_report import ReportExporter


# ============================================================
# 헬퍼
# ============================================================
def make_issue(
    issue_type=IssueType.CHARSET_ISSUE,
    severity="warning",
    location="testdb.users",
    description="Test issue",
    suggestion="Fix it",
    fix_query=None,
    doc_link=None,
    table_name=None,
    column_name=None,
):
    return CompatibilityIssue(
        issue_type=issue_type,
        severity=severity,
        location=location,
        description=description,
        suggestion=suggestion,
        fix_query=fix_query,
        doc_link=doc_link,
        table_name=table_name,
        column_name=column_name,
    )


@pytest.fixture
def empty_exporter():
    return ReportExporter([])


@pytest.fixture
def single_exporter():
    return ReportExporter([make_issue()])


@pytest.fixture
def mixed_exporter():
    return ReportExporter([
        make_issue(severity="error", issue_type=IssueType.CHARSET_ISSUE),
        make_issue(severity="warning", issue_type=IssueType.INVALID_DATE),
        make_issue(severity="warning", issue_type=IssueType.INVALID_DATE),
        make_issue(severity="info", issue_type=IssueType.INT_DISPLAY_WIDTH),
    ])


@pytest.fixture
def fixable_exporter():
    return ReportExporter([
        make_issue(
            issue_type=IssueType.CHARSET_ISSUE,
            fix_query="ALTER TABLE users CONVERT TO CHARACTER SET utf8mb4;",
        ),
        make_issue(
            issue_type=IssueType.CHARSET_ISSUE,
            location="testdb.orders",
            fix_query="ALTER TABLE orders CONVERT TO CHARACTER SET utf8mb4;",
        ),
        make_issue(
            issue_type=IssueType.INVALID_DATE,
            fix_query=None,  # 수정 불가
        ),
    ])


# ============================================================
# summary 프로퍼티
# ============================================================
class TestSummary:
    def test_empty_summary(self, empty_exporter):
        s = empty_exporter.summary
        assert s == {"total": 0, "error": 0, "warning": 0, "info": 0}

    def test_single_warning(self, single_exporter):
        s = single_exporter.summary
        assert s["total"] == 1
        assert s["warning"] == 1
        assert s["error"] == 0
        assert s["info"] == 0

    def test_mixed_summary(self, mixed_exporter):
        s = mixed_exporter.summary
        assert s["total"] == 4
        assert s["error"] == 1
        assert s["warning"] == 2
        assert s["info"] == 1


# ============================================================
# issues_by_type 프로퍼티
# ============================================================
class TestIssuesByType:
    def test_empty(self, empty_exporter):
        assert empty_exporter.issues_by_type == {}

    def test_single(self, single_exporter):
        by_type = single_exporter.issues_by_type
        assert IssueType.CHARSET_ISSUE.value in by_type
        assert by_type[IssueType.CHARSET_ISSUE.value] == 1

    def test_multiple_same_type(self, mixed_exporter):
        by_type = mixed_exporter.issues_by_type
        assert by_type.get(IssueType.INVALID_DATE.value) == 2

    def test_mixed_types(self, mixed_exporter):
        by_type = mixed_exporter.issues_by_type
        assert len(by_type) == 3  # charset_issue, invalid_date, int_display_width


# ============================================================
# export_json
# ============================================================
class TestExportJson:
    def test_valid_json(self, mixed_exporter):
        result = mixed_exporter.export_json()
        data = json.loads(result)  # 파싱 성공해야 함
        assert isinstance(data, dict)

    def test_required_keys(self, mixed_exporter):
        data = json.loads(mixed_exporter.export_json())
        assert "version" in data
        assert "tool" in data
        assert "generated_at" in data
        assert "summary" in data
        assert "issues_by_type" in data
        assert "issues" in data

    def test_issues_list(self, mixed_exporter):
        data = json.loads(mixed_exporter.export_json())
        assert len(data["issues"]) == 4

    def test_issue_required_fields(self, single_exporter):
        data = json.loads(single_exporter.export_json())
        issue = data["issues"][0]
        assert "type" in issue
        assert "severity" in issue
        assert "location" in issue
        assert "description" in issue
        assert "suggestion" in issue

    def test_fix_query_included_by_default(self, fixable_exporter):
        data = json.loads(fixable_exporter.export_json())
        issues_with_fix = [i for i in data["issues"] if "fix_query" in i]
        assert len(issues_with_fix) == 2

    def test_fix_query_excluded(self, fixable_exporter):
        data = json.loads(fixable_exporter.export_json(include_fix_queries=False))
        assert all("fix_query" not in i for i in data["issues"])

    def test_doc_link_included(self):
        exporter = ReportExporter([
            make_issue(doc_link="https://dev.mysql.com/doc/relnotes/mysql/8.4/en/")
        ])
        data = json.loads(exporter.export_json())
        assert "doc_link" in data["issues"][0]

    def test_table_name_included(self):
        exporter = ReportExporter([make_issue(table_name="users")])
        data = json.loads(exporter.export_json())
        assert data["issues"][0].get("table_name") == "users"

    def test_column_name_included(self):
        exporter = ReportExporter([make_issue(column_name="created_at")])
        data = json.loads(exporter.export_json())
        assert data["issues"][0].get("column_name") == "created_at"

    def test_empty_issues(self, empty_exporter):
        data = json.loads(empty_exporter.export_json())
        assert data["issues"] == []
        assert data["summary"]["total"] == 0

    def test_summary_in_json(self, mixed_exporter):
        data = json.loads(mixed_exporter.export_json())
        assert data["summary"]["total"] == 4
        assert data["summary"]["error"] == 1


# ============================================================
# export_csv
# ============================================================
class TestExportCsv:
    def test_has_header(self, single_exporter):
        csv_text = single_exporter.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        header = next(reader)
        assert "Type" in header
        assert "Severity" in header
        assert "Location" in header

    def test_correct_row_count(self, mixed_exporter):
        csv_text = mixed_exporter.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 5  # 1 header + 4 data

    def test_empty_csv(self, empty_exporter):
        csv_text = empty_exporter.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        rows = list(reader)
        assert len(rows) == 1  # header only

    def test_fix_query_in_row(self):
        exporter = ReportExporter([
            make_issue(fix_query="ALTER TABLE t CONVERT TO CHARACTER SET utf8mb4;")
        ])
        csv_text = exporter.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        next(reader)  # skip header
        row = next(reader)
        fix_query_idx = 5  # 'Fix Query' column
        assert "ALTER TABLE" in row[fix_query_idx]

    def test_empty_optional_fields(self, single_exporter):
        csv_text = single_exporter.export_csv()
        reader = csv.reader(io.StringIO(csv_text))
        next(reader)  # skip header
        row = next(reader)
        # fix_query, doc_link, table, column fields should be empty
        assert row[5] == ""  # fix_query
        assert row[6] == ""  # doc_link


# ============================================================
# export_mysql_shell
# ============================================================
class TestExportMysqlShell:
    def test_header_present(self, mixed_exporter):
        text = mixed_exporter.export_mysql_shell()
        assert "MySQL Server Upgrade Compatibility Check" in text

    def test_source_path_default(self, mixed_exporter):
        text = mixed_exporter.export_mysql_shell()
        assert "dump-analysis" in text

    def test_custom_source_path(self, mixed_exporter):
        text = mixed_exporter.export_mysql_shell(source_path="/data/backup")
        assert "/data/backup" in text

    def test_summary_section(self, mixed_exporter):
        text = mixed_exporter.export_mysql_shell()
        assert "Summary" in text
        assert "Errors:" in text
        assert "Warnings:" in text

    def test_error_message_when_errors(self):
        exporter = ReportExporter([make_issue(severity="error")])
        text = exporter.export_mysql_shell()
        assert "errors that need to be fixed" in text

    def test_warning_message_when_warnings_only(self):
        exporter = ReportExporter([make_issue(severity="warning")])
        text = exporter.export_mysql_shell()
        assert "warnings" in text.lower()

    def test_clean_message_when_no_issues(self, empty_exporter):
        text = empty_exporter.export_mysql_shell()
        assert "No issues found" in text

    def test_doc_link_included(self):
        exporter = ReportExporter([
            make_issue(doc_link="https://dev.mysql.com/doc/")
        ])
        text = exporter.export_mysql_shell()
        assert "https://dev.mysql.com/doc/" in text

    def test_location_shown_in_output(self, single_exporter):
        text = single_exporter.export_mysql_shell()
        assert "testdb.users" in text

    def test_large_group_truncated(self):
        """11개 이상 이슈는 ... and N more 표시"""
        issues = [make_issue(location=f"db.t{i}") for i in range(15)]
        exporter = ReportExporter(issues)
        text = exporter.export_mysql_shell()
        assert "more" in text


# ============================================================
# _group_by_check_id
# ============================================================
class TestGroupByCheckId:
    def test_groups_by_issue_type(self):
        exporter = ReportExporter([
            make_issue(issue_type=IssueType.CHARSET_ISSUE),
            make_issue(issue_type=IssueType.CHARSET_ISSUE),
            make_issue(issue_type=IssueType.INVALID_DATE),
        ])
        grouped = exporter._group_by_check_id()
        # CHARSET_ISSUE 2개, INVALID_DATE 1개로 그룹화
        total = sum(len(v) for v in grouped.values())
        assert total == 3

    def test_empty(self, empty_exporter):
        grouped = empty_exporter._group_by_check_id()
        assert grouped == {}

    def test_mysql_shell_check_id_attr(self):
        """mysql_shell_check_id 속성이 있으면 우선 사용"""
        issue = make_issue()
        issue.mysql_shell_check_id = "customCheckId"

        exporter = ReportExporter([issue])
        grouped = exporter._group_by_check_id()
        assert "customCheckId" in grouped


# ============================================================
# export_fix_queries_sql
# ============================================================
class TestExportFixQueriesSql:
    def test_no_fixable_issues(self, single_exporter):
        result = single_exporter.export_fix_queries_sql()
        assert "No fixable issues" in result

    def test_has_sql_header(self, fixable_exporter):
        result = fixable_exporter.export_fix_queries_sql()
        assert "Fix Queries" in result

    def test_contains_fix_queries(self, fixable_exporter):
        result = fixable_exporter.export_fix_queries_sql()
        assert "ALTER TABLE users" in result
        assert "ALTER TABLE orders" in result

    def test_non_fixable_excluded(self, fixable_exporter):
        result = fixable_exporter.export_fix_queries_sql()
        # fix_query=None인 INVALID_DATE 이슈의 내용이 없어야 함
        # (단, type명은 나올 수 있으니 fix_query 내용 기준으로 체크)
        assert result.count("ALTER TABLE") == 2

    def test_has_transaction_comments(self, fixable_exporter):
        result = fixable_exporter.export_fix_queries_sql()
        assert "START TRANSACTION" in result
        assert "COMMIT" in result
        assert "ROLLBACK" in result

    def test_grouped_by_type(self, fixable_exporter):
        result = fixable_exporter.export_fix_queries_sql()
        # CHARSET_ISSUE 타입 섹션이 있어야 함
        assert "CHARSET" in result.upper()

    def test_fix_description_in_comment(self):
        exporter = ReportExporter([
            make_issue(
                description="This is a short description",
                fix_query="ALTER TABLE t CONVERT TO CHARACTER SET utf8mb4;",
            )
        ])
        result = exporter.export_fix_queries_sql()
        assert "This is a short description" in result

    def test_long_description_truncated(self):
        long_desc = "A" * 100
        exporter = ReportExporter([
            make_issue(
                description=long_desc,
                fix_query="ALTER TABLE t CONVERT TO CHARACTER SET utf8mb4;",
            )
        ])
        result = exporter.export_fix_queries_sql()
        assert "..." in result


# ============================================================
# export_html
# ============================================================
class TestExportHtml:
    def test_valid_html_structure(self, mixed_exporter):
        html = mixed_exporter.export_html()
        assert "<!DOCTYPE html>" in html
        assert "<html" in html
        assert "</html>" in html

    def test_title(self, mixed_exporter):
        html = mixed_exporter.export_html()
        assert "MySQL 8.4 Upgrade Check Report" in html

    def test_summary_section(self, mixed_exporter):
        html = mixed_exporter.export_html()
        assert "Errors: 1" in html
        assert "Warnings: 2" in html

    def test_issue_rows_present(self, mixed_exporter):
        html = mixed_exporter.export_html()
        assert html.count("<tr>") > 1  # 헤더 + 데이터 행

    def test_empty_table(self, empty_exporter):
        html = empty_exporter.export_html()
        assert "Total: 0" in html

    def test_issue_location_in_html(self, single_exporter):
        html = single_exporter.export_html()
        assert "testdb.users" in html

    def test_severity_class(self, mixed_exporter):
        html = mixed_exporter.export_html()
        assert 'class="error"' in html
        assert 'class="warning"' in html


# ============================================================
# save_to_file
# ============================================================
class TestSaveToFile:
    def test_save_json(self, mixed_exporter, tmp_path):
        filepath = str(tmp_path / "report.json")
        result = mixed_exporter.save_to_file(filepath, "json")
        assert result == filepath
        assert Path(filepath).exists()
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        assert "issues" in data

    def test_save_csv(self, mixed_exporter, tmp_path):
        filepath = str(tmp_path / "report.csv")
        mixed_exporter.save_to_file(filepath, "csv")
        content = Path(filepath).read_text(encoding="utf-8")
        assert "Type" in content

    def test_save_mysql_shell(self, mixed_exporter, tmp_path):
        filepath = str(tmp_path / "report.txt")
        mixed_exporter.save_to_file(filepath, "mysql_shell")
        content = Path(filepath).read_text(encoding="utf-8")
        assert "MySQL Server Upgrade" in content

    def test_save_sql(self, fixable_exporter, tmp_path):
        filepath = str(tmp_path / "fixes.sql")
        fixable_exporter.save_to_file(filepath, "sql")
        content = Path(filepath).read_text(encoding="utf-8")
        assert "ALTER TABLE" in content

    def test_save_html(self, mixed_exporter, tmp_path):
        filepath = str(tmp_path / "report.html")
        mixed_exporter.save_to_file(filepath, "html")
        content = Path(filepath).read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content

    def test_creates_parent_dirs(self, mixed_exporter, tmp_path):
        filepath = str(tmp_path / "nested" / "deep" / "report.json")
        mixed_exporter.save_to_file(filepath, "json")
        assert Path(filepath).exists()

    def test_unknown_format_raises(self, mixed_exporter, tmp_path):
        with pytest.raises(ValueError, match="Unknown format"):
            mixed_exporter.save_to_file(str(tmp_path / "x.xyz"), "xyz")


# ============================================================
# save_all_formats
# ============================================================
class TestSaveAllFormats:
    def test_saves_all_5_formats(self, mixed_exporter, tmp_path):
        saved = mixed_exporter.save_all_formats(str(tmp_path))
        assert set(saved.keys()) == {"json", "csv", "mysql_shell", "sql", "html"}

    def test_all_files_exist(self, mixed_exporter, tmp_path):
        saved = mixed_exporter.save_all_formats(str(tmp_path))
        for fmt, path in saved.items():
            assert Path(path).exists(), f"{fmt} file not found: {path}"

    def test_creates_base_dir(self, mixed_exporter, tmp_path):
        base = str(tmp_path / "reports" / "output")
        mixed_exporter.save_all_formats(base)
        assert Path(base).is_dir()

    def test_filenames_have_timestamp(self, mixed_exporter, tmp_path):
        saved = mixed_exporter.save_all_formats(str(tmp_path))
        for path in saved.values():
            filename = Path(path).name
            # 파일명에 숫자(타임스탬프) 포함
            assert any(c.isdigit() for c in filename)


# ============================================================
# 미커버 경로 추가 테스트
# ============================================================
class TestUncoveredPaths:
    # --- report.py line 72: code_snippet 속성 포함 ---
    def test_export_json_includes_code_snippet(self):
        """issue에 code_snippet 속성이 있으면 JSON에 'code' 키로 포함"""
        issue = make_issue()
        issue.code_snippet = "SELECT * FROM users WHERE id = 0000-00-00"
        exporter = ReportExporter([issue])
        data = json.loads(exporter.export_json())
        assert data["issues"][0].get("code") == issue.code_snippet

    def test_export_json_no_code_snippet_attr(self):
        """code_snippet 속성이 없으면 'code' 키 포함 안 됨"""
        issue = make_issue()
        # CompatibilityIssue는 code_snippet 속성이 없을 수도 있음
        exporter = ReportExporter([issue])
        data = json.loads(exporter.export_json())
        # code_snippet이 없거나 None이면 'code' 키 없어야 함
        # (있더라도 None이면 포함 안 됨)
        assert "code" not in data["issues"][0] or data["issues"][0]["code"] is None

    # --- report.py line 187: issue_type 없는 'unknown' check_id 경로 ---
    def test_group_by_check_id_unknown_fallback(self):
        """issue_type 속성이 없는 이슈는 'unknown'으로 그룹화"""
        from types import SimpleNamespace
        # issue_type 속성 없음, mysql_shell_check_id도 없음
        issue = SimpleNamespace(
            severity="warning",
            location="db.t",
            description="mystery",
            suggestion="fix",
        )
        exporter = ReportExporter([issue])
        grouped = exporter._group_by_check_id()
        assert "unknown" in grouped
        assert len(grouped["unknown"]) == 1
