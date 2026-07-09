"""오류 요약 생성 순수 로직 (네트워크 의존 없음)

GitHubIssueReporter가 사용하던 오류 메시지 정리/핵심 오류 추출/핑거프린트
생성/이슈 본문 생성 로직을 requests 등 네트워크 의존 없이 분리한 모듈.
GitHubIssueReporter는 이 클래스의 인스턴스를 보유하고 얇은 위임 메서드로
기존 private 메서드(_sanitize_error_message 등)를 유지한다.
"""
import re
import hashlib
from datetime import datetime
from typing import Dict, Optional


class ErrorSummaryBuilder:
    """오류 요약(제목/본문/라벨/핑거프린트) 생성기 — 순수 로직, 네트워크 비의존"""

    # 이슈 제목에 포함할 핵심 오류 미리보기 길이
    TITLE_PREVIEW_LEN = 80
    # _extract_core_error가 잘라내는 핵심 오류 문자열 최대 길이
    CORE_ERROR_MAX_LEN = 100
    # 이슈 본문에 포함할 전체 오류 메시지 미리보기 길이
    BODY_PREVIEW_LEN = 2000

    def sanitize_error_message(self, message: str) -> str:
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

    def extract_core_error(self, message: str) -> str:
        """오류 메시지에서 핵심 오류 추출"""
        # MySQL 오류 코드 패턴
        mysql_error = re.search(r'(ERROR \d+.*?)(?:\n|$)', message)
        if mysql_error:
            return mysql_error.group(1).strip()

        # 외부 도구 오류 패턴
        tool_error = re.search(r'(?:Error|ERROR):\s*(.+?)(?:\n|$)', message)
        if tool_error:
            return tool_error.group(1).strip()

        # Duplicate entry 등 특정 패턴
        dup_error = re.search(r"(Duplicate entry .+ for key .+)", message)
        if dup_error:
            return dup_error.group(1).strip()

        # Foreign key 오류
        fk_error = re.search(r"(Cannot add or update a child row.*)", message)
        if fk_error:
            return fk_error.group(1)[:self.CORE_ERROR_MAX_LEN].strip()

        # Deadlock
        if 'deadlock' in message.lower():
            return "Deadlock detected during operation"

        # Timeout
        if 'timeout' in message.lower() or '시간 초과' in message:
            return "Operation timeout"

        # 기본: 첫 줄 또는 100자
        first_line = message.split('\n')[0].strip()
        return first_line[:self.CORE_ERROR_MAX_LEN] if len(first_line) > self.CORE_ERROR_MAX_LEN else first_line

    def generate_fingerprint(self, error_type: str, core_error: str) -> str:
        """중복 검사용 핑거프린트 생성"""
        # 숫자와 특수 문자를 제거하여 일반화
        normalized = re.sub(r'[\d\'"`]', '', core_error.lower())
        normalized = re.sub(r'\s+', ' ', normalized).strip()

        # 해시 생성
        hash_input = f"{error_type}:{normalized}"
        return hashlib.md5(hash_input.encode()).hexdigest()[:16]

    def generate_issue_body(self, operation: str, core_error: str,
                             full_message: str, context: Dict) -> str:
        """이슈 본문 생성"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        body = f"""## 오류 요약
**작업 유형**: {operation}
**핵심 오류**: `{core_error}`
**발생 시간**: {timestamp}

## 상세 오류 메시지
```
{full_message[:self.BODY_PREVIEW_LEN]}
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

    def summarize_error(self, error_type: str, error_message: str,
                         context: Optional[Dict] = None) -> Dict:
        """
        오류를 요약하여 이슈 제목과 본문 생성

        Args:
            error_type: 오류 유형 ("export" 또는 "import")
            error_message: 오류 메시지
            context: 추가 컨텍스트 (schema, tables, timestamp 등)

        Returns:
            Dict with 'title', 'body', 'labels', 'fingerprint', 'core_error', 'full_message'
        """
        context = context or {}

        # 오류 메시지 정리 (민감 정보 제거)
        sanitized_message = self.sanitize_error_message(error_message)

        # 핵심 오류 추출
        core_error = self.extract_core_error(sanitized_message)

        # 이슈 제목 생성
        operation = "Export" if error_type == "export" else "Import"
        title = f"[{operation} Error] {core_error[:self.TITLE_PREVIEW_LEN]}"

        # 이슈 본문 생성
        body = self.generate_issue_body(
            operation=operation,
            core_error=core_error,
            full_message=sanitized_message,
            context=context
        )

        # 라벨 설정
        labels = ["bug", f"{error_type}-error", "auto-reported"]

        # 중복 검사용 핑거프린트 (핵심 오류 기반)
        fingerprint = self.generate_fingerprint(error_type, core_error)

        return {
            "title": title,
            "body": body,
            "labels": labels,
            "fingerprint": fingerprint,
            "core_error": core_error,
            "full_message": sanitized_message,
        }
