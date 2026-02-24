"""
MySQL 8.0 → 8.4 마이그레이션 Post-Migration Validator

마이그레이션 완료 후 수정된 이슈가 실제로 해결되었는지 검증하고,
수정 전후 비교 리포트를 생성합니다.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Dict, Optional, Any, Set
from pathlib import Path
import json

from src.core.db_connector import MySQLConnector
from src.core.migration_constants import IssueType


@dataclass
class ValidationResult:
    """검증 결과"""
    all_fixed: bool
    remaining_issues: List[Any] = field(default_factory=list)
    fixed_count: int = 0
    new_issues: List[Any] = field(default_factory=list)
    validation_time: str = ""

    def __post_init__(self):
        if not self.validation_time:
            self.validation_time = datetime.now().isoformat()


@dataclass
class MigrationReport:
    """마이그레이션 리포트"""
    schema: str
    started_at: str
    completed_at: str
    pre_issue_count: int
    post_issue_count: int
    fixed_issues: List[Any] = field(default_factory=list)
    remaining_issues: List[Any] = field(default_factory=list)
    new_issues: List[Any] = field(default_factory=list)
    success: bool = False
    execution_log: List[str] = field(default_factory=list)
    duration_seconds: float = 0.0
    execution_log_path: str = ""  # 전체 실행 로그 파일 경로

    def get_summary(self) -> Dict[str, Any]:
        """리포트 요약 반환"""
        return {
            'schema': self.schema,
            'success': self.success,
            'pre_issue_count': self.pre_issue_count,
            'post_issue_count': self.post_issue_count,
            'fixed_count': len(self.fixed_issues),
            'remaining_count': len(self.remaining_issues),
            'new_count': len(self.new_issues),
            'duration_seconds': self.duration_seconds
        }


class PostMigrationValidator:
    """마이그레이션 후 검증기"""

    def __init__(self, connector: MySQLConnector):
        """
        Args:
            connector: MySQL 연결 객체
        """
        self.connector = connector
        self._analyzer = None

    def _get_analyzer(self):
        """MigrationAnalyzer 인스턴스 (lazy import)"""
        if self._analyzer is None:
            from src.core.migration_analyzer import MigrationAnalyzer
            self._analyzer = MigrationAnalyzer(self.connector)
        return self._analyzer

    def validate(
        self,
        schema: str,
        pre_issues: List[Any]
    ) -> ValidationResult:
        """
        마이그레이션 후 검증 실행

        Args:
            schema: 스키마명
            pre_issues: 수정 전 이슈 목록 (CompatibilityIssue)

        Returns:
            ValidationResult
        """
        # 1. 동일한 분석 다시 실행
        analyzer = self._get_analyzer()
        post_analysis = analyzer.analyze_schema(schema)
        post_issues = post_analysis.compatibility_issues

        # 2. 이슈 키 집합 생성
        pre_set = {self._issue_key(i) for i in pre_issues}
        post_set = {self._issue_key(i) for i in post_issues}

        # 3. 비교
        fixed_keys = pre_set - post_set      # 해결된 이슈
        remaining_keys = pre_set & post_set  # 남은 이슈
        new_keys = post_set - pre_set        # 새로 발생한 이슈

        # 4. 이슈 객체로 변환
        remaining = [i for i in post_issues if self._issue_key(i) in remaining_keys]
        new = [i for i in post_issues if self._issue_key(i) in new_keys]

        return ValidationResult(
            all_fixed=(len(remaining_keys) == 0 and len(new_keys) == 0),
            remaining_issues=remaining,
            fixed_count=len(fixed_keys),
            new_issues=new,
            validation_time=datetime.now().isoformat()
        )

    def _issue_key(self, issue: Any) -> str:
        """
        이슈 고유 키 생성

        Args:
            issue: CompatibilityIssue

        Returns:
            고유 키 문자열
        """
        issue_type = getattr(issue, 'issue_type', None)
        if issue_type:
            type_name = issue_type.name if hasattr(issue_type, 'name') else str(issue_type)
        else:
            type_name = 'UNKNOWN'

        location = getattr(issue, 'location', '')
        table = getattr(issue, 'table_name', '') or ''
        column = getattr(issue, 'column_name', '') or ''

        return f"{type_name}:{location}:{table}:{column}"

    def generate_report(
        self,
        schema: str,
        pre_issues: List[Any],
        validation: ValidationResult,
        started_at: datetime,
        execution_log: List[str] = None
    ) -> MigrationReport:
        """
        마이그레이션 리포트 생성

        Args:
            schema: 스키마명
            pre_issues: 수정 전 이슈 목록
            validation: 검증 결과
            started_at: 시작 시간
            execution_log: 실행 로그

        Returns:
            MigrationReport
        """
        completed_at = datetime.now()
        duration = (completed_at - started_at).total_seconds()

        # 해결된 이슈 추출
        pre_set = {self._issue_key(i) for i in pre_issues}
        remaining_set = {self._issue_key(i) for i in validation.remaining_issues}
        fixed_keys = pre_set - remaining_set

        fixed_issues = [i for i in pre_issues if self._issue_key(i) in fixed_keys]

        return MigrationReport(
            schema=schema,
            started_at=started_at.isoformat(),
            completed_at=completed_at.isoformat(),
            pre_issue_count=len(pre_issues),
            post_issue_count=len(validation.remaining_issues) + len(validation.new_issues),
            fixed_issues=fixed_issues,
            remaining_issues=validation.remaining_issues,
            new_issues=validation.new_issues,
            success=validation.all_fixed,
            execution_log=execution_log or [],
            duration_seconds=duration
        )

    def check_data_integrity(self, schema: str) -> List[str]:
        """
        데이터 무결성 검사

        Args:
            schema: 스키마명

        Returns:
            발견된 문제 목록
        """
        problems = []

        # 1. FK 관계 확인 (고아 레코드)
        try:
            analyzer = self._get_analyzer()
            orphans = analyzer.find_orphan_records(schema)

            for orphan in orphans:
                problems.append(
                    f"고아 레코드 발견: {orphan.child_table}.{orphan.child_column} → "
                    f"{orphan.parent_table}.{orphan.parent_column} ({orphan.orphan_count}개)"
                )
        except Exception as e:
            problems.append(f"FK 관계 검사 실패: {str(e)}")

        # 2. NULL 제약 조건 위반 확인 (선택적)
        # 이 검사는 시간이 오래 걸릴 수 있어 기본적으로 스킵

        return problems

    def export_report_html(self, report: MigrationReport, output_path: str) -> str:
        """
        HTML 리포트 내보내기

        Args:
            report: MigrationReport
            output_path: 출력 파일 경로

        Returns:
            출력 파일 경로
        """
        # 이슈 목록을 HTML 테이블로 변환하는 헬퍼
        def issues_to_html(issues: List[Any], title: str) -> str:
            if not issues:
                return "<p>없음</p>"

            severity_colors = {
                'error': '#e74c3c',
                'warning': '#f39c12',
                'info': '#3498db',
            }

            rows = []
            for issue in issues:
                issue_type = getattr(issue, 'issue_type', 'N/A')
                if hasattr(issue_type, 'value'):
                    issue_type = issue_type.value
                location = getattr(issue, 'location', 'N/A')
                description = getattr(issue, 'description', 'N/A')
                severity = getattr(issue, 'severity', '')
                suggestion = getattr(issue, 'suggestion', '')

                sev_color = severity_colors.get(str(severity).lower(), '#7f8c8d')
                sev_badge = f'<span style="background-color:{sev_color};color:white;padding:2px 6px;border-radius:3px;font-size:0.85em;">{severity}</span>' if severity else ''

                rows.append(f"""
                <tr>
                    <td>{issue_type}</td>
                    <td style="font-family:monospace;font-size:0.9em;">{location}</td>
                    <td>{sev_badge}</td>
                    <td>{description}</td>
                    <td style="font-family:monospace;font-size:0.85em;color:#555;">{suggestion}</td>
                </tr>
                """)

            return f"""
            <p style="color:#7f8c8d;font-size:0.9em;">총 {len(issues)}개</p>
            <table border="1" cellpadding="5" cellspacing="0" style="border-collapse: collapse; width:100%;">
                <tr style="background-color: #f0f0f0;">
                    <th>타입</th>
                    <th>위치</th>
                    <th>심각도</th>
                    <th>설명</th>
                    <th>수정 방법</th>
                </tr>
                {''.join(rows)}
            </table>
            """

        # 상태 배지
        status_badge = (
            '<span style="background-color: #27ae60; color: white; padding: 5px 10px; border-radius: 4px;">✅ 성공</span>'
            if report.success else
            '<span style="background-color: #e74c3c; color: white; padding: 5px 10px; border-radius: 4px;">❌ 미완료</span>'
        )

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MySQL 마이그레이션 리포트 - {report.schema}</title>
    <style>
        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            margin: 20px;
            background-color: #f5f5f5;
        }}
        .container {{
            max-width: 1000px;
            margin: 0 auto;
            background-color: white;
            padding: 30px;
            border-radius: 8px;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}
        h1 {{
            color: #2c3e50;
            border-bottom: 2px solid #3498db;
            padding-bottom: 10px;
        }}
        h2 {{
            color: #34495e;
            margin-top: 30px;
        }}
        .summary {{
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 15px;
            margin: 20px 0;
        }}
        .summary-item {{
            background-color: #ecf0f1;
            padding: 15px;
            border-radius: 6px;
            text-align: center;
        }}
        .summary-item .number {{
            font-size: 2em;
            font-weight: bold;
            color: #2c3e50;
        }}
        .summary-item .label {{
            color: #7f8c8d;
        }}
        table {{
            width: 100%;
            margin-top: 10px;
        }}
        th, td {{
            text-align: left;
            padding: 8px;
        }}
        tr:nth-child(even) {{
            background-color: #f9f9f9;
        }}
        .footer {{
            margin-top: 30px;
            padding-top: 20px;
            border-top: 1px solid #ddd;
            color: #95a5a6;
            font-size: 0.9em;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔄 MySQL 8.0 → 8.4 마이그레이션 리포트</h1>

        <p><strong>스키마:</strong> {report.schema}</p>
        <p><strong>상태:</strong> {status_badge}</p>
        <p><strong>시작 시간:</strong> {report.started_at}</p>
        <p><strong>완료 시간:</strong> {report.completed_at}</p>
        <p><strong>소요 시간:</strong> {report.duration_seconds:.1f}초</p>

        <div class="summary">
            <div class="summary-item">
                <div class="number">{report.pre_issue_count}</div>
                <div class="label">수정 전 이슈</div>
            </div>
            <div class="summary-item">
                <div class="number" style="color: #27ae60;">{len(report.fixed_issues)}</div>
                <div class="label">해결됨</div>
            </div>
            <div class="summary-item">
                <div class="number" style="color: #f39c12;">{len(report.remaining_issues)}</div>
                <div class="label">남은 이슈</div>
            </div>
            <div class="summary-item">
                <div class="number" style="color: #e74c3c;">{len(report.new_issues)}</div>
                <div class="label">새 이슈</div>
            </div>
        </div>

        <h2>📝 해결된 이슈 ({len(report.fixed_issues)}개)</h2>
        {issues_to_html(report.fixed_issues, "해결된 이슈")}

        <h2>⚠️ 남은 이슈 ({len(report.remaining_issues)}개)</h2>
        {issues_to_html(report.remaining_issues, "남은 이슈")}

        <h2>🆕 새로 발견된 이슈 ({len(report.new_issues)}개)</h2>
        {issues_to_html(report.new_issues, "새 이슈")}

        <h2>📋 실행 로그</h2>
        <div style="background-color:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:6px;font-family:monospace;font-size:0.85em;max-height:400px;overflow-y:auto;white-space:pre-wrap;">{chr(10).join(report.execution_log) if report.execution_log else "로그 없음"}</div>

        <div class="footer">
            <p>이 리포트는 TunnelForge MySQL 마이그레이션 도구에 의해 생성되었습니다.</p>
            <p>생성 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>
"""

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html)

        return output_path

    def export_report_json(self, report: MigrationReport, output_path: str) -> str:
        """
        JSON 리포트 내보내기

        Args:
            report: MigrationReport
            output_path: 출력 파일 경로

        Returns:
            출력 파일 경로
        """
        # 이슈를 직렬화 가능한 형태로 변환
        def serialize_issues(issues: List[Any]) -> List[Dict]:
            result = []
            for issue in issues:
                issue_type = getattr(issue, 'issue_type', None)
                if issue_type:
                    type_value = issue_type.value if hasattr(issue_type, 'value') else str(issue_type)
                else:
                    type_value = 'unknown'

                result.append({
                    'type': type_value,
                    'severity': getattr(issue, 'severity', 'unknown'),
                    'location': getattr(issue, 'location', ''),
                    'description': getattr(issue, 'description', ''),
                    'suggestion': getattr(issue, 'suggestion', ''),
                    'table_name': getattr(issue, 'table_name', None),
                    'column_name': getattr(issue, 'column_name', None),
                })
            return result

        data = {
            'schema': report.schema,
            'success': report.success,
            'started_at': report.started_at,
            'completed_at': report.completed_at,
            'duration_seconds': report.duration_seconds,
            'summary': {
                'pre_issue_count': report.pre_issue_count,
                'post_issue_count': report.post_issue_count,
                'fixed_count': len(report.fixed_issues),
                'remaining_count': len(report.remaining_issues),
                'new_count': len(report.new_issues),
            },
            'fixed_issues': serialize_issues(report.fixed_issues),
            'remaining_issues': serialize_issues(report.remaining_issues),
            'new_issues': serialize_issues(report.new_issues),
            'execution_log': report.execution_log
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return output_path

    def quick_validate(self, schema: str, expected_fixes: int) -> Dict[str, Any]:
        """
        빠른 검증 (간단한 확인용)

        Args:
            schema: 스키마명
            expected_fixes: 예상 수정 개수

        Returns:
            검증 결과 딕셔너리
        """
        analyzer = self._get_analyzer()
        analysis = analyzer.analyze_schema(schema)

        current_count = len(analysis.compatibility_issues)

        return {
            'current_issue_count': current_count,
            'expected_fixes': expected_fixes,
            'validation_passed': current_count == 0 or expected_fixes > 0,
            'timestamp': datetime.now().isoformat()
        }
