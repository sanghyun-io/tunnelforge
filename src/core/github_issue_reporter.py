"""
GitHub 이슈 자동 발행 모듈

Export/Import 오류 발생 시:
1. 오류 요약
2. 열려있는 이슈 중 유사한 내용 검색
3. 없으면 이슈 생성, 있으면 코멘트 추가
"""

import re
from datetime import datetime
from typing import Optional, Tuple, Dict
from difflib import SequenceMatcher

from src.core.error_summary_builder import ErrorSummaryBuilder

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class GitHubIssueReporter:
    """GitHub 이슈 자동 리포터"""

    GITHUB_API_BASE = "https://api.github.com"

    # 유사도 임계값 (0.0 ~ 1.0)
    SIMILARITY_THRESHOLD = 0.6

    # add_comment가 재발생 코멘트에 포함하는 오류 메시지 미리보기 길이
    COMMENT_PREVIEW_LEN = 1000

    def __init__(self, token: str, repo: str, headers: Optional[Dict] = None):
        """
        Args:
            token: GitHub Personal Access Token (또는 Installation Token)
            repo: 리포지토리 (owner/repo 형식, 예: 'sanghyun-io/tunnelforge')
            headers: 커스텀 헤더 (GitHub App 사용 시)
        """
        self.token = token
        self.repo = repo
        self._github_app = None  # GitHub App 인스턴스 (동적 토큰 갱신용)
        self._last_error_status: Optional[int] = None  # 마지막 API 실패의 HTTP status (재시도 판단용)
        self._summary = ErrorSummaryBuilder()  # 오류 요약 순수 로직 (네트워크 비의존)

        if headers:
            self._headers = headers
        else:
            self._headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
                "User-Agent": "TunnelForge"
            }

    @classmethod
    def from_github_app(cls, github_app) -> 'GitHubIssueReporter':
        """GitHub App 인증으로 인스턴스 생성"""
        headers = github_app.get_headers()
        instance = cls(
            token="",  # GitHub App은 동적 토큰 사용
            repo=github_app.repo,
            headers=headers
        )
        instance._github_app = github_app
        return instance

    def _refresh_headers_if_needed(self, force: bool = False):
        """GitHub App 사용 시 헤더 갱신"""
        if self._github_app:
            if force:
                self._github_app.get_installation_token(force_refresh=True)
            self._headers = self._github_app.get_headers()

    @staticmethod
    def check_available() -> Tuple[bool, str]:
        """requests 라이브러리 사용 가능 여부 확인"""
        if HAS_REQUESTS:
            return True, "GitHub 이슈 리포팅 사용 가능"
        return False, "requests 라이브러리가 설치되지 않았습니다. pip install requests"

    def summarize_error(self, error_type: str, error_message: str,
                        context: Optional[Dict] = None) -> Dict:
        """
        오류를 요약하여 이슈 제목과 본문 생성 (ErrorSummaryBuilder에 위임)

        Args:
            error_type: 오류 유형 ("export" 또는 "import")
            error_message: 오류 메시지
            context: 추가 컨텍스트 (schema, tables, timestamp 등)

        Returns:
            Dict with 'title', 'body', 'labels', 'fingerprint', 'core_error', 'full_message'
        """
        return self._summary.summarize_error(error_type, error_message, context)

    def _sanitize_error_message(self, message: str) -> str:
        """민감 정보 제거 (IP, 비밀번호, 경로 등) — ErrorSummaryBuilder에 위임"""
        return self._summary.sanitize_error_message(message)

    def _extract_core_error(self, message: str) -> str:
        """오류 메시지에서 핵심 오류 추출 — ErrorSummaryBuilder에 위임"""
        return self._summary.extract_core_error(message)

    def _generate_issue_body(self, operation: str, core_error: str,
                             full_message: str, context: Dict) -> str:
        """이슈 본문 생성 — ErrorSummaryBuilder에 위임"""
        return self._summary.generate_issue_body(operation, core_error, full_message, context)

    def _generate_fingerprint(self, error_type: str, core_error: str) -> str:
        """중복 검사용 핑거프린트 생성 — ErrorSummaryBuilder에 위임"""
        return self._summary.generate_fingerprint(error_type, core_error)

    def find_similar_issue(self, summary: Dict) -> Optional[Dict]:
        """
        열린 이슈 중 유사한 것 검색

        Args:
            summary: summarize_error()의 반환값

        Returns:
            유사한 이슈가 있으면 해당 이슈 정보, 없으면 None
        """
        if not HAS_REQUESTS:
            return None

        try:
            self._refresh_headers_if_needed()

            # 열린 이슈 검색 (최근 100개)
            url = f"{self.GITHUB_API_BASE}/repos/{self.repo}/issues"
            params = {
                "state": "open",
                "per_page": 100,
                "labels": "auto-reported"
            }

            response = requests.get(url, headers=self._headers, params=params, timeout=30)
            response.raise_for_status()

            issues = response.json()
            core_error = summary.get('core_error', '')
            fingerprint = summary.get('fingerprint', '')

            for issue in issues:
                # 핑거프린트 일치 확인 (본문에 저장됨)
                if fingerprint and f"fingerprint:{fingerprint}" in issue.get('body', ''):
                    return issue

                # 제목 유사도 검사
                issue_title = issue.get('title', '')
                similarity = SequenceMatcher(
                    None,
                    summary['title'].lower(),
                    issue_title.lower()
                ).ratio()

                if similarity >= self.SIMILARITY_THRESHOLD:
                    return issue

                # 핵심 오류 유사도 검사
                issue_body = issue.get('body', '')
                if core_error:
                    # 본문에서 핵심 오류 추출
                    body_error_match = re.search(r'\*\*핵심 오류\*\*:\s*`([^`]+)`', issue_body)
                    if body_error_match:
                        existing_error = body_error_match.group(1)
                        error_similarity = SequenceMatcher(
                            None,
                            core_error.lower(),
                            existing_error.lower()
                        ).ratio()
                        if error_similarity >= self.SIMILARITY_THRESHOLD:
                            return issue

            return None

        except requests.RequestException as e:
            self._last_error_status = self._extract_status_code(e)
            print(f"GitHub API 오류 (이슈 검색): {e}")
            return None

    def create_issue(self, summary: Dict) -> Tuple[bool, str, Optional[int]]:
        """
        새 이슈 생성

        Args:
            summary: summarize_error()의 반환값

        Returns:
            (성공여부, 메시지, 이슈번호)
        """
        if not HAS_REQUESTS:
            return False, "requests 라이브러리가 필요합니다", None

        try:
            self._refresh_headers_if_needed()

            url = f"{self.GITHUB_API_BASE}/repos/{self.repo}/issues"

            # 본문에 핑거프린트 추가
            body_with_fingerprint = summary['body']
            body_with_fingerprint += f"\n<!-- fingerprint:{summary['fingerprint']} -->\n"

            data = {
                "title": summary['title'],
                "body": body_with_fingerprint,
                "labels": summary['labels']
            }

            response = requests.post(
                url,
                headers=self._headers,
                json=data,
                timeout=30
            )
            response.raise_for_status()

            issue = response.json()
            issue_number = issue.get('number')
            issue_url = issue.get('html_url', '')

            return True, f"이슈 #{issue_number} 생성됨: {issue_url}", issue_number

        except requests.RequestException as e:
            self._last_error_status = self._extract_status_code(e)
            error_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_detail = e.response.json().get('message', '')
                    error_msg = f"{error_msg}: {error_detail}"
                except (ValueError, Exception):
                    pass
            return False, f"이슈 생성 실패: {error_msg}", None

    def add_comment(self, issue_number: int, summary: Dict) -> Tuple[bool, str]:
        """
        기존 이슈에 코멘트 추가

        Args:
            issue_number: 이슈 번호
            summary: summarize_error()의 반환값

        Returns:
            (성공여부, 메시지)
        """
        if not HAS_REQUESTS:
            return False, "requests 라이브러리가 필요합니다"

        try:
            self._refresh_headers_if_needed()

            url = f"{self.GITHUB_API_BASE}/repos/{self.repo}/issues/{issue_number}/comments"

            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            error_preview = (summary.get('full_message') or summary.get('core_error', ''))[:self.COMMENT_PREVIEW_LEN]
            comment_body = f"""## 동일 오류 재발생
**발생 시간**: {timestamp}

### 오류 메시지
```
{error_preview}
```

---
> 자동 생성된 코멘트입니다.
"""

            response = requests.post(
                url,
                headers=self._headers,
                json={"body": comment_body},
                timeout=30
            )
            response.raise_for_status()

            return True, f"이슈 #{issue_number}에 코멘트 추가됨"

        except requests.RequestException as e:
            self._last_error_status = self._extract_status_code(e)
            return False, f"코멘트 추가 실패: {str(e)}"

    @staticmethod
    def _extract_status_code(exc: Exception) -> Optional[int]:
        """예외에서 HTTP status code 추출 (response가 없으면 None)"""
        if hasattr(exc, 'response') and exc.response is not None:
            return getattr(exc.response, 'status_code', None)
        return None

    def _is_auth_error(self, exc: Exception) -> bool:
        """401/403 인증 오류인지 확인"""
        return self._extract_status_code(exc) in (401, 403)

    def report_error(self, error_type: str, error_message: str,
                     context: Optional[Dict] = None) -> Tuple[bool, str]:
        """
        오류 리포트 (메인 진입점)

        1. 오류 요약
        2. 유사 이슈 검색
        3. 없으면 생성, 있으면 코멘트 추가
        4. 401/403 시 토큰 갱신 후 1회 재시도

        주의: find_similar_issue/create_issue/add_comment는 모두 RequestException을
        내부에서 잡아 (False, msg) 형태의 반환값으로 바꾸므로, 여기서 RequestException을
        캐치하는 경로는 정상적으로는 도달하지 않는다. 따라서 auth 실패 감지는 각 메서드가
        기록한 self._last_error_status(반환된 status)를 검사해서 판단한다.

        Args:
            error_type: "export" 또는 "import"
            error_message: 오류 메시지
            context: 추가 컨텍스트

        Returns:
            (성공여부, 결과메시지)
        """
        if not HAS_REQUESTS:
            return False, "requests 라이브러리가 설치되지 않았습니다"

        # GitHub App 모드면 token 체크 스킵
        if not self._github_app and not self.repo:
            return False, "GitHub App이 설정되지 않았습니다"

        try:
            self._last_error_status = None
            success, msg = self._do_report(error_type, error_message, context)

            if not success and self._github_app and self._last_error_status in (401, 403):
                try:
                    self._refresh_headers_if_needed(force=True)
                    self._last_error_status = None
                    return self._do_report(error_type, error_message, context)
                except Exception as retry_e:
                    return False, f"토큰 갱신 후 재시도 실패: {str(retry_e)}"

            return success, msg
        except requests.RequestException as e:
            # 안전망: 향후 변경으로 예외가 실제로 전파되는 경우를 대비
            if self._github_app and self._is_auth_error(e):
                try:
                    self._refresh_headers_if_needed(force=True)
                    return self._do_report(error_type, error_message, context)
                except Exception as retry_e:
                    return False, f"토큰 갱신 후 재시도 실패: {str(retry_e)}"
            return False, f"오류 리포트 실패: {str(e)}"
        except Exception as e:
            return False, f"오류 리포트 실패: {str(e)}"

    def _do_report(self, error_type: str, error_message: str,
                   context: Optional[Dict] = None) -> Tuple[bool, str]:
        """실제 리포트 수행 (report_error에서 호출)"""
        # 1. 오류 요약
        summary = self.summarize_error(error_type, error_message, context)

        # 2. 유사 이슈 검색
        similar_issue = self.find_similar_issue(summary)

        if similar_issue:
            # 3a. 기존 이슈에 코멘트 추가
            issue_number = similar_issue.get('number')
            success, msg = self.add_comment(issue_number, summary)
            if success:
                return True, f"기존 이슈 #{issue_number}에 코멘트 추가됨"
            return False, msg
        else:
            # 3b. 새 이슈 생성
            success, msg, issue_number = self.create_issue(summary)
            return success, msg


def get_reporter_from_config(config_manager) -> Optional[GitHubIssueReporter]:
    """
    GitHubIssueReporter 인스턴스 생성 (GitHub App 사용)
    """
    auto_report = config_manager.get_app_setting('github_auto_report', False)
    if not auto_report:
        return None

    # GitHub App 사용
    try:
        from src.core.github_app_auth import get_github_app_auth
        github_app = get_github_app_auth()
        if github_app:
            available, _ = github_app.check_available()
            if available:
                return GitHubIssueReporter.from_github_app(github_app)
    except ImportError:
        pass

    return None


def is_github_app_available() -> bool:
    """GitHub App 설정 여부 확인"""
    try:
        from src.core.github_app_auth import is_github_app_configured
        return is_github_app_configured()
    except ImportError:
        return False
