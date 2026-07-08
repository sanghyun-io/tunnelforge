"""
SQL 에디터 구문 강조 (기본 하이라이터 + 검증 결과 인라인 표시)
"""
from PyQt6.QtGui import QSyntaxHighlighter, QTextCharFormat, QColor, QFont
import re


class SQLHighlighter(QSyntaxHighlighter):
    """SQL 구문 하이라이팅"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._init_rules()

    def _init_rules(self):
        """하이라이팅 규칙 초기화"""
        self.highlighting_rules = []

        # 키워드 포맷
        keyword_format = QTextCharFormat()
        keyword_format.setForeground(QColor("#569CD6"))  # 파란색
        keyword_format.setFontWeight(QFont.Weight.Bold)

        keywords = [
            "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IN", "LIKE",
            "INSERT", "INTO", "VALUES", "UPDATE", "SET", "DELETE",
            "CREATE", "ALTER", "DROP", "TABLE", "INDEX", "VIEW", "DATABASE",
            "JOIN", "INNER", "LEFT", "RIGHT", "OUTER", "FULL", "CROSS", "ON",
            "GROUP", "BY", "ORDER", "ASC", "DESC", "HAVING", "LIMIT", "OFFSET",
            "UNION", "ALL", "DISTINCT", "AS", "CASE", "WHEN", "THEN", "ELSE", "END",
            "NULL", "IS", "BETWEEN", "EXISTS", "PRIMARY", "KEY", "FOREIGN",
            "REFERENCES", "CONSTRAINT", "DEFAULT", "AUTO_INCREMENT",
            "TRUNCATE", "BEGIN", "COMMIT", "ROLLBACK", "TRANSACTION",
            "IF", "ELSE", "WHILE", "DECLARE", "CURSOR", "FETCH", "PROCEDURE", "FUNCTION",
            "RETURNS", "RETURN", "CALL", "TRIGGER", "BEFORE", "AFTER", "FOR", "EACH", "ROW",
            "TRUE", "FALSE", "USE", "SHOW", "DESCRIBE", "EXPLAIN", "GRANT", "REVOKE"
        ]

        for word in keywords:
            pattern = rf"\b{word}\b"
            self.highlighting_rules.append((re.compile(pattern, re.IGNORECASE), keyword_format))

        # 함수 포맷
        function_format = QTextCharFormat()
        function_format.setForeground(QColor("#DCDCAA"))  # 노란색

        functions = [
            "COUNT", "SUM", "AVG", "MIN", "MAX", "COALESCE", "IFNULL", "NULLIF",
            "CONCAT", "SUBSTRING", "LENGTH", "TRIM", "UPPER", "LOWER", "REPLACE",
            "NOW", "DATE", "TIME", "DATETIME", "TIMESTAMP", "YEAR", "MONTH", "DAY",
            "HOUR", "MINUTE", "SECOND", "DATEDIFF", "DATE_ADD", "DATE_SUB",
            "CAST", "CONVERT", "ROUND", "FLOOR", "CEIL", "ABS", "MOD", "POWER",
            "GROUP_CONCAT", "JSON_EXTRACT", "JSON_ARRAY", "JSON_OBJECT"
        ]

        for word in functions:
            pattern = rf"\b{word}\s*\("
            self.highlighting_rules.append((re.compile(pattern, re.IGNORECASE), function_format))

        # 숫자 포맷
        number_format = QTextCharFormat()
        number_format.setForeground(QColor("#B5CEA8"))  # 연두색
        self.highlighting_rules.append((re.compile(r"\b\d+\.?\d*\b"), number_format))

        # 문자열 포맷 (작은따옴표)
        string_format = QTextCharFormat()
        string_format.setForeground(QColor("#CE9178"))  # 주황색
        self.highlighting_rules.append((re.compile(r"'[^']*'"), string_format))
        self.highlighting_rules.append((re.compile(r'"[^"]*"'), string_format))

        # 주석 포맷
        comment_format = QTextCharFormat()
        comment_format.setForeground(QColor("#6A9955"))  # 녹색
        comment_format.setFontItalic(True)
        self.highlighting_rules.append((re.compile(r"--[^\n]*"), comment_format))
        self.highlighting_rules.append((re.compile(r"#[^\n]*"), comment_format))

        # 멀티라인 주석 저장
        self.multiline_comment_format = comment_format

    def highlightBlock(self, text):
        """블록 하이라이팅"""
        # 일반 규칙 적용
        for pattern, format_ in self.highlighting_rules:
            for match in pattern.finditer(text):
                start = match.start()
                length = match.end() - start
                self.setFormat(start, length, format_)

        # 멀티라인 주석 처리
        self.setCurrentBlockState(0)

        start_index = 0
        if self.previousBlockState() != 1:
            start_match = re.search(r"/\*", text)
            start_index = start_match.start() if start_match else -1

        while start_index >= 0:
            end_match = re.search(r"\*/", text[start_index + 2:])
            if end_match:
                end_index = start_index + 2 + end_match.end()
                comment_length = end_index - start_index
            else:
                self.setCurrentBlockState(1)
                comment_length = len(text) - start_index

            self.setFormat(start_index, comment_length, self.multiline_comment_format)

            start_match = re.search(r"/\*", text[start_index + comment_length:])
            start_index = (start_index + comment_length + start_match.start()) if start_match else -1


class SQLValidatorHighlighter(SQLHighlighter):
    """SQL 구문 하이라이터 + 검증 이슈 밑줄 표시"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._issues = []  # ValidationIssue 목록
        self._issue_formats = {}  # line -> [(col, end_col, format), ...]

        # 에러 포맷 (빨간 물결 밑줄)
        self.error_format = QTextCharFormat()
        self.error_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        self.error_format.setUnderlineColor(QColor("#E74C3C"))  # 빨간색

        # 경고 포맷 (노란 물결 밑줄)
        self.warning_format = QTextCharFormat()
        self.warning_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
        self.warning_format.setUnderlineColor(QColor("#F39C12"))  # 노란색

        # 정보 포맷 (파란 밑줄)
        self.info_format = QTextCharFormat()
        self.info_format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SingleUnderline)
        self.info_format.setUnderlineColor(QColor("#3498DB"))  # 파란색

    def set_issues(self, issues: list):
        """검증 이슈 설정 및 재하이라이팅"""
        self._issues = issues
        self._build_issue_map()
        self.rehighlight()

    def _build_issue_map(self):
        """줄별 이슈 맵 생성"""
        self._issue_formats = {}

        for issue in self._issues:
            line = issue.line
            if line not in self._issue_formats:
                self._issue_formats[line] = []

            # 심각도에 따른 포맷 선택
            from src.core.sql_validator import IssueSeverity
            if issue.severity == IssueSeverity.ERROR:
                fmt = self.error_format
            elif issue.severity == IssueSeverity.WARNING:
                fmt = self.warning_format
            else:
                fmt = self.info_format

            self._issue_formats[line].append((issue.column, issue.end_column, fmt))

    def highlightBlock(self, text):
        """블록 하이라이팅 (기본 + 검증 이슈)"""
        # 기본 SQL 하이라이팅
        super().highlightBlock(text)

        # 검증 이슈 밑줄 추가
        block_number = self.currentBlock().blockNumber()
        if block_number in self._issue_formats:
            for col, end_col, fmt in self._issue_formats[block_number]:
                # 범위 검증
                start = max(0, col)
                length = min(end_col, len(text)) - start
                if length > 0:
                    self.setFormat(start, length, fmt)

    def get_issues(self) -> list:
        """현재 이슈 목록 반환"""
        return self._issues
