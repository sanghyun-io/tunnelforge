"""
GitHub 이슈 자동 발행 모듈

Export/Import 오류 발생 시:
1. 오류 요약
2. 열려있는 이슈 중 유사한 내용 검색
3. 없으면 이슈 생성, 있으면 코멘트 추가
"""

import re
import hashlib
from datetime import datetime
from typing import Optional, Tuple, Dict
from difflib import SequenceMatcher

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
        오류를 요약하여 이슈 제목과 본문 생성

        Args:
            error_type: 오류 유형 ("export" 또는 "import")
            error_message: 오류 메시지
            context: 추가 컨텍스트 (schema, tables, timestamp 등)

        Returns:
            Dict with 'title', 'body', 'labels', 'fingerprint'
        """
        context = context or {}

        # 오류 메시지 정리 (민감 정보 제거)
        sanitized_message = self._sanitize_error_message(error_message)

        # 핵심 오류 추출
        core_error = self._extract_core_error(sanitized_message)

        # 이슈 제목 생성
        operation = "Export" if error_type == "export" else "Import"
        title = f"[{operation} Error] {core_error[:80]}"

        # 이슈 본문 생성
        body = self._generate_issue_body(
            operation=operation,
            core_error=core_error,
            full_message=sanitized_message,
            context=context
        )

        # 라벨 설정
        labels = ["bug", f"{error_type}-error", "auto-reported"]

        # 중복 검사용 핑거프린트 (핵심 오류 기반)
        fingerprint = self._generate_fingerprint(error_type, core_error)

        return {
            "title": title,
            "body": body,
            "labels": labels,
            "fingerprint": fingerprint,
            "core_error": core_error
        }

    def _sanitize_error_message(self, message: str) -> str:
        """민감 정보 제거 (IP, 비밀번호, 경로 등)"""
        sanitized = message

        # IP 주소 마스킹
        sanitized = re.sub(
            r'\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b',
            '[IP_HIDDEN]',
            sanitized
        )

        # 비밀번호 패턴 마스킹
        sanitized = re.sub(
            r'(password[=:]\s*)[^\s]+',
            r'\1[HIDDEN]',
            sanitized,
            flags=re.IGNORECASE
        )

        # 파일 경로 마스킹 (사용자명 부분)
        sanitized = re.sub(
            r'(C:\\Users\\)[^\\]+',
            r'\1[USER]',
            sanitized
        )
        sanitized = re.sub(
            r'(/home/|/Users/)[^/]+',
            r'\1[USER]',
            sanitized
        )

        return sanitized

    def _extract_core_error(self, message: str) -> str:
        """오류 메시지에서 핵심 오류 추출"""
        # MySQL 오류 코드 패턴
        mysql_error = re.search(r'(ERROR \d+.*?)(?:\n|$)', message)
        if mysql_error:
            return mysql_error.group(1).strip()

        # mysqlsh 오류 패턴
        mysqlsh_error = re.search(r'(?:Error|ERROR):\s*(.+?)(?:\n|$)', message)
        if mysqlsh_error:
            return mysqlsh_error.group(1).strip()

        # Duplicate entry 등 특정 패턴
        dup_error = re.search(r"(Duplicate entry .+ for key .+)", message)
        if dup_error:
            return dup_error.group(1).strip()

        # Foreign key 오류
        fk_error = re.search(r"(Cannot add or update a child row.*)", message)
        if fk_error:
            return fk_error.group(1)[:100].strip()

        # Deadlock
        if 'deadlock' in message.lower():
            return "Deadlock detected during operation"

        # Timeout
        if 'timeout' in message.lower() or '시간 초과' in message:
            return "Operation timeout"

        # 기본: 첫 줄 또는 100자
        first_line = message.split('\n')[0].strip()
        return first_line[:100] if len(first_line) > 100 else first_line

    def _generate_issue_body(self, operation: str, core_error: str,
                             full_message: str, context: Dict) -> str:
        """이슈 본문 생성"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        body = f"""## 오류 요약
**작업 유형**: {operation}
**핵심 오류**: `{core_error}`
**발생 시간**: {timestamp}

## 상세 오류 메시지
```
{full_message[:2000]}
```

"""
        # 컨텍스트 정보 추가
        if context:
            body += "## 컨텍스트 정보\n"
            if context.get('schema'):
                body += f"- **스키마**: `{context['schema']}`\n"
            if context.get('tables'):
                tables = context['tables']
                if len(tables) <= 5:
                    body += f"- **테이블**: `{', '.join(tables)}`\n"
                else:
                    body += f"- **테이블**: `{', '.join(tables[:5])}` 외 {len(tables)-5}개\n"
            if context.get('mode'):
                body += f"- **모드**: `{context['mode']}`\n"
            if context.get('failed_tables'):
                failed = context['failed_tables']
                body += f"- **실패한 테이블**: `{', '.join(failed[:10])}`\n"
            body += "\n"

        body += """---
> 이 이슈는 TunnelForge에서 자동으로 생성되었습니다.
"""
        return body

    def _generate_fingerprint(self, error_type: str, core_error: str) -> str:
        """중복 검사용 핑거프린트 생성"""
        # 숫자와 특수 문자를 제거하여 일반화
        normalized = re.sub(r'[\d\'"`]', '', core_error.lower())
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        # 해시 생성
        hash_input = f"{error_type}:{normalized}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:16]

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
            error_msg = str(e)
            if hasattr(e, 'response') and e.response is not None:
                try:
                    error_detail = e.response.json().get('message', '')
                    error_msg = f"{error_msg}: {error_detail}"
                except:
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
            comment_body = f"""## 동일 오류 재발생
**발생 시간**: {timestamp}

### 오류 메시지
```
{summary.get('body', '').split('## 상세 오류 메시지')[1].split('```')[1][:1000] if '## 상세 오류 메시지' in summary.get('body', '') else summary.get('core_error', '')}
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
            return False, f"코멘트 추가 실패: {str(e)}"

    def _is_auth_error(self, exc: Exception) -> bool:
        """401/403 인증 오류인지 확인"""
        if hasattr(exc, 'response') and exc.response is not None:
            return exc.response.status_code in (401, 403)
        return False

    def report_error(self, error_type: str, error_message: str,
                     context: Optional[Dict] = None) -> Tuple[bool, str]:
        """
        오류 리포트 (메인 진입점)

        1. 오류 요약
        2. 유사 이슈 검색
        3. 없으면 생성, 있으면 코멘트 추가
        4. 401/403 시 토큰 갱신 후 1회 재시도

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
            return self._do_report(error_type, error_message, context)
        except requests.RequestException as e:
            # 401/403 인증 오류 시 토큰 갱신 후 1회 재시도
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
