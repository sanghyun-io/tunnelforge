"""
GitHubIssueReporter 테스트

오류 요약, 핑거프린트, 유사 이슈 검색, 이슈 생성/코멘트 로직을 검증합니다.
"""
import pytest
from unittest.mock import patch, MagicMock

from src.core.github_issue_reporter import GitHubIssueReporter, get_reporter_from_config


# ============================================================
# Fixtures
# ============================================================

@pytest.fixture
def reporter():
    """기본 GitHubIssueReporter (PAT 모드)"""
    return GitHubIssueReporter(token="fake-token", repo="owner/repo")


@pytest.fixture
def reporter_with_app():
    """GitHub App 모드 GitHubIssueReporter"""
    mock_app = MagicMock()
    mock_app.repo = "owner/repo"
    mock_app.get_headers.return_value = {
        "Authorization": "token app-token",
        "Accept": "application/vnd.github.v3+json",
        "User-Agent": "TunnelForge"
    }
    return GitHubIssueReporter.from_github_app(mock_app)


# ============================================================
# check_available
# ============================================================

class TestCheckAvailable:

    def test_available(self):
        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True):
            ok, msg = GitHubIssueReporter.check_available()
        assert ok is True

    def test_unavailable(self):
        with patch('src.core.github_issue_reporter.HAS_REQUESTS', False):
            ok, msg = GitHubIssueReporter.check_available()
        assert ok is False
        assert 'requests' in msg


# ============================================================
# 민감 정보 제거 (_sanitize_error_message)
# ============================================================

class TestSanitizeErrorMessage:

    def test_ip_address_masked(self, reporter):
        msg = "Connection failed to 192.168.1.100:3306"
        result = reporter._sanitize_error_message(msg)
        assert '192.168.1.100' not in result
        assert '[IP_HIDDEN]' in result

    def test_password_masked(self, reporter):
        msg = "Access denied for password=mysecret123"
        result = reporter._sanitize_error_message(msg)
        assert 'mysecret123' not in result
        assert '[HIDDEN]' in result

    def test_password_case_insensitive(self, reporter):
        msg = "PASSWORD: hunter2"
        result = reporter._sanitize_error_message(msg)
        assert 'hunter2' not in result

    def test_windows_user_path_masked(self, reporter):
        msg = r"Error in C:\Users\JohnDoe\Documents\file.txt"
        result = reporter._sanitize_error_message(msg)
        assert 'JohnDoe' not in result
        assert '[USER]' in result

    def test_unix_user_path_masked(self, reporter):
        msg = "Error in /home/johndoe/project/file.py"
        result = reporter._sanitize_error_message(msg)
        assert 'johndoe' not in result
        assert '[USER]' in result

    def test_mac_user_path_masked(self, reporter):
        msg = "Error in /Users/johndoe/project/file.py"
        result = reporter._sanitize_error_message(msg)
        assert 'johndoe' not in result

    def test_clean_message_unchanged(self, reporter):
        msg = "Table 'users' doesn't exist"
        result = reporter._sanitize_error_message(msg)
        assert result == msg


# ============================================================
# 핵심 오류 추출 (_extract_core_error)
# ============================================================

class TestExtractCoreError:

    def test_mysql_error_code(self, reporter):
        msg = "ERROR 1045 (28000): Access denied for user"
        result = reporter._extract_core_error(msg)
        assert 'ERROR 1045' in result

    def test_mysqlsh_error(self, reporter):
        msg = "Error: Shell.connect: Cannot open database"
        result = reporter._extract_core_error(msg)
        assert 'Shell.connect' in result

    def test_duplicate_entry(self, reporter):
        msg = "Duplicate entry '42' for key 'PRIMARY'"
        result = reporter._extract_core_error(msg)
        assert 'Duplicate entry' in result
        assert 'PRIMARY' in result

    def test_foreign_key_error(self, reporter):
        msg = "Cannot add or update a child row: a foreign key constraint fails"
        result = reporter._extract_core_error(msg)
        assert 'Cannot add or update' in result

    def test_deadlock(self, reporter):
        msg = "Lock wait timeout exceeded; try restarting transaction (deadlock)"
        result = reporter._extract_core_error(msg)
        assert 'Deadlock' in result

    def test_timeout(self, reporter):
        msg = "Operation timeout after 30 seconds"
        result = reporter._extract_core_error(msg)
        assert 'timeout' in result.lower()

    def test_korean_timeout(self, reporter):
        msg = "작업 시간 초과"
        result = reporter._extract_core_error(msg)
        assert result == "Operation timeout"

    def test_long_message_truncated(self, reporter):
        msg = "x" * 200
        result = reporter._extract_core_error(msg)
        assert len(result) <= 100

    def test_multiline_takes_first_line(self, reporter):
        msg = "First line error\nSecond line detail\nThird line"
        result = reporter._extract_core_error(msg)
        assert result == "First line error"


# ============================================================
# 핑거프린트 생성 (_generate_fingerprint)
# ============================================================

class TestGenerateFingerprint:

    def test_same_error_same_fingerprint(self, reporter):
        fp1 = reporter._generate_fingerprint("export", "Access denied")
        fp2 = reporter._generate_fingerprint("export", "Access denied")
        assert fp1 == fp2

    def test_different_type_different_fingerprint(self, reporter):
        fp1 = reporter._generate_fingerprint("export", "Access denied")
        fp2 = reporter._generate_fingerprint("import", "Access denied")
        assert fp1 != fp2

    def test_numbers_ignored_in_fingerprint(self, reporter):
        fp1 = reporter._generate_fingerprint("export", "Error on table 42")
        fp2 = reporter._generate_fingerprint("export", "Error on table 99")
        assert fp1 == fp2

    def test_fingerprint_length(self, reporter):
        fp = reporter._generate_fingerprint("export", "some error")
        assert len(fp) == 16


# ============================================================
# 오류 요약 (summarize_error)
# ============================================================

class TestSummarizeError:

    def test_export_title_format(self, reporter):
        summary = reporter.summarize_error("export", "ERROR 1045: Access denied")
        assert summary['title'].startswith('[Export Error]')

    def test_import_title_format(self, reporter):
        summary = reporter.summarize_error("import", "ERROR 1045: Access denied")
        assert summary['title'].startswith('[Import Error]')

    def test_labels_include_error_type(self, reporter):
        summary = reporter.summarize_error("export", "some error")
        assert "bug" in summary['labels']
        assert "export-error" in summary['labels']
        assert "auto-reported" in summary['labels']

    def test_body_contains_core_error(self, reporter):
        summary = reporter.summarize_error("export", "ERROR 1045: Access denied for user")
        assert 'ERROR 1045' in summary['body']

    def test_context_included_in_body(self, reporter):
        context = {'schema': 'mydb', 'tables': ['users', 'orders'], 'mode': 'full'}
        summary = reporter.summarize_error("export", "some error", context)
        assert 'mydb' in summary['body']
        assert 'users' in summary['body']

    def test_many_tables_truncated(self, reporter):
        tables = [f'table_{i}' for i in range(10)]
        context = {'tables': tables}
        summary = reporter.summarize_error("export", "error", context)
        assert '외' in summary['body']

    def test_fingerprint_present(self, reporter):
        summary = reporter.summarize_error("export", "some error")
        assert 'fingerprint' in summary
        assert len(summary['fingerprint']) == 16

    def test_sensitive_info_removed_from_body(self, reporter):
        summary = reporter.summarize_error(
            "export",
            "Failed connecting to 10.0.0.1 with password=secret123"
        )
        assert '10.0.0.1' not in summary['body']
        assert 'secret123' not in summary['body']

    def test_title_max_length(self, reporter):
        long_error = "x" * 200
        summary = reporter.summarize_error("export", long_error)
        # [Export Error] prefix + 80 chars max
        assert len(summary['title']) <= len('[Export Error] ') + 100


# ============================================================
# 유사 이슈 검색 (find_similar_issue)
# ============================================================

class TestFindSimilarIssue:

    def _make_summary(self, title="[Export Error] Test", fingerprint="abc123def456", core_error="Test error"):
        return {
            'title': title,
            'body': 'test body',
            'fingerprint': fingerprint,
            'core_error': core_error,
            'labels': ['bug']
        }

    def test_finds_by_fingerprint(self, reporter):
        summary = self._make_summary(fingerprint="fp1234567890ab")
        issues = [
            {'title': 'Other issue', 'body': 'contains fingerprint:fp1234567890ab in body', 'number': 10}
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = issues
        mock_response.status_code = 200

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            result = reporter.find_similar_issue(summary)

        assert result is not None
        assert result['number'] == 10

    def test_finds_by_title_similarity(self, reporter):
        summary = self._make_summary(title="[Export Error] Access denied for user")
        issues = [
            {'title': '[Export Error] Access denied for user root', 'body': 'no fingerprint', 'number': 20}
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = issues

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            result = reporter.find_similar_issue(summary)

        assert result is not None
        assert result['number'] == 20

    def test_finds_by_core_error_similarity(self, reporter):
        summary = self._make_summary(
            title="[Export Error] Completely different title",
            core_error="ERROR 1045: Access denied",
            fingerprint=""
        )
        issues = [
            {
                'title': 'Unrelated title',
                'body': '**핵심 오류**: `ERROR 1045: Access denied for user`',
                'number': 30
            }
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = issues

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            result = reporter.find_similar_issue(summary)

        assert result is not None
        assert result['number'] == 30

    def test_returns_none_when_no_similar(self, reporter):
        summary = self._make_summary(
            title="[Export Error] Unique error",
            fingerprint="unique_fp_12345",
            core_error="Unique error message"
        )
        issues = [
            {'title': 'Totally different issue', 'body': 'no matching content', 'number': 99}
        ]

        mock_response = MagicMock()
        mock_response.json.return_value = issues

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            result = reporter.find_similar_issue(summary)

        assert result is None

    def test_returns_none_when_no_issues(self, reporter):
        summary = self._make_summary()

        mock_response = MagicMock()
        mock_response.json.return_value = []

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            result = reporter.find_similar_issue(summary)

        assert result is None

    def test_returns_none_on_api_error(self, reporter):
        import requests as real_requests
        summary = self._make_summary()

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.RequestException = real_requests.RequestException
            mock_requests.get.side_effect = real_requests.RequestException("timeout")

            result = reporter.find_similar_issue(summary)

        assert result is None

    def test_returns_none_without_requests(self, reporter):
        summary = self._make_summary()

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', False):
            result = reporter.find_similar_issue(summary)

        assert result is None


# ============================================================
# 이슈 생성 (create_issue)
# ============================================================

class TestCreateIssue:

    def _make_summary(self):
        return {
            'title': '[Export Error] Test error',
            'body': 'Error body content',
            'labels': ['bug', 'export-error', 'auto-reported'],
            'fingerprint': 'abc123'
        }

    def test_creates_issue_successfully(self, reporter):
        summary = self._make_summary()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            'number': 42,
            'html_url': 'https://github.com/owner/repo/issues/42'
        }

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.post.return_value = mock_response

            ok, msg, num = reporter.create_issue(summary)

        assert ok is True
        assert num == 42
        assert '#42' in msg

    def test_fingerprint_appended_to_body(self, reporter):
        summary = self._make_summary()
        mock_response = MagicMock()
        mock_response.json.return_value = {'number': 1, 'html_url': ''}

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.post.return_value = mock_response

            reporter.create_issue(summary)

        # 전송된 데이터 확인
        call_kwargs = mock_requests.post.call_args
        sent_body = call_kwargs[1]['json']['body'] if 'json' in call_kwargs[1] else call_kwargs[0][1]['body']
        assert 'fingerprint:abc123' in sent_body

    def test_labels_sent(self, reporter):
        summary = self._make_summary()
        mock_response = MagicMock()
        mock_response.json.return_value = {'number': 1, 'html_url': ''}

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.post.return_value = mock_response

            reporter.create_issue(summary)

        call_kwargs = mock_requests.post.call_args
        sent_data = call_kwargs[1]['json'] if 'json' in call_kwargs[1] else {}
        assert sent_data['labels'] == ['bug', 'export-error', 'auto-reported']

    def test_failure_on_api_error(self, reporter):
        import requests as real_requests
        summary = self._make_summary()

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.json.return_value = {'message': 'Forbidden'}

        exc = real_requests.HTTPError(response=mock_response)

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.RequestException = real_requests.RequestException
            mock_requests.post.side_effect = exc

            ok, msg, num = reporter.create_issue(summary)

        assert ok is False
        assert num is None

    def test_failure_without_requests(self, reporter):
        summary = self._make_summary()

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', False):
            ok, msg, num = reporter.create_issue(summary)

        assert ok is False
        assert num is None


# ============================================================
# 코멘트 추가 (add_comment)
# ============================================================

class TestAddComment:

    def _make_summary(self):
        return {
            'title': '[Export Error] Test',
            'body': '## 상세 오류 메시지\n```\nError detail here\n```\nmore content',
            'core_error': 'Error detail here',
            'fingerprint': 'fp123'
        }

    def test_adds_comment_successfully(self, reporter):
        summary = self._make_summary()
        mock_response = MagicMock()
        mock_response.status_code = 201

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.post.return_value = mock_response

            ok, msg = reporter.add_comment(42, summary)

        assert ok is True
        assert '#42' in msg

    def test_comment_url_correct(self, reporter):
        summary = self._make_summary()
        mock_response = MagicMock()

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.post.return_value = mock_response

            reporter.add_comment(42, summary)

        call_args = mock_requests.post.call_args
        url = call_args[0][0] if call_args[0] else call_args[1].get('url', '')
        assert '/issues/42/comments' in url

    def test_failure_on_api_error(self, reporter):
        import requests as real_requests
        summary = self._make_summary()

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True), \
             patch('src.core.github_issue_reporter.requests') as mock_requests:
            mock_requests.RequestException = real_requests.RequestException
            mock_requests.post.side_effect = real_requests.RequestException("timeout")

            ok, msg = reporter.add_comment(42, summary)

        assert ok is False
        assert '실패' in msg

    def test_failure_without_requests(self, reporter):
        summary = self._make_summary()

        with patch('src.core.github_issue_reporter.HAS_REQUESTS', False):
            ok, msg = reporter.add_comment(42, summary)

        assert ok is False


# ============================================================
# 메인 진입점 (report_error)
# ============================================================

class TestReportError:

    def test_creates_new_issue_when_no_similar(self, reporter):
        with patch.object(reporter, 'find_similar_issue', return_value=None), \
             patch.object(reporter, 'create_issue', return_value=(True, "이슈 #1 생성됨", 1)):

            ok, msg = reporter.report_error("export", "ERROR 1045: Access denied")

        assert ok is True
        assert '#1' in msg

    def test_adds_comment_when_similar_found(self, reporter):
        similar = {'number': 10, 'title': 'Similar issue'}

        with patch.object(reporter, 'find_similar_issue', return_value=similar), \
             patch.object(reporter, 'add_comment', return_value=(True, "코멘트 추가됨")):

            ok, msg = reporter.report_error("export", "ERROR 1045: Access denied")

        assert ok is True
        assert '#10' in msg

    def test_returns_false_when_comment_fails(self, reporter):
        similar = {'number': 10}

        with patch.object(reporter, 'find_similar_issue', return_value=similar), \
             patch.object(reporter, 'add_comment', return_value=(False, "API error")):

            ok, msg = reporter.report_error("export", "some error")

        assert ok is False

    def test_returns_false_without_requests(self, reporter):
        with patch('src.core.github_issue_reporter.HAS_REQUESTS', False):
            ok, msg = reporter.report_error("export", "some error")

        assert ok is False

    def test_returns_false_when_no_repo_no_app(self):
        r = GitHubIssueReporter(token="tok", repo="")
        with patch('src.core.github_issue_reporter.HAS_REQUESTS', True):
            ok, msg = r.report_error("export", "error")
        assert ok is False

    def test_handles_unexpected_exception(self, reporter):
        with patch.object(reporter, 'summarize_error', side_effect=RuntimeError("unexpected")):
            ok, msg = reporter.report_error("export", "error")

        assert ok is False
        assert '실패' in msg


# ============================================================
# from_github_app
# ============================================================

class TestFromGitHubApp:

    def test_creates_instance_from_app(self):
        mock_app = MagicMock()
        mock_app.repo = "test/repo"
        mock_app.get_headers.return_value = {"Authorization": "token app-tok"}

        reporter = GitHubIssueReporter.from_github_app(mock_app)

        assert reporter.repo == "test/repo"
        assert reporter._github_app is mock_app
        assert reporter._headers == {"Authorization": "token app-tok"}

    def test_refresh_headers_calls_app(self, reporter_with_app):
        reporter_with_app._github_app.get_headers.return_value = {"Authorization": "token new-tok"}

        reporter_with_app._refresh_headers_if_needed()

        assert reporter_with_app._headers == {"Authorization": "token new-tok"}

    def test_refresh_headers_noop_without_app(self, reporter):
        original_headers = reporter._headers.copy()
        reporter._refresh_headers_if_needed()
        assert reporter._headers == original_headers


# ============================================================
# get_reporter_from_config
# ============================================================

class TestGetReporterFromConfig:

    def test_returns_none_when_auto_report_disabled(self):
        config = MagicMock()
        config.get_app_setting.return_value = False

        result = get_reporter_from_config(config)
        assert result is None

    def test_returns_none_when_github_app_unavailable(self):
        config = MagicMock()
        config.get_app_setting.return_value = True

        mock_app = MagicMock()
        mock_app.check_available.return_value = (False, "missing lib")

        with patch('src.core.github_issue_reporter.get_reporter_from_config') as mock_func:
            # 직접 호출
            pass

        # 실제 함수 테스트: github_app_auth import 실패 시
        with patch.dict('sys.modules', {'src.core.github_app_auth': None}):
            result = get_reporter_from_config(config)

        assert result is None

    def test_returns_reporter_when_app_available(self):
        config = MagicMock()
        config.get_app_setting.return_value = True

        mock_app = MagicMock()
        mock_app.repo = "test/repo"
        mock_app.check_available.return_value = (True, "ok")
        mock_app.get_headers.return_value = {"Authorization": "token tok"}

        mock_module = MagicMock()
        mock_module.get_github_app_auth.return_value = mock_app

        with patch.dict('sys.modules', {'src.core.github_app_auth': mock_module}):
            result = get_reporter_from_config(config)

        assert result is not None
        assert result.repo == "test/repo"
