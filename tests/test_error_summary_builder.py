"""
ErrorSummaryBuilder 단위 테스트

GitHubIssueReporter에서 분리된 순수 오류 요약 로직(민감정보 제거, 핵심 오류
추출, 핑거프린트 생성, 이슈 본문 생성)을 네트워크 의존 없이 검증합니다.
"""
import pytest

from src.core.error_summary_builder import ErrorSummaryBuilder


@pytest.fixture
def builder():
    return ErrorSummaryBuilder()


class TestSanitizeErrorMessage:

    def test_ip_address_masked(self, builder):
        msg = "Connection failed to 192.168.1.100:3306"
        result = builder.sanitize_error_message(msg)
        assert '192.168.1.100' not in result
        assert '[IP_HIDDEN]' in result

    def test_password_masked(self, builder):
        msg = "Access denied for password=mysecret123"
        result = builder.sanitize_error_message(msg)
        assert 'mysecret123' not in result
        assert '[HIDDEN]' in result

    def test_windows_user_path_masked(self, builder):
        msg = r"Error in C:\Users\JohnDoe\Documents\file.txt"
        result = builder.sanitize_error_message(msg)
        assert 'JohnDoe' not in result
        assert '[USER]' in result

    def test_clean_message_unchanged(self, builder):
        msg = "Table 'users' doesn't exist"
        assert builder.sanitize_error_message(msg) == msg


class TestExtractCoreError:

    def test_mysql_error_code(self, builder):
        msg = "ERROR 1045 (28000): Access denied for user"
        assert 'ERROR 1045' in builder.extract_core_error(msg)

    def test_duplicate_entry(self, builder):
        msg = "Duplicate entry '42' for key 'PRIMARY'"
        result = builder.extract_core_error(msg)
        assert 'Duplicate entry' in result
        assert 'PRIMARY' in result

    def test_deadlock(self, builder):
        msg = "Lock wait timeout exceeded; try restarting transaction (deadlock)"
        assert builder.extract_core_error(msg) == "Deadlock detected during operation"

    def test_korean_timeout(self, builder):
        assert builder.extract_core_error("작업 시간 초과") == "Operation timeout"

    def test_long_message_truncated_to_core_error_max_len(self, builder):
        msg = "x" * 200
        result = builder.extract_core_error(msg)
        assert len(result) == builder.CORE_ERROR_MAX_LEN


class TestGenerateFingerprint:

    def test_same_error_same_fingerprint(self, builder):
        fp1 = builder.generate_fingerprint("export", "Access denied")
        fp2 = builder.generate_fingerprint("export", "Access denied")
        assert fp1 == fp2

    def test_different_type_different_fingerprint(self, builder):
        fp1 = builder.generate_fingerprint("export", "Access denied")
        fp2 = builder.generate_fingerprint("import", "Access denied")
        assert fp1 != fp2

    def test_fingerprint_length(self, builder):
        assert len(builder.generate_fingerprint("export", "some error")) == 16


class TestGenerateIssueBody:

    def test_contains_core_error(self, builder):
        body = builder.generate_issue_body("Export", "ERROR 1045", "ERROR 1045: full", {})
        assert 'ERROR 1045' in body

    def test_context_included(self, builder):
        context = {'schema': 'mydb', 'tables': ['users', 'orders'], 'mode': 'full'}
        body = builder.generate_issue_body("Export", "err", "full message", context)
        assert 'mydb' in body
        assert 'users' in body

    def test_many_tables_truncated(self, builder):
        tables = [f'table_{i}' for i in range(10)]
        body = builder.generate_issue_body("Export", "err", "msg", {'tables': tables})
        assert '외' in body

    def test_body_preview_truncated_to_body_preview_len(self, builder):
        long_message = "x" * (builder.BODY_PREVIEW_LEN + 500)
        body = builder.generate_issue_body("Export", "err", long_message, {})
        assert long_message not in body


class TestSummarizeError:

    def test_title_format(self, builder):
        summary = builder.summarize_error("export", "ERROR 1045: Access denied")
        assert summary['title'].startswith('[Export Error]')

    def test_labels_include_error_type(self, builder):
        summary = builder.summarize_error("export", "some error")
        assert "bug" in summary['labels']
        assert "export-error" in summary['labels']
        assert "auto-reported" in summary['labels']

    def test_full_message_present_and_sanitized(self, builder):
        summary = builder.summarize_error(
            "export", "Failed connecting to 10.0.0.1 with password=secret123"
        )
        assert 'full_message' in summary
        assert '10.0.0.1' not in summary['full_message']
        assert 'secret123' not in summary['full_message']

    def test_fingerprint_present(self, builder):
        summary = builder.summarize_error("export", "some error")
        assert len(summary['fingerprint']) == 16

    def test_sensitive_info_removed_from_body(self, builder):
        summary = builder.summarize_error(
            "export", "Failed connecting to 10.0.0.1 with password=secret123"
        )
        assert '10.0.0.1' not in summary['body']
        assert 'secret123' not in summary['body']
