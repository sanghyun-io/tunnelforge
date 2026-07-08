"""
MySQL 마이그레이션 리포트 렌더러

Post-Migration 리포트(MigrationReport)를 HTML/JSON으로 내보냅니다.
DB 커넥터에 의존하지 않는(connector-free) 렌더러이며, Rust DB Core가 보내는
dict 형태 이슈와 레거시/테스트 경로의 object/dataclass 이슈를 모두 지원합니다.
"""
import html
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List


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
            'duration_seconds': self.duration_seconds,
        }


def _issue_field(issue: Any, key: str, default: Any = "") -> Any:
    """dict 이슈(Rust payload)와 object/dataclass 이슈(레거시/테스트) 모두에서 값 추출"""
    if isinstance(issue, dict):
        return issue.get(key, default)
    return getattr(issue, key, default)


def _issue_description(issue: Any) -> str:
    """Rust MigrationIssue는 'message', object 이슈(CompatibilityIssue)는
    'description'에 본문 텍스트가 있다. 둘 다 지원한다."""
    value = _issue_field(issue, 'description', '')
    if value:
        return value
    return _issue_field(issue, 'message', '') or ''


def _issue_type_str(issue: Any) -> str:
    """dict 이슈는 issue_type이 이미 문자열, object 이슈는 IssueType enum일 수 있다."""
    issue_type = _issue_field(issue, 'issue_type', '')
    if issue_type is None:
        return ''
    if hasattr(issue_type, 'value'):
        return issue_type.value
    return str(issue_type)


class MigrationReportRenderer:
    """MigrationReport를 HTML/JSON으로 내보내는 connector-free 렌더러"""

    def export_report_html(self, report: MigrationReport, output_path: str) -> str:
        """
        HTML 리포트 내보내기

        Args:
            report: MigrationReport
            output_path: 출력 파일 경로

        Returns:
            출력 파일 경로
        """
        def esc(value: Any) -> str:
            return html.escape(str(value), quote=True)

        def issues_to_html(issues: List[Any]) -> str:
            if not issues:
                return "<p>없음</p>"

            severity_colors = {
                'error': '#e74c3c',
                'warning': '#f39c12',
                'info': '#3498db',
            }

            rows = []
            for issue in issues:
                issue_type = esc(_issue_type_str(issue) or 'N/A')
                location = esc(_issue_field(issue, 'location', 'N/A'))
                description = esc(_issue_description(issue) or 'N/A')
                severity = _issue_field(issue, 'severity', '')
                suggestion = esc(_issue_field(issue, 'suggestion', ''))

                sev_color = severity_colors.get(str(severity).lower(), '#7f8c8d')
                sev_badge = (
                    f'<span style="background-color:{sev_color};color:white;padding:2px 6px;'
                    f'border-radius:3px;font-size:0.85em;">{esc(severity)}</span>'
                ) if severity else ''

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

        status_badge = (
            '<span style="background-color: #27ae60; color: white; padding: 5px 10px; border-radius: 4px;">✅ 성공</span>'
            if report.success else
            '<span style="background-color: #e74c3c; color: white; padding: 5px 10px; border-radius: 4px;">❌ 미완료</span>'
        )

        if report.execution_log:
            execution_log_html = "\n".join(esc(line) for line in report.execution_log)
        else:
            execution_log_html = "로그 없음"

        html_doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>MySQL 마이그레이션 리포트 - {esc(report.schema)}</title>
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

        <p><strong>스키마:</strong> {esc(report.schema)}</p>
        <p><strong>상태:</strong> {status_badge}</p>
        <p><strong>시작 시간:</strong> {esc(report.started_at)}</p>
        <p><strong>완료 시간:</strong> {esc(report.completed_at)}</p>
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
        {issues_to_html(report.fixed_issues)}

        <h2>⚠️ 남은 이슈 ({len(report.remaining_issues)}개)</h2>
        {issues_to_html(report.remaining_issues)}

        <h2>🆕 새로 발견된 이슈 ({len(report.new_issues)}개)</h2>
        {issues_to_html(report.new_issues)}

        <h2>📋 실행 로그</h2>
        <div style="background-color:#1e1e1e;color:#d4d4d4;padding:15px;border-radius:6px;font-family:monospace;font-size:0.85em;max-height:400px;overflow-y:auto;white-space:pre-wrap;">{execution_log_html}</div>

        <div class="footer">
            <p>이 리포트는 TunnelForge MySQL 마이그레이션 도구에 의해 생성되었습니다.</p>
            <p>생성 시간: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </div>
</body>
</html>
"""

        with open(output_path, 'w', encoding='utf-8') as f:
            f.write(html_doc)

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
        def serialize_issues(issues: List[Any]) -> List[Dict]:
            result = []
            for issue in issues:
                result.append({
                    'type': _issue_type_str(issue) or 'unknown',
                    'severity': _issue_field(issue, 'severity', 'unknown'),
                    'location': _issue_field(issue, 'location', ''),
                    'description': _issue_description(issue),
                    'suggestion': _issue_field(issue, 'suggestion', ''),
                    'table_name': _issue_field(issue, 'table_name', None),
                    'column_name': _issue_field(issue, 'column_name', None),
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
            'execution_log': report.execution_log,
        }

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

        return output_path
