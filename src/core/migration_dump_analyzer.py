"""
덤프 파일(SQL/TSV) 분석기 - MySQL 8.0 -> 8.4 호환성 검사
"""
import re
from typing import List, Callable, Optional, Tuple
from dataclasses import dataclass, field
from pathlib import Path

from src.core.migration_constants import (
    ALL_RESERVED_KEYWORDS,
    IssueType,
    CompatibilityIssue,
    INVALID_DATE_PATTERN,
    INVALID_DATETIME_PATTERN,
    ZEROFILL_PATTERN,
    FLOAT_PRECISION_PATTERN,
    FK_NAME_LENGTH_PATTERN,
    AUTH_PLUGIN_PATTERN,
    FTS_TABLE_PREFIX_PATTERN,
    SUPER_PRIVILEGE_PATTERN,
    SYS_VAR_USAGE_PATTERN,
)


@dataclass
class DumpAnalysisResult:
    """덤프 파일 분석 결과"""
    dump_path: str
    analyzed_at: str
    total_sql_files: int
    total_tsv_files: int
    compatibility_issues: List[CompatibilityIssue] = field(default_factory=list)


class DumpFileAnalyzer:
    """
    dump 파일 분석기

    덤프 폴더의 SQL/TSV 파일을 분석하여 MySQL 8.4 호환성 이슈를 탐지합니다.
    """

    def __init__(self):
        self._progress_callback: Optional[Callable[[str], None]] = None
        self._issue_callback: Optional[Callable[[CompatibilityIssue], None]] = None

    def set_progress_callback(self, callback: Callable[[str], None]):
        """진행 상황 콜백 설정"""
        self._progress_callback = callback

    def set_issue_callback(self, callback: Callable[[CompatibilityIssue], None]):
        """이슈 발견 시 콜백 설정"""
        self._issue_callback = callback

    def _log(self, message: str):
        """진행 상황 로깅"""
        if self._progress_callback:
            self._progress_callback(message)

    def _report_issue(self, issue: CompatibilityIssue):
        """이슈 발견 시 콜백 호출"""
        if self._issue_callback:
            self._issue_callback(issue)

    def analyze_dump_folder(self, dump_path: str) -> DumpAnalysisResult:
        """
        덤프 폴더 전체 분석

        Args:
            dump_path: dump 폴더 경로

        Returns:
            DumpAnalysisResult
        """
        from datetime import datetime

        path = Path(dump_path)
        if not path.exists():
            raise FileNotFoundError(f"덤프 폴더를 찾을 수 없습니다: {dump_path}")

        self._log(f"🔍 덤프 폴더 분석 시작: {dump_path}")

        issues: List[CompatibilityIssue] = []

        # SQL 파일 목록
        sql_files = list(path.glob("*.sql"))
        tsv_files = list(path.glob("*.tsv")) + list(path.glob("*.tsv.zst"))

        self._log(f"  SQL 파일: {len(sql_files)}개, 데이터 파일: {len(tsv_files)}개")

        # SQL 파일 분석
        for i, sql_file in enumerate(sql_files, 1):
            self._log(f"  [{i}/{len(sql_files)}] {sql_file.name} 분석 중...")
            file_issues = self._analyze_sql_file(sql_file)
            issues.extend(file_issues)

            # 실시간 이슈 콜백
            for issue in file_issues:
                self._report_issue(issue)

        # TSV 데이터 파일 분석 (0000-00-00 날짜 등)
        # 압축되지 않은 TSV 파일만 분석 (압축 파일은 너무 느림)
        uncompressed_tsv = [f for f in tsv_files if not str(f).endswith('.zst')]
        if uncompressed_tsv:
            for i, tsv_file in enumerate(uncompressed_tsv, 1):
                self._log(f"  [{i}/{len(uncompressed_tsv)}] {tsv_file.name} 분석 중...")
                file_issues = self._analyze_tsv_file(tsv_file)
                issues.extend(file_issues)

                for issue in file_issues:
                    self._report_issue(issue)

        # 결과 생성
        result = DumpAnalysisResult(
            dump_path=str(dump_path),
            analyzed_at=datetime.now().isoformat(),
            total_sql_files=len(sql_files),
            total_tsv_files=len(tsv_files),
            compatibility_issues=issues
        )

        # 요약
        error_count = sum(1 for i in issues if i.severity == "error")
        warning_count = sum(1 for i in issues if i.severity == "warning")

        self._log("✅ 덤프 분석 완료")
        self._log(f"  - 오류: {error_count}개")
        self._log(f"  - 경고: {warning_count}개")

        return result

    def _analyze_sql_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """
        SQL 파일 분석 - 스키마 호환성 검사

        Args:
            file_path: SQL 파일 경로

        Returns:
            발견된 이슈 목록
        """
        issues = []

        try:
            # 대용량 파일 가드레일: 100MB 초과 시 경고 후 스킵
            MAX_SQL_FILE_SIZE = 100 * 1024 * 1024  # 100MB
            file_size = file_path.stat().st_size
            if file_size > MAX_SQL_FILE_SIZE:
                self._log(f"  ⚠️ 파일 크기 초과 ({file_size // (1024*1024)}MB > 100MB): {file_path.name} - 스키마 분석 스킵")
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SCAN_TRUNCATED,
                    severity="warning",
                    location=file_path.name,
                    description=f"SQL 파일이 너무 큼 ({file_size // (1024*1024)}MB): 스키마 호환성 검사 스킵",
                    suggestion="파일을 분할하거나 라이브 DB 모드로 직접 분석하세요"
                ))
                return issues

            content = file_path.read_text(encoding='utf-8', errors='replace')

            # 1. ZEROFILL 속성 검사
            for match in ZEROFILL_PATTERN.finditer(content):
                # 컨텍스트에서 테이블/컬럼 이름 추출 시도
                line_start = content.rfind('\n', 0, match.start()) + 1
                line_end = content.find('\n', match.end())
                line = content[line_start:line_end]

                issues.append(CompatibilityIssue(
                    issue_type=IssueType.ZEROFILL_USAGE,
                    severity="warning",
                    location=f"{file_path.name}",
                    description=f"ZEROFILL 속성 사용: {line.strip()[:80]}...",
                    suggestion="ZEROFILL은 deprecated됨"
                ))

            # 2. FLOAT(M,D), DOUBLE(M,D) 구문 검사
            for match in FLOAT_PRECISION_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FLOAT_PRECISION,
                    severity="warning",
                    location=f"{file_path.name}",
                    description=f"FLOAT/DOUBLE 정밀도 구문: {match.group(0)}",
                    suggestion="FLOAT(M,D) 구문은 deprecated됨"
                ))

            # 3. FK 이름 64자 초과 검사
            for match in FK_NAME_LENGTH_PATTERN.finditer(content):
                fk_name = match.group(1)
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FK_NAME_LENGTH,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"FK 이름 64자 초과: {fk_name[:30]}... ({len(fk_name)}자)",
                    suggestion="FK 이름을 64자 이하로 변경 필요"
                ))

            # 4. 인증 플러그인 검사
            for match in AUTH_PLUGIN_PATTERN.finditer(content):
                plugin = match.group(1).lower()
                # removed(fido 계열)=error, disabled(native)=error, deprecated(sha256)=warning
                if plugin in ('authentication_fido', 'authentication_fido_client'):
                    severity = "error"
                    desc = f"{plugin} 플러그인 사용 (8.4에서 제거됨)"
                elif plugin == 'mysql_native_password':
                    severity = "error"
                    desc = f"{plugin} 인증 사용 (8.4에서 기본 비활성화)"
                else:
                    severity = "warning"
                    desc = f"{plugin} 인증 사용 (deprecated)"
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.AUTH_PLUGIN_ISSUE,
                    severity=severity,
                    location=f"{file_path.name}",
                    description=desc,
                    suggestion="caching_sha2_password 사용 권장"
                ))

            # 5. FTS_ 테이블명 검사
            for match in FTS_TABLE_PREFIX_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.FTS_TABLE_PREFIX,
                    severity="error",
                    location=f"{file_path.name}",
                    description="FTS_ 접두사 테이블명 (내부 예약어)",
                    suggestion="FTS_ 접두사는 내부 전문 검색용으로 예약됨, 테이블명 변경 필요"
                ))

            # 6. SUPER 권한 검사
            for match in SUPER_PRIVILEGE_PATTERN.finditer(content):
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.SUPER_PRIVILEGE,
                    severity="warning",
                    location=f"{file_path.name}",
                    description="SUPER 권한 사용 (deprecated)",
                    suggestion="동적 권한 (BINLOG_ADMIN, CONNECTION_ADMIN 등)으로 세분화 권장"
                ))

            # 7. 제거된 시스템 변수 사용 검사
            for match in SYS_VAR_USAGE_PATTERN.finditer(content):
                var_name = match.group(1)
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.REMOVED_SYS_VAR,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"제거된 시스템 변수 사용: {var_name}",
                    suggestion=f"'{var_name}'은 8.4에서 제거됨, 대체 방법 확인 필요"
                ))

            # 8. 예약어 충돌 (테이블/컬럼 이름) - CREATE TABLE 문에서
            table_pattern = re.compile(
                r'CREATE\s+TABLE\s+`?(\w+)`?\s*\(',
                re.IGNORECASE
            )
            column_pattern = re.compile(
                r'`(\w+)`\s+(?:INT|VARCHAR|TEXT|DATE|DECIMAL|FLOAT|DOUBLE|CHAR|BLOB|ENUM|SET)',
                re.IGNORECASE
            )

            keywords_upper = set(k.upper() for k in ALL_RESERVED_KEYWORDS)

            for match in table_pattern.finditer(content):
                table_name = match.group(1)
                if table_name.upper() in keywords_upper:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.RESERVED_KEYWORD,
                        severity="error",
                        location=f"{file_path.name}",
                        description=f"테이블명 '{table_name}'이 예약어와 충돌",
                        suggestion="테이블명 변경 또는 백틱(`) 사용 필요"
                    ))

            for match in column_pattern.finditer(content):
                column_name = match.group(1)
                if column_name.upper() in keywords_upper:
                    issues.append(CompatibilityIssue(
                        issue_type=IssueType.RESERVED_KEYWORD,
                        severity="warning",
                        location=f"{file_path.name}",
                        description=f"컬럼명 '{column_name}'이 예약어와 충돌",
                        suggestion="컬럼 참조 시 백틱(`) 사용 필요"
                    ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    def _analyze_tsv_file(self, file_path: Path) -> List[CompatibilityIssue]:
        """
        TSV 데이터 파일 분석 - 데이터 무결성 검사

        Args:
            file_path: TSV 파일 경로

        Returns:
            발견된 이슈 목록
        """
        issues = []
        invalid_date_count = 0

        try:
            # 대용량 파일은 샘플링
            max_lines = 10000
            line_count = 0

            with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    line_count += 1
                    if line_count > max_lines:
                        break

                    # 0000-00-00 날짜 검사
                    if INVALID_DATE_PATTERN.search(line) or INVALID_DATETIME_PATTERN.search(line):
                        invalid_date_count += 1

            if invalid_date_count > 0:
                issues.append(CompatibilityIssue(
                    issue_type=IssueType.INVALID_DATE,
                    severity="error",
                    location=f"{file_path.name}",
                    description=f"잘못된 날짜 값 발견: {invalid_date_count}개 행 (0000-00-00)",
                    suggestion="NO_ZERO_DATE SQL 모드 활성화 시 오류 발생, 유효한 날짜로 변환 필요"
                ))

        except Exception as e:
            self._log(f"  ⚠️ 파일 읽기 오류: {file_path.name} - {str(e)}")

        return issues

    def quick_scan(self, dump_path: str) -> Tuple[int, int, int]:
        """
        빠른 스캔 - 이슈 개수만 반환

        Args:
            dump_path: 덤프 폴더 경로

        Returns:
            (오류 수, 경고 수, 정보 수)
        """
        try:
            result = self.analyze_dump_folder(dump_path)
            error_count = sum(1 for i in result.compatibility_issues if i.severity == "error")
            warning_count = sum(1 for i in result.compatibility_issues if i.severity == "warning")
            info_count = sum(1 for i in result.compatibility_issues if i.severity == "info")
            return error_count, warning_count, info_count
        except Exception as e:
            self._log(f"  ⚠️ 요약 카운트 오류: {str(e)[:80]}")
            return 0, 0, 0
