"""
migration_report_renderer.py 단위 테스트

MigrationReport + MigrationReportRenderer 검증 — HTML/JSON export,
악성 문자열에 대한 HTML 이스케이프, dict/object 이슈 혼용 지원.
DB 의존성 없음.
"""
import json

import pytest

from src.core.migration_constants import IssueType, CompatibilityIssue
from src.core.migration_report_renderer import MigrationReport, MigrationReportRenderer


def make_report(**overrides):
    defaults = dict(
        schema="testdb",
        started_at="2026-01-01T00:00:00",
        completed_at="2026-01-01T00:05:00",
        pre_issue_count=3,
        post_issue_count=1,
        fixed_issues=[],
        remaining_issues=[],
        new_issues=[],
        success=True,
        execution_log=[],
        duration_seconds=12.5,
    )
    defaults.update(overrides)
    return MigrationReport(**defaults)


# ============================================================
# MigrationReport.get_summary
# ============================================================
class TestMigrationReportGetSummary:
    def test_get_summary_fields(self):
        report = make_report(
            fixed_issues=[{"location": "a"}],
            remaining_issues=[{"location": "b"}],
            new_issues=[],
        )
        summary = report.get_summary()
        assert summary["schema"] == "testdb"
        assert summary["success"] is True
        assert summary["fixed_count"] == 1
        assert summary["remaining_count"] == 1
        assert summary["new_count"] == 0
        assert summary["duration_seconds"] == 12.5


# ============================================================
# export_report_json
# ============================================================
class TestExportReportJson:
    def test_contains_summary_issues_log(self, tmp_path):
        report = make_report(
            fixed_issues=[{
                "issue_type": "charset_issue", "severity": "warning",
                "location": "t", "message": "m", "suggestion": "s",
            }],
            execution_log=["step 1 done", "step 2 done"],
        )
        output = tmp_path / "report.json"
        MigrationReportRenderer().export_report_json(report, str(output))

        data = json.loads(output.read_text(encoding="utf-8"))
        assert "summary" in data
        assert "fixed_issues" in data
        assert "remaining_issues" in data
        assert "new_issues" in data
        assert data["execution_log"] == ["step 1 done", "step 2 done"]
        assert data["summary"]["fixed_count"] == 1

    def test_dict_issue_serializes(self, tmp_path):
        """Rust DB Core가 보내는 dict 형태 이슈가 올바르게 직렬화되는지"""
        report = make_report(remaining_issues=[{
            "issue_type": "charset_issue",
            "severity": "error",
            "location": "db.t",
            "message": "bad charset",
            "suggestion": "convert",
            "table_name": "t",
        }])
        output = tmp_path / "report.json"
        MigrationReportRenderer().export_report_json(report, str(output))

        issue = json.loads(output.read_text(encoding="utf-8"))["remaining_issues"][0]
        assert issue["type"] == "charset_issue"
        assert issue["severity"] == "error"
        assert issue["location"] == "db.t"
        assert issue["description"] == "bad charset"
        assert issue["table_name"] == "t"

    def test_object_issue_serializes(self, tmp_path):
        """CompatibilityIssue 같은 object/dataclass 이슈도 올바르게 직렬화되는지"""
        issue = CompatibilityIssue(
            issue_type=IssueType.INVALID_DATE,
            severity="error",
            location="db.orders.created_at",
            description="invalid date value",
            suggestion="fix it",
            table_name="orders",
            column_name="created_at",
        )
        report = make_report(remaining_issues=[issue])
        output = tmp_path / "report.json"
        MigrationReportRenderer().export_report_json(report, str(output))

        serialized = json.loads(output.read_text(encoding="utf-8"))["remaining_issues"][0]
        assert serialized["type"] == IssueType.INVALID_DATE.value
        assert serialized["description"] == "invalid date value"
        assert serialized["table_name"] == "orders"
        assert serialized["column_name"] == "created_at"

    def test_empty_issues(self, tmp_path):
        report = make_report()
        output = tmp_path / "report.json"
        MigrationReportRenderer().export_report_json(report, str(output))
        data = json.loads(output.read_text(encoding="utf-8"))
        assert data["fixed_issues"] == []
        assert data["remaining_issues"] == []
        assert data["new_issues"] == []


# ============================================================
# export_report_html
# ============================================================
class TestExportReportHtml:
    def test_valid_html_structure(self, tmp_path):
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(make_report(), str(output))
        content = output.read_text(encoding="utf-8")
        assert "<!DOCTYPE html>" in content
        assert "</html>" in content

    def test_returns_output_path(self, tmp_path):
        output = tmp_path / "report.html"
        result = MigrationReportRenderer().export_report_html(make_report(), str(output))
        assert result == str(output)

    def test_escapes_malicious_schema(self, tmp_path):
        report = make_report(schema="<script>alert(1)</script>")
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(report, str(output))
        content = output.read_text(encoding="utf-8")
        assert "<script>alert(1)</script>" not in content
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in content

    def test_escapes_malicious_issue_fields(self, tmp_path):
        report = make_report(remaining_issues=[{
            "issue_type": "charset_issue",
            "severity": "warning",
            "location": "<img src=x onerror=alert(1)>",
            "message": "<b>bold</b>",
            "suggestion": "'; DROP TABLE users; --",
        }])
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(report, str(output))
        content = output.read_text(encoding="utf-8")
        assert "<img src=x onerror=alert(1)>" not in content
        assert "&lt;img src=x onerror=alert(1)&gt;" in content
        assert "<b>bold</b>" not in content
        assert "&lt;b&gt;bold&lt;/b&gt;" in content

    def test_escapes_malicious_execution_log(self, tmp_path):
        report = make_report(execution_log=["<img onerror=alert(1) src=x>"])
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(report, str(output))
        content = output.read_text(encoding="utf-8")
        assert "<img onerror=alert(1) src=x>" not in content
        assert "&lt;img onerror=alert(1) src=x&gt;" in content

    def test_no_issues_renders_placeholder(self, tmp_path):
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(make_report(), str(output))
        content = output.read_text(encoding="utf-8")
        assert content.count("<p>없음</p>") == 3  # fixed/remaining/new 모두 비어있음

    def test_dict_and_object_issue_both_render(self, tmp_path):
        dict_issue = {
            "issue_type": "charset_issue", "severity": "warning",
            "location": "db.t1", "message": "dict issue text", "suggestion": "fix",
        }
        obj_issue = CompatibilityIssue(
            issue_type=IssueType.INVALID_DATE,
            severity="error",
            location="db.t2",
            description="object issue text",
            suggestion="fix2",
        )
        report = make_report(remaining_issues=[dict_issue, obj_issue])
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(report, str(output))
        content = output.read_text(encoding="utf-8")
        assert "dict issue text" in content
        assert "object issue text" in content
        assert "db.t1" in content
        assert "db.t2" in content

    def test_execution_log_empty_shows_placeholder(self, tmp_path):
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(make_report(execution_log=[]), str(output))
        content = output.read_text(encoding="utf-8")
        assert "로그 없음" in content

    def test_summary_counts_reflected(self, tmp_path):
        report = make_report(
            fixed_issues=[{"location": "a"}, {"location": "b"}],
            remaining_issues=[{"location": "c"}],
            new_issues=[],
        )
        output = tmp_path / "report.html"
        MigrationReportRenderer().export_report_html(report, str(output))
        content = output.read_text(encoding="utf-8")
        assert "해결된 이슈 (2개)" in content
        assert "남은 이슈 (1개)" in content
        assert "새로 발견된 이슈 (0개)" in content
