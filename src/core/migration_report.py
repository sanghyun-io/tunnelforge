"""
Report Exporter 모듈

분석 결과를 다양한 형식(JSON, CSV, MySQL Shell 호환, SQL)으로 내보냅니다.
"""

import json
import csv
import io
from datetime import datetime
from typing import List, Dict, Optional
from pathlib import Path

from .migration_constants import IssueType, MYSQL_SHELL_CHECK_IDS


class ReportExporter:
    """분석 결과 리포트 내보내기"""

    def __init__(self, issues: list):
        self.issues = issues

    @property
    def summary(self) -> Dict[str, int]:
        """이슈 요약"""
        return {
            'total': len(self.issues),
            'error': sum(1 for i in self.issues if i.severity == 'error'),
            'warning': sum(1 for i in self.issues if i.severity == 'warning'),
            'info': sum(1 for i in self.issues if i.severity == 'info'),
        }

    @property
    def issues_by_type(self) -> Dict[str, int]:
        """이슈 타입별 개수"""
        by_type = {}
        for issue in self.issues:
            type_name = issue.issue_type.value if hasattr(issue.issue_type, 'value') else str(issue.issue_type)
            by_type[type_name] = by_type.get(type_name, 0) + 1
        return by_type

    # ================================================================
    # JSON 리포트
    # ================================================================
    def export_json(self, include_fix_queries: bool = True) -> str:
        """JSON 형식 리포트"""
        report = {
            'version': '1.0',
            'tool': 'TunnelForge MySQL 8.4 Upgrade Checker',
            'generated_at': datetime.now().isoformat(),
            'summary': self.summary,
            'issues_by_type': self.issues_by_type,
            'issues': []
        }

        for issue in self.issues:
            issue_dict = {
                'type': issue.issue_type.value if hasattr(issue.issue_type, 'value') else str(issue.issue_type),
                'severity': issue.severity,
                'location': issue.location,
                'description': issue.description,
                'suggestion': issue.suggestion,
            }

            if include_fix_queries and hasattr(issue, 'fix_query') and issue.fix_query:
                issue_dict['fix_query'] = issue.fix_query

            if hasattr(issue, 'doc_link') and issue.doc_link:
                issue_dict['doc_link'] = issue.doc_link

            if hasattr(issue, 'code_snippet') and issue.code_snippet:
                issue_dict['code'] = issue.code_snippet

            if hasattr(issue, 'table_name') and issue.table_name:
                issue_dict['table_name'] = issue.table_name

            if hasattr(issue, 'column_name') and issue.column_name:
                issue_dict['column_name'] = issue.column_name

            report['issues'].append(issue_dict)

        return json.dumps(report, indent=2, ensure_ascii=False)

    # ================================================================
    # CSV 리포트
    # ================================================================
    def export_csv(self) -> str:
        """CSV 형식 리포트"""
        output = io.StringIO()
        writer = csv.writer(output)

        # 헤더
        headers = [
            'Type', 'Severity', 'Location', 'Description',
            'Suggestion', 'Fix Query', 'Doc Link', 'Table', 'Column'
        ]
        writer.writerow(headers)

        # 데이터
        for issue in self.issues:
            writer.writerow([
                issue.issue_type.value if hasattr(issue.issue_type, 'value') else str(issue.issue_type),
                issue.severity,
                issue.location,
                issue.description,
                issue.suggestion,
                getattr(issue, 'fix_query', '') or '',
                getattr(issue, 'doc_link', '') or '',
                getattr(issue, 'table_name', '') or '',
                getattr(issue, 'column_name', '') or ''
            ])

        return output.getvalue()

    # ================================================================
    # MySQL Shell 호환 리포트
    # ================================================================
    def export_mysql_shell(self, source_path: str = "dump-analysis") -> str:
        """MySQL Shell 호환 형식 리포트"""
        lines = []

        lines.append("=" * 70)
        lines.append("MySQL Server Upgrade Compatibility Check")
        lines.append("=" * 70)
        lines.append("")
        lines.append(f"The MySQL data at {source_path}")
        lines.append("will now be checked for compatibility issues for upgrade to MySQL 8.4...")
        lines.append("")

        # 이슈를 check_id 기준으로 그룹화
        grouped = self._group_by_check_id()

        check_num = 1
        for check_id, check_issues in grouped.items():
            max_severity = 'Error' if any(i.severity == 'error' for i in check_issues) else 'Warning'

            # 첫 번째 이슈의 설명에서 카테고리 추출
            first_desc = check_issues[0].description
            category = first_desc.split(':')[0] if ':' in first_desc else first_desc[:50]

            lines.append(f"{check_num}) {category}")
            lines.append("")
            lines.append(f"  {max_severity}: {check_issues[0].description}")

            for issue in check_issues[:10]:  # 최대 10개만 표시
                lines.append(f"    - {issue.location}")

            if len(check_issues) > 10:
                lines.append(f"    ... and {len(check_issues) - 10} more")

            doc_link = getattr(check_issues[0], 'doc_link', None)
            if doc_link:
                lines.append(f"  More information: {doc_link}")

            lines.append("")
            check_num += 1

        # 요약
        lines.append("=" * 70)
        lines.append("Summary")
        lines.append("=" * 70)
        s = self.summary
        lines.append(f"Errors:   {s['error']}")
        lines.append(f"Warnings: {s['warning']}")
        lines.append(f"Notices:  {s['info']}")
        lines.append("")

        if s['error'] > 0:
            lines.append("MySQL upgrade check detected errors that need to be fixed before upgrading.")
        elif s['warning'] > 0:
            lines.append("MySQL upgrade check detected warnings that should be reviewed before upgrading.")
        else:
            lines.append("No issues found. Ready for upgrade to MySQL 8.4.")

        return "\n".join(lines)

    def _group_by_check_id(self) -> Dict[str, list]:
        """mysql_shell_check_id 또는 issue_type 기준 그룹화"""
        grouped = {}
        for issue in self.issues:
            # mysql_shell_check_id가 있으면 사용, 없으면 issue_type 사용
            if hasattr(issue, 'mysql_shell_check_id') and issue.mysql_shell_check_id:
                check_id = issue.mysql_shell_check_id
            elif hasattr(issue, 'issue_type'):
                check_id = MYSQL_SHELL_CHECK_IDS.get(issue.issue_type, issue.issue_type.value)
            else:
                check_id = 'unknown'

            if check_id not in grouped:
                grouped[check_id] = []
            grouped[check_id].append(issue)

        return grouped

    # ================================================================
    # Fix Queries SQL 파일
    # ================================================================
    def export_fix_queries_sql(self) -> str:
        """수정 SQL 모음 파일"""
        fixable = [i for i in self.issues if hasattr(i, 'fix_query') and i.fix_query]

        if not fixable:
            return "-- No fixable issues found"

        lines = [
            "-- ================================================================",
            "-- MySQL 8.0 → 8.4 Upgrade Fix Queries",
            f"-- Generated: {datetime.now().isoformat()}",
            f"-- Total fixes: {len(fixable)}",
            "-- ================================================================",
            "",
            "-- WARNING: Review these queries carefully before executing!",
            "-- Some queries may need adjustments for your specific environment.",
            "-- Back up your data before running any of these queries.",
            "",
            "-- START TRANSACTION;",
            ""
        ]

        # 이슈 타입별 그룹화
        by_type = {}
        for issue in fixable:
            type_name = issue.issue_type.value if hasattr(issue.issue_type, 'value') else str(issue.issue_type)
            if type_name not in by_type:
                by_type[type_name] = []
            by_type[type_name].append(issue)

        for type_name, type_issues in by_type.items():
            lines.append(f"-- {'=' * 60}")
            lines.append(f"-- {type_name.upper().replace('_', ' ')}")
            lines.append(f"-- {'=' * 60}")
            lines.append("")

            for i, issue in enumerate(type_issues, 1):
                desc = issue.description[:50] + "..." if len(issue.description) > 50 else issue.description
                lines.append(f"-- Fix {i}: {desc}")
                lines.append(f"-- Location: {issue.location}")

                # fix_query의 각 줄 추가
                for query_line in issue.fix_query.split('\n'):
                    lines.append(query_line)
                lines.append("")

        lines.append("-- ================================================================")
        lines.append("-- Uncomment one of the following after review:")
        lines.append("-- COMMIT;")
        lines.append("-- ROLLBACK;")
        lines.append("-- ================================================================")

        return "\n".join(lines)

    # ================================================================
    # HTML 리포트 (선택적)
    # ================================================================
    def export_html(self) -> str:
        """HTML 형식 리포트"""
        s = self.summary

        html = f"""<!DOCTYPE html>
<html lang="ko">
<head>
    <meta charset="UTF-8">
    <title>MySQL 8.4 Upgrade Check Report</title>
    <style>
        body {{ font-family: 'Segoe UI', Tahoma, sans-serif; margin: 20px; }}
        h1 {{ color: #333; }}
        .summary {{ background: #f5f5f5; padding: 15px; border-radius: 5px; margin-bottom: 20px; }}
        .summary span {{ margin-right: 20px; }}
        .error {{ color: #d32f2f; }}
        .warning {{ color: #f57c00; }}
        .info {{ color: #1976d2; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 20px; }}
        th, td {{ border: 1px solid #ddd; padding: 10px; text-align: left; }}
        th {{ background: #f0f0f0; }}
        tr:nth-child(even) {{ background: #fafafa; }}
        .fix-query {{ font-family: monospace; background: #f5f5f5; padding: 5px; font-size: 12px; }}
    </style>
</head>
<body>
    <h1>MySQL 8.4 Upgrade Check Report</h1>
    <p>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>

    <div class="summary">
        <h3>Summary</h3>
        <span class="error">Errors: {s['error']}</span>
        <span class="warning">Warnings: {s['warning']}</span>
        <span class="info">Info: {s['info']}</span>
        <span>Total: {s['total']}</span>
    </div>

    <table>
        <thead>
            <tr>
                <th>Severity</th>
                <th>Type</th>
                <th>Location</th>
                <th>Description</th>
                <th>Suggestion</th>
            </tr>
        </thead>
        <tbody>
"""

        for issue in self.issues:
            severity_class = issue.severity
            type_val = issue.issue_type.value if hasattr(issue.issue_type, 'value') else str(issue.issue_type)
            html += f"""            <tr>
                <td class="{severity_class}">{issue.severity.upper()}</td>
                <td>{type_val}</td>
                <td>{issue.location}</td>
                <td>{issue.description}</td>
                <td>{issue.suggestion}</td>
            </tr>
"""

        html += """        </tbody>
    </table>
</body>
</html>"""

        return html

    # ================================================================
    # 파일 저장 헬퍼
    # ================================================================
    def save_to_file(self, filepath: str, format: str = 'json'):
        """리포트를 파일로 저장"""
        exporters = {
            'json': self.export_json,
            'csv': self.export_csv,
            'mysql_shell': self.export_mysql_shell,
            'sql': self.export_fix_queries_sql,
            'html': self.export_html,
        }

        exporter = exporters.get(format.lower())
        if not exporter:
            raise ValueError(f"Unknown format: {format}. Available: {list(exporters.keys())}")

        content = exporter()

        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)

        return filepath

    def save_all_formats(self, base_path: str) -> Dict[str, str]:
        """모든 형식으로 저장"""
        base = Path(base_path)
        base.mkdir(parents=True, exist_ok=True)

        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        saved = {}

        formats = {
            'json': f'upgrade_check_{timestamp}.json',
            'csv': f'upgrade_check_{timestamp}.csv',
            'mysql_shell': f'upgrade_check_{timestamp}.txt',
            'sql': f'fix_queries_{timestamp}.sql',
            'html': f'upgrade_check_{timestamp}.html',
        }

        for fmt, filename in formats.items():
            filepath = base / filename
            self.save_to_file(str(filepath), fmt)
            saved[fmt] = str(filepath)

        return saved
