"""
식별자 이슈 탐지용 컨텍스트 제한 매처

migration_constants.py에서 분리된 모듈. 아래 매처들은 DDL 식별자 컨텍스트로
스캔 범위를 제한해 문자열 리터럴/데이터 영역의 오탐을 줄인다.
"""
import re

# ============================================================
# 식별자 이슈 탐지용 컨텍스트 제한 매처
# ============================================================
# 배경: 단순 `[^`]*\$[^`]*` 형태의 raw 정규식은 백틱이 짝을 이루는지
# 확인하지 않아, 인접한 두 식별자 사이의 텍스트(따옴표 문자열 리터럴 포함)를
# 하나의 식별자로 오인해 대량의 오탐을 만들었다. 아래 헬퍼는 완전한 백틱
# 토큰(여는/닫는 백틱이 한 쌍인 구간) 단위로만 술어를 검사하고, 문자열
# 리터럴은 스캔 전에 마스킹하여 데이터 영역의 `$`/제어문자를 식별자 문제로
# 오인하지 않도록 한다. `.search()`/`.finditer()`는 기존 `re.Pattern`과
# 동일하게 실제 `re.Match` 객체를 반환하므로 호출부(`schema_rules.py`,
# 테스트)의 `match.group(0)` 사용은 변경 없이 그대로 동작한다.
class _IdentifierIssuePattern:
    """DDL 식별자 컨텍스트 안의 완전한 백틱 토큰만 검사하는 패턴 매처"""

    # CREATE/ALTER/DROP/RENAME TABLE, CREATE/DROP INDEX 등 식별자가
    # 정의/참조되는 DDL 구문 범위. 세미콜론까지(또는 세미콜론이 없으면
    # 문자열 끝까지)를 하나의 구문으로 간주하는 보수적인 스캐너.
    _DDL_CONTEXT_PATTERN = re.compile(
        r'\b(?:CREATE(?:\s+(?:UNIQUE|FULLTEXT|SPATIAL))?|ALTER|DROP|RENAME)'
        r'\s+(?:TABLE|INDEX)\b[^;]*;?',
        re.IGNORECASE | re.DOTALL,
    )
    # 완전한 백틱 토큰 하나(개행을 넘어가지 않음)
    _TOKEN_PATTERN = re.compile(r'`([^`\r\n]*)`')
    # 작은/큰따옴표 문자열 리터럴 (이스케이프 문자 포함)
    _STRING_LITERAL_PATTERN = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"", re.DOTALL)

    def __init__(self, predicate):
        self._predicate = predicate

    @classmethod
    def _mask_string_literals(cls, text: str) -> str:
        """문자열 리터럴 내부를 동일 길이의 'x'로 치환해 오프셋을 보존한다."""
        def _mask(m):
            s = m.group(0)
            if len(s) < 2:
                return s
            return s[0] + 'x' * (len(s) - 2) + s[-1]
        return cls._STRING_LITERAL_PATTERN.sub(_mask, text)

    def _iter_scan_spans(self, text: str):
        """DDL 컨텍스트 구간만 산출. 컨텍스트 키워드가 전혀 없으면
        (상수 단위 테스트처럼 식별자만 단독으로 주어진 입력) 전체 텍스트를
        그대로 하나의 구간으로 취급한다."""
        found = False
        for m in self._DDL_CONTEXT_PATTERN.finditer(text):
            found = True
            yield m.start(), m.group(0)
        if not found:
            yield 0, text

    def finditer(self, text: str):
        for offset, span_text in self._iter_scan_spans(text):
            masked = self._mask_string_literals(span_text)
            for m in self._TOKEN_PATTERN.finditer(masked):
                if self._predicate(m.group(1)):
                    real_match = self._TOKEN_PATTERN.match(text, offset + m.start())
                    if real_match:
                        yield real_match

    def search(self, text: str):
        for m in self.finditer(text):
            return m
        return None


class _ContextualDotPattern:
    """FROM/JOIN/INTO/UPDATE/TABLE/REFERENCES 키워드 바로 뒤에 오는
    식별자 참조에서만 연속 점(..) 오타를 검사하는 패턴 매처.

    키워드 제한 없이 원시 텍스트 전체를 스캔하면 INSERT 데이터나 문자열
    리터럴 안의 '..'까지 식별자 문제로 오인한다. 매치 시작 위치를 키워드
    바로 다음으로 고정하므로 `match.group(0)`에는 키워드가 포함되지 않는다.
    """

    _KEYWORD_PATTERN = re.compile(
        r'\b(?:FROM|JOIN|INTO|UPDATE|TABLE|REFERENCES)\s+',
        re.IGNORECASE,
    )
    _IDENTIFIER_PATTERN = re.compile(r'`?[\w$]+`?\s*\.\.\s*`?[\w$]+`?')

    def finditer(self, text: str):
        for kw_match in self._KEYWORD_PATTERN.finditer(text):
            id_match = self._IDENTIFIER_PATTERN.match(text, kw_match.end())
            if id_match:
                yield id_match

    def search(self, text: str):
        for m in self.finditer(text):
            return m
        return None


# 제어 문자 판별용 내부 문자 클래스
# \x09(tab)은 트레일링 스페이스 검사가 담당하고 \x0a/\x0d(개행)는 백틱
# 토큰이 개행을 넘어가지 않도록 이미 제외되므로 여기서는 제외한다.
_CONTROL_CHAR_INNER_PATTERN = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')

# 달러 기호 식별자 패턴 (DDL 식별자 컨텍스트로 제한, 문자열 리터럴 제외)
DOLLAR_SIGN_PATTERN = _IdentifierIssuePattern(lambda name: '$' in name)

# 트레일링 스페이스 식별자 패턴 (DDL 식별자 컨텍스트로 제한, 문자열 리터럴 제외)
TRAILING_SPACE_PATTERN = _IdentifierIssuePattern(
    lambda name: bool(name) and name[-1] in (' ', '\t')
)

# 제어 문자 식별자 패턴 (DDL 식별자 컨텍스트로 제한, 문자열 리터럴 제외)
CONTROL_CHAR_PATTERN = _IdentifierIssuePattern(
    lambda name: bool(_CONTROL_CHAR_INNER_PATTERN.search(name))
)

# 식별자에 연속 점(..) 사용 패턴 (schema..table 또는 ..table 형태)
# FROM/JOIN/INTO/UPDATE/TABLE/REFERENCES 등 식별자 참조 컨텍스트로 제한하여
# INSERT 데이터나 문자열 리터럴 내부의 '..'를 오탐하지 않도록 한다.
INVALID_57_NAME_MULTIPLE_DOTS_PATTERN = _ContextualDotPattern()
