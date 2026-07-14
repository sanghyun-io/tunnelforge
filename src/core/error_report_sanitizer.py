"""Fail-closed scalar sanitization for anonymous error reports."""

import re
import unicodedata


_REDACTED = "REDACTED"

_PRIVATE_KEY_PATTERN = re.compile(
    r"-----BEGIN\s+[^-\r\n]*PRIVATE KEY-----"
    r"(?:.*?-----END\s+[^-\r\n]*PRIVATE KEY-----|.*\Z)",
    re.IGNORECASE | re.DOTALL,
)


def _separator_insensitive_keys(keys):
    patterns = []
    for key in keys:
        characters = [character for character in key if character.isalnum()]
        patterns.append(r"[\s_-]*".join(map(re.escape, characters)))
    return "(?:" + "|".join(patterns) + ")"


_PRIVACY_SENSITIVE_KEYS = _separator_insensitive_keys(
    (
        "password",
        "passwd",
        "pwd",
        "api_key",
        "access_token",
        "refresh_token",
        "token",
        "aws_secret_access_key",
        "aws_access_key_id",
        "aws_session_token",
        "aws_security_token",
        "secret",
        "cookie",
        "session",
        "authorization",
        "pgpassword",
        "pgpassfile",
        "mysql_pwd",
        "mysql_password",
        "oracle_pwd",
    )
)
_IDENTITY_ASSIGNMENT_KEYS = _separator_insensitive_keys(
    ("user", "username", "uid", "user_id")
)
_ESCAPED_ASSIGNMENT_DELIMITER = r"(?:[:=]|\\+(?:u003a|u003d|x3a|x3d))"
_SENSITIVE_ASSIGNMENT_REMAINDER_PATTERN = re.compile(
    rf"(?<!\w)(?:{_PRIVACY_SENSITIVE_KEYS}|{_IDENTITY_ASSIGNMENT_KEYS})"
    rf"\s*{_ESCAPED_ASSIGNMENT_DELIMITER}.*$",
    re.IGNORECASE | re.DOTALL,
)
_PRIVACY_CREDENTIAL_REMAINDER_PATTERN = re.compile(
    rf"(?<!\w){_PRIVACY_SENSITIVE_KEYS}\s+(?:is|was)\s+.*$",
    re.IGNORECASE | re.DOTALL,
)
_CREDENTIAL_USER_REMAINDER_PATTERN = re.compile(
    r"\b(?:(?:access\s+denied|authentication\s+failed|login\s+failed)"
    r"\s+for\s+user|(?:authenticated|connected|logged\s+in)\s+as\s+user)\b.*$",
    re.IGNORECASE | re.DOTALL,
)
_NETWORK_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:host(?:name)?|server|port|source[_-]?ip|destination[_-]?ip)"
    rf"\s*{_ESCAPED_ASSIGNMENT_DELIMITER}\s*[^\s,;]+",
    re.IGNORECASE,
)
_DSN_ASSIGNMENT_PATTERN = re.compile(
    r"\b(?:dsn|data\s+source|dbname|database|initial\s+catalog)"
    rf"\s*{_ESCAPED_ASSIGNMENT_DELIMITER}\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)",
    re.IGNORECASE,
)
_LABELED_IDENTITY_PATTERN = re.compile(
    r"\b(?:account(?:\s+name)?|computer(?:\s+name)?|"
    r"machine(?:\s+name)?|host(?:name)?|server|port|customer|tenant|person)\s*"
    rf"(?:(?:{_ESCAPED_ASSIGNMENT_DELIMITER}\s*)|\s+)(?:\"[^\"]*\"|'[^']*'|"
    r"[^\s,;]+(?:\s+[A-Z][A-Za-z'-]*){0,3})",
    re.IGNORECASE,
)
_CLI_CONNECTION_REMAINDER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"--(?:host|port|user(?:name)?|password)(?:\s+|=)|"
    r"-[hPup](?:\s+|=)?"
    r").*$",
    re.IGNORECASE | re.DOTALL,
)
_CWD_REMAINDER_PATTERN = re.compile(
    rf"(?<!\w)cwd\s*{_ESCAPED_ASSIGNMENT_DELIMITER}.*$",
    re.IGNORECASE | re.DOTALL,
)
_URL_PATTERN = re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s]+", re.IGNORECASE)
_EMAIL_PATTERN = re.compile(
    r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE
)
_UNC_PATH_PATTERN = re.compile(r"\\\\[^\r\n,;]+")
_WINDOWS_PATH_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\r\n,;]+"
)
_POSIX_PATH_PATTERN = re.compile(r"(?<![A-Za-z0-9_])/(?:[^\r\n,;]+)")
_IPV6_PATTERN = re.compile(
    r"\[?(?=[0-9A-Fa-f:]*:)[0-9A-Fa-f]{0,4}"
    r"(?::[0-9A-Fa-f]{0,4}){2,}\]?(?::\d{1,5})?"
)
_IPV4_PATTERN = re.compile(
    r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d{1,5})?\b"
)
_HOST_PORT_PATTERN = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})\.)*"
    r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}):\d{1,5}\b"
)
_HOSTNAME_PATTERN = re.compile(
    r"\b(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})\.)+"
    r"[A-Za-z]{2,63}\b"
)
_HOSTLIKE_SERVICE = (
    r"(?:db|sql|mysql|mariadb|postgres(?:ql)?|oracle|redis|cache|api|app|web|"
    r"worker|node)"
)
_HOSTLIKE_ROLE = (
    r"(?:prod(?:uction)?|stag(?:e|ing)|dev(?:elopment)?|test(?:ing)?|qa|uat|"
    r"primary|replica|standby|leader|follower|reader|writer|read|write|ro|rw|"
    r"backup|main|secondary|blue|green|internal|external|local|remote)"
)
_HOSTLIKE_NAME_PATTERN = re.compile(
    r"\b(?=[A-Za-z0-9-]{3,63}\b)"
    r"(?:"
    rf"{_HOSTLIKE_SERVICE}-{_HOSTLIKE_ROLE}\d*(?:-[A-Za-z0-9]+)*|"
    rf"{_HOSTLIKE_ROLE}-{_HOSTLIKE_SERVICE}"
    rf"(?:-{_HOSTLIKE_ROLE})?\d*(?:-[A-Za-z0-9]+)*"
    r")\b",
    re.IGNORECASE,
)
_SQL_IDENTIFIER = r"[`\"A-Za-z_][A-Za-z0-9_$.`\"]*"
_SQL_LOCK_RELATION = rf"(?:ONLY\s+)?{_SQL_IDENTIFIER}\s*\*?"
_SQL_LOCK_RELATIONS = (
    rf"{_SQL_LOCK_RELATION}(?:\s*,\s*{_SQL_LOCK_RELATION})*"
)
_SQL_LOCK_MODE = (
    r"(?:ACCESS\s+SHARE|ROW\s+SHARE|ROW\s+EXCLUSIVE|"
    r"SHARE\s+UPDATE\s+EXCLUSIVE|SHARE|SHARE\s+ROW\s+EXCLUSIVE|EXCLUSIVE|"
    r"ACCESS\s+EXCLUSIVE)"
)
_LEADING_LOCK_REMAINDER_PATTERN = re.compile(
    r"\A\s*LOCK\s+(?=[^\s,;]).*$",
    re.IGNORECASE | re.DOTALL,
)
_SQL_PATTERN = re.compile(
    r"\b(?:"
    r"SELECT\b(?=[^;\r\n]*\bFROM\b)|"
    r"INSERT\s+INTO\b|REPLACE\s+INTO\b|"
    r"UPDATE\b(?=\s+[`\"A-Za-z_][^;\r\n]*\bSET\b)|"
    r"DELETE\s+FROM\b|"
    r"CREATE\s+(?:DATABASE|SCHEMA|TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|"
    r"TRIGGER|SEQUENCE)\b|"
    r"ALTER\s+(?:DATABASE|SCHEMA|TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|"
    r"TRIGGER|SEQUENCE)\b|"
    r"DROP\s+(?:DATABASE|SCHEMA|TABLE|VIEW|INDEX|PROCEDURE|FUNCTION|"
    r"TRIGGER|SEQUENCE)\b|"
    r"TRUNCATE(?:\s+TABLE)?\b|GRANT\b(?=[^;\r\n]*\bON\b)|"
    r"REVOKE\b(?=[^;\r\n]*\bON\b)|MERGE\s+INTO\b|"
    r"CALL\b(?=\s+[`\"A-Za-z_][A-Za-z0-9_$.`\"]*\s*\()|"
    r"EXEC(?:UTE)?\b(?=\s+[`\"A-Za-z_])|"
    r"WITH\b(?=[^;\r\n]*\bAS\s*\()|"
    r"SHOW\s+(?:DATABASES|SCHEMAS|TABLES|COLUMNS|INDEX(?:ES)?|CREATE)\b|"
    r"(?:DESCRIBE|DESC)\b(?=\s+[`\"A-Za-z_])|"
    r"EXPLAIN\b(?=\s+(?:ANALYZE\s+)?(?:SELECT|INSERT|UPDATE|DELETE|WITH)\b)|"
    r"USE\b(?=\s+[`\"A-Za-z_][A-Za-z0-9_$.`\"]*\s*(?:;|$))|"
    r"SET\b(?=\s+(?:(?:SESSION|LOCAL|GLOBAL)\s+)?[A-Za-z_@]"
    r"[A-Za-z0-9_$.@]*\s*(?:=|TO)\s*)|"
    r"BEGIN(?:\s+(?:WORK|TRANSACTION))?(?=\s*(?:;|$))|"
    r"(?:COMMIT|ROLLBACK)(?:\s+(?:WORK|TRANSACTION))?(?=\s*(?:;|$))|"
    r"COPY\b(?=[^;\r\n]*\b(?:FROM|TO)\b)|"
    r"VACUUM\b(?=\s+(?:(?:FULL|FREEZE|ANALYZE|VERBOSE)\s+)*[`\"A-Za-z_])|"
    r"ANALYZE(?:\s+TABLE)?\b(?=\s+[`\"A-Za-z_])|"
    r"REINDEX(?:\s+(?:TABLE|INDEX|SCHEMA|DATABASE|SYSTEM))?\b"
    r"(?=\s+[`\"A-Za-z_])|"
    r"PRAGMA\b(?=\s+[`\"A-Za-z_])|"
    rf"LOCK\s+TABLES\s+{_SQL_IDENTIFIER}"
    rf"(?:\s+AS\s+{_SQL_IDENTIFIER})?\s+"
    r"(?:READ(?:\s+LOCAL)?|(?:LOW_PRIORITY\s+)?WRITE)\b|"
    rf"LOCK\s+TABLE\s+{_SQL_LOCK_RELATIONS}"
    rf"(?=\s+IN\s+{_SQL_LOCK_MODE}\s+MODE\b|\s+NOWAIT\b|\s*(?:;|$))|"
    rf"LOCK\s+ONLY\s+{_SQL_IDENTIFIER}\s*\*?"
    rf"(?:\s*,\s*{_SQL_LOCK_RELATION})*"
    rf"(?=\s+IN\s+{_SQL_LOCK_MODE}\s+MODE\b|\s+NOWAIT\b|\s*(?:;|$))|"
    rf"LOCK\s+{_SQL_LOCK_RELATIONS}"
    rf"(?=\s+IN\s+{_SQL_LOCK_MODE}\s+MODE\b|\s+NOWAIT\b)"
    r")\b.*?(?:;|$)",
    re.IGNORECASE | re.DOTALL,
)
_CONSTRAINT_KEY_VALUE_PATTERN = re.compile(
    r"\bkey\s*\([^)]*\)\s*=\s*\([^)]*\)", re.IGNORECASE
)
_QUOTED_VALUE_PATTERN = re.compile(r"`[^`]*`|\"[^\"]*\"|'[^']*'")
_DB_OBJECT_PATTERN = re.compile(
    r"\b(?:database|schema|table|column|index|constraint|relation|view|"
    r"procedure|trigger|sequence)"
    r"(?:\s+name)?\s*(?:(?:[:=]\s*)|\s+)"
    r"(?:\"[^\"]*\"|'[^']*'|`[^`]*`|[A-Za-z_][A-Za-z0-9_$.-]*)",
    re.IGNORECASE,
)
_ENV_REFERENCE_PATTERN = re.compile(
    r"%[A-Za-z_][A-Za-z0-9_]*%|\$\{[A-Za-z_][A-Za-z0-9_]*\}|"
    r"\$[A-Za-z_][A-Za-z0-9_]*"
)
_JWT_PATTERN = re.compile(
    r"\b[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)
_KNOWN_SECRET_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_-])(?:"
    r"gh[pousr]_[A-Za-z0-9]{8,}|"
    r"github_pat_[A-Za-z0-9_]{8,}|"
    r"glpat-[A-Za-z0-9_-]{8,}|"
    r"sk-(?:proj-)?[A-Za-z0-9_-]{8,}|"
    r"xox[baprcs]-[A-Za-z0-9-]{8,}"
    r")(?![A-Za-z0-9_-])",
    re.IGNORECASE,
)
_AWS_ACCESS_KEY_ID_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Za-z0-9])"
)
_BEARER_TOKEN_PATTERN = re.compile(
    r"(?<!\w)Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE
)
_MARKDOWN_CONTROLS_PATTERN = re.compile(r"[`*_~#>|\[\]()!]")
_WHITESPACE_PATTERN = re.compile(r"\s+")

_LOCAL_HEX_SEPARATOR_ESCAPE = (
    r"\\+x(?!(?:3[0-9]|4[1-9a-f]|5[0-9a]|6[1-9a-f]|7[0-9a]))"
    r"[0-9a-f]{2}"
)
_LOCAL_KEY_ESCAPE = (
    rf"(?:\\+(?:[nrt]|u[0-9a-f]{{4}}|U[0-9a-f]{{8}})|"
    rf"{_LOCAL_HEX_SEPARATOR_ESCAPE})"
)
_LOCAL_KEY_SEPARATOR = (
    r"(?:[\s_\-\x00-\x1f\x7f\u200b-\u200f\u202a-\u202e"
    rf"\u2060-\u206f\ufeff]|{_LOCAL_KEY_ESCAPE})*"
)


def _local_key_character(character):
    codepoints = {ord(character)}
    if character.isalpha():
        codepoints.update((ord(character.lower()), ord(character.upper())))
    forms = [re.escape(character)]
    for codepoint in sorted(codepoints):
        forms.extend(
            (rf"\\+u{codepoint:04x}", rf"\\+U{codepoint:08x}")
        )
        if codepoint <= 0xFF:
            forms.append(rf"\\+x{codepoint:02x}")
    return "(?:" + "|".join(forms) + ")"


def _local_sensitive_keys(keys):
    patterns = []
    for key in keys:
        characters = [character for character in key if character.isalnum()]
        patterns.append(
            _LOCAL_KEY_SEPARATOR.join(
                _local_key_character(character) for character in characters
            )
        )
    return "(?:" + "|".join(patterns) + ")"


_LOCAL_CREDENTIAL_KEYS = _local_sensitive_keys(
    (
        "password",
        "passwd",
        "pwd",
        "api_key",
        "access_token",
        "refresh_token",
        "token",
        "aws_secret_access_key",
        "aws_access_key_id",
        "aws_session_token",
        "aws_security_token",
        "secret",
        "client_secret",
        "private_key",
        "authorization",
        "cookie",
        "pgpassword",
        "mysql_pwd",
    )
)
_LOCAL_ESCAPED_ASSIGNMENT_DELIMITER = r"(?:[:=]|\\+(?:u003a|u003d|x3a|x3d))"
_ESCAPED_SENSITIVE_ASSIGNMENT_REMAINDER_PATTERN = re.compile(
    rf"(?<!\w){_LOCAL_CREDENTIAL_KEYS}\s*{_LOCAL_ESCAPED_ASSIGNMENT_DELIMITER}.*$",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_ASSIGNMENT_KEYS = rf"(?:{_LOCAL_CREDENTIAL_KEYS}|credentials)"
_LOCAL_DSN_IDENTITY_KEYS = _local_sensitive_keys(
    ("user", "username", "user_id", "uid")
)
_LOCAL_QUOTE = r"\\*[\"']"
_LOCAL_ESCAPED_CONTROL = r"\\(?:[nrt]|x[0-9a-f]{2}|u[0-9a-f]{4}|U[0-9a-f]{8})"
_LOCAL_VALUE_BOUNDARY = (
    rf"(?=\s+(?:for\s+)?(?:table|schema|database|relation|column|index)\b|"
    rf"[\r\n,;]|{_LOCAL_ESCAPED_CONTROL}|{_LOCAL_QUOTE}|\Z)"
)
_LOCAL_UNQUOTED_VALUE_BOUNDARY = (
    rf"(?=[,;]|(?:\s|{_LOCAL_ESCAPED_CONTROL})+"
    rf"(?:for\s+)?(?:table|schema|database|relation|column|index)\b|"
    rf"\Z)"
)
_LOCAL_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>(?:{_LOCAL_QUOTE})?{_LOCAL_ASSIGNMENT_KEYS}"
    rf"(?:{_LOCAL_QUOTE})?\s*{_LOCAL_ESCAPED_ASSIGNMENT_DELIMITER}\s*)"
    rf"(?P<quote>{_LOCAL_QUOTE}).*?(?P=quote)(?=[\s,;}}\]]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_UNTERMINATED_QUOTED_ASSIGNMENT_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>(?:{_LOCAL_QUOTE})?{_LOCAL_ASSIGNMENT_KEYS}"
    rf"(?:{_LOCAL_QUOTE})?\s*{_LOCAL_ESCAPED_ASSIGNMENT_DELIMITER}\s*)(?P<quote>{_LOCAL_QUOTE})"
    rf"(?:(?!(?P=quote)(?=[\s,;}}\]]|\Z)).)*\Z",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_ASSIGNMENT_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>(?:{_LOCAL_QUOTE})?{_LOCAL_ASSIGNMENT_KEYS}"
    rf"(?:{_LOCAL_QUOTE})?\s*{_LOCAL_ESCAPED_ASSIGNMENT_DELIMITER}\s*)"
    rf"(?!\s*{_LOCAL_QUOTE})"
    rf"(?!{_REDACTED}{_LOCAL_UNQUOTED_VALUE_BOUNDARY})"
    rf".+?{_LOCAL_UNQUOTED_VALUE_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_DSN_IDENTITY_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>{_LOCAL_DSN_IDENTITY_KEYS}\s*{_LOCAL_ESCAPED_ASSIGNMENT_DELIMITER}\s*)"
    rf"(?!{_REDACTED}{_LOCAL_UNQUOTED_VALUE_BOUNDARY})"
    rf".+?{_LOCAL_UNQUOTED_VALUE_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_QUOTED_CREDENTIAL_SENTENCE_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>(?:the\s+)?{_LOCAL_CREDENTIAL_KEYS}"
    rf"\s+(?:is|was)\s+)(?P<quote>{_LOCAL_QUOTE})"
    rf".*?(?P=quote)(?=[\s,;}}\]]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_UNTERMINATED_QUOTED_CREDENTIAL_SENTENCE_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>(?:the\s+)?{_LOCAL_CREDENTIAL_KEYS}"
    rf"\s+(?:is|was)\s+)(?P<quote>{_LOCAL_QUOTE})"
    rf"(?:(?!(?P=quote)(?=[\s,;}}\]]|\Z)).)*\Z",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_CREDENTIAL_SENTENCE_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>(?:the\s+)?{_LOCAL_CREDENTIAL_KEYS}"
    rf"\s+(?:is|was)\s+)"
    rf"(?!\s*{_LOCAL_QUOTE})"
    rf"(?!{_REDACTED}{_LOCAL_UNQUOTED_VALUE_BOUNDARY})"
    rf".+?{_LOCAL_UNQUOTED_VALUE_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_USING_QUOTED_CREDENTIAL_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>using\s+(?:the\s+)?{_LOCAL_CREDENTIAL_KEYS}\s+)"
    rf"(?P<quote>{_LOCAL_QUOTE}).*?(?P=quote)(?=[\s,;}}\]]|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_USING_UNTERMINATED_QUOTED_CREDENTIAL_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>using\s+(?:the\s+)?{_LOCAL_CREDENTIAL_KEYS}\s+)"
    rf"(?P<quote>{_LOCAL_QUOTE})"
    rf"(?:(?!(?P=quote)(?=[\s,;}}\]]|\Z)).)*\Z",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_USING_CREDENTIAL_PATTERN = re.compile(
    rf"(?<!\w)(?P<prefix>using\s+(?:the\s+)?{_LOCAL_CREDENTIAL_KEYS}\s+)"
    rf"(?!\s*{_LOCAL_QUOTE})"
    rf"(?!{_REDACTED}{_LOCAL_UNQUOTED_VALUE_BOUNDARY})"
    rf".+?{_LOCAL_UNQUOTED_VALUE_BOUNDARY}",
    re.IGNORECASE | re.DOTALL,
)
_LOCAL_CLI_CREDENTIAL_PATTERN = re.compile(
    r"(?<![A-Za-z0-9_])((?:--(?:password|passwd|pwd|token|api-key|secret|"
    r"user(?:name)?)(?:\s+|=)|-[up](?:\s+|=)?))"
    r"(?:\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;]+)",
    re.IGNORECASE,
)
_LOCAL_CREDENTIAL_URL_PATTERN = re.compile(
    r"\b([A-Za-z][A-Za-z0-9+.-]*://)[^\s/?#]*@",
    re.IGNORECASE,
)
_LOCAL_BEARER_TOKEN_PATTERN = re.compile(
    r"(?<!\w)(Bearer\s+)[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE
)
_HIGH_ENTROPY_TOKEN_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])([A-Za-z0-9+/_=-]{32,})(?![A-Za-z0-9])"
)

_REDACTION_PATTERNS = (
    _PRIVATE_KEY_PATTERN,
    _ESCAPED_SENSITIVE_ASSIGNMENT_REMAINDER_PATTERN,
    _SENSITIVE_ASSIGNMENT_REMAINDER_PATTERN,
    _PRIVACY_CREDENTIAL_REMAINDER_PATTERN,
    _CREDENTIAL_USER_REMAINDER_PATTERN,
    _CLI_CONNECTION_REMAINDER_PATTERN,
    _CWD_REMAINDER_PATTERN,
    _URL_PATTERN,
    _EMAIL_PATTERN,
    _UNC_PATH_PATTERN,
    _WINDOWS_PATH_PATTERN,
    _POSIX_PATH_PATTERN,
    _IPV6_PATTERN,
    _IPV4_PATTERN,
    _HOST_PORT_PATTERN,
    _HOSTNAME_PATTERN,
    _HOSTLIKE_NAME_PATTERN,
    _NETWORK_ASSIGNMENT_PATTERN,
    _DSN_ASSIGNMENT_PATTERN,
    _LABELED_IDENTITY_PATTERN,
    _CONSTRAINT_KEY_VALUE_PATTERN,
    _LEADING_LOCK_REMAINDER_PATTERN,
    _SQL_PATTERN,
    _QUOTED_VALUE_PATTERN,
    _DB_OBJECT_PATTERN,
    _ENV_REFERENCE_PATTERN,
    _JWT_PATTERN,
    _KNOWN_SECRET_TOKEN_PATTERN,
    _AWS_ACCESS_KEY_ID_PATTERN,
    _BEARER_TOKEN_PATTERN,
)


_LOCAL_KEY_ESCAPE_PATTERN = re.compile(
    r"\\+(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|"
    r"U([0-9A-Fa-f]{8})|([nrt]))"
)
_LOCAL_SURROGATE_PAIR_ESCAPE_PATTERN = re.compile(
    r"\\+u([dD][89aAbB][0-9A-Fa-f]{2})"
    r"\\+u([dD][c-fC-F][0-9A-Fa-f]{2})"
)
_LOCAL_NEUTRAL_KEY_SEPARATOR = "\u200b"


def _decode_local_credential_key_escapes(value: str) -> str:
    """Decode bounded escape runs in a structured credential key."""

    def replace_surrogate_pair(match):
        high = int(match.group(1), 16)
        low = int(match.group(2), 16)
        codepoint = 0x10000 + ((high - 0xD800) << 10) + (low - 0xDC00)
        return chr(codepoint)

    def replace(match):
        escaped_control = match.group(4)
        if escaped_control:
            return {"n": "\n", "r": "\r", "t": "\t"}[escaped_control.lower()]
        raw_codepoint = match.group(1) or match.group(2) or match.group(3)
        codepoint = int(raw_codepoint, 16)
        if 0xD800 <= codepoint <= 0xDFFF or codepoint > 0x10FFFF:
            return _LOCAL_NEUTRAL_KEY_SEPARATOR
        return chr(codepoint)

    paired = _LOCAL_SURROGATE_PAIR_ESCAPE_PATTERN.sub(
        replace_surrogate_pair, value
    )
    return _LOCAL_KEY_ESCAPE_PATTERN.sub(replace, paired)


def _normalize_text(text):
    try:
        value = "" if text is None else str(text)
    except BaseException:
        return ""

    value = unicodedata.normalize("NFKD", value)
    normalized = []
    for character in value:
        category = unicodedata.category(character)
        if category.startswith(("M", "C")):
            continue
        if character in ("\u2028", "\u2029"):
            normalized.append(" ")
        else:
            normalized.append(character)
    return unicodedata.normalize("NFC", "".join(normalized))


def _redact_remote_high_entropy_tokens(value: str) -> str:
    """Redact opaque token-like values from the remote-only report contract."""

    def replace(match):
        token = match.group(1)
        character_classes = sum((
            any(character.islower() for character in token),
            any(character.isupper() for character in token),
            any(character.isdigit() for character in token),
        ))
        if character_classes >= 2 and len(set(token)) >= 10:
            return _REDACTED
        return token

    return _HIGH_ENTROPY_TOKEN_PATTERN.sub(replace, value)


def sanitize_error_text(text: object, max_length: int = 2000) -> str:
    """Return bounded plain text with sensitive patterns replaced."""

    try:
        limit = int(max_length)
    except (TypeError, ValueError, OverflowError):
        return ""
    if limit <= 0:
        return ""

    sanitized = _normalize_text(text)
    if not sanitized:
        return ""

    for pattern in _REDACTION_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)
    sanitized = _redact_remote_high_entropy_tokens(sanitized)

    sanitized = _MARKDOWN_CONTROLS_PATTERN.sub("", sanitized)
    for pattern in _REDACTION_PATTERNS:
        sanitized = pattern.sub(_REDACTED, sanitized)
    sanitized = _redact_remote_high_entropy_tokens(sanitized)
    sanitized = _WHITESPACE_PATTERN.sub(" ", sanitized).strip()
    return sanitized[:limit]


def sanitize_local_diagnostic(text: object, max_length: int = 20_000) -> str:
    """Redact secrets while preserving useful local DB and path diagnostics."""

    try:
        limit = int(max_length)
        value = "" if text is None else str(text)
    except BaseException:
        return ""
    if limit <= 0 or not value:
        return ""

    sanitized = unicodedata.normalize("NFC", value)
    sanitized = _PRIVATE_KEY_PATTERN.sub(_REDACTED, sanitized)
    sanitized = _KNOWN_SECRET_TOKEN_PATTERN.sub(_REDACTED, sanitized)
    sanitized = _AWS_ACCESS_KEY_ID_PATTERN.sub(_REDACTED, sanitized)
    sanitized = _LOCAL_BEARER_TOKEN_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}", sanitized
    )
    sanitized = _LOCAL_CREDENTIAL_URL_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}@",
        sanitized,
    )
    sanitized = _LOCAL_CLI_CREDENTIAL_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        sanitized,
    )
    sanitized = _LOCAL_USING_QUOTED_CREDENTIAL_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_REDACTED}{match.group('quote')}"
        ),
        sanitized,
    )
    sanitized = _LOCAL_USING_UNTERMINATED_QUOTED_CREDENTIAL_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{_REDACTED}"
        ),
        sanitized,
    )
    sanitized = _LOCAL_USING_CREDENTIAL_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        sanitized,
    )
    sanitized = _LOCAL_QUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_REDACTED}{match.group('quote')}"
        ),
        sanitized,
    )
    sanitized = _LOCAL_UNTERMINATED_QUOTED_ASSIGNMENT_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{_REDACTED}"
        ),
        sanitized,
    )
    sanitized = _LOCAL_ASSIGNMENT_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        sanitized,
    )
    sanitized = _LOCAL_DSN_IDENTITY_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        sanitized,
    )
    sanitized = _LOCAL_QUOTED_CREDENTIAL_SENTENCE_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}"
            f"{_REDACTED}{match.group('quote')}"
        ),
        sanitized,
    )
    sanitized = _LOCAL_UNTERMINATED_QUOTED_CREDENTIAL_SENTENCE_PATTERN.sub(
        lambda match: (
            f"{match.group('prefix')}{match.group('quote')}{_REDACTED}"
        ),
        sanitized,
    )
    sanitized = _LOCAL_CREDENTIAL_SENTENCE_PATTERN.sub(
        lambda match: f"{match.group(1)}{_REDACTED}",
        sanitized,
    )
    sanitized = _JWT_PATTERN.sub(_REDACTED, sanitized)

    escaped = []
    for character in sanitized:
        codepoint = ord(character)
        category = unicodedata.category(character)
        if character == "\n":
            escaped.append(r"\n")
        elif character == "\r":
            escaped.append(r"\r")
        elif character == "\t":
            escaped.append(r"\t")
        elif category in {"Cc", "Cf"}:
            escaped.append(
                f"\\x{codepoint:02x}"
                if codepoint <= 0xFF
                else f"\\u{codepoint:04x}"
            )
        elif category in {"Zl", "Zp"}:
            escaped.append(f"\\u{codepoint:04x}")
        else:
            escaped.append(character)
    return "".join(escaped)[:limit]


_LOCAL_STRUCTURED_SENSITIVE_KEYS = {
    "password",
    "passwd",
    "pwd",
    "token",
    "accesstoken",
    "refreshtoken",
    "apikey",
    "secret",
    "clientsecret",
    "privatekey",
    "authorization",
    "cookie",
    "pgpassword",
    "mysqlpwd",
    "awssecretaccesskey",
    "awsaccesskeyid",
    "awssessiontoken",
    "awssecuritytoken",
    "credentials",
}


def _normalized_local_key(key: object) -> str:
    try:
        value = _decode_local_credential_key_escapes(str(key))
    except BaseException:
        return ""
    return "".join(character.lower() for character in value if character.isalnum())


def sanitize_local_diagnostic_data(value: object, max_depth: int = 12):
    """Return a bounded, cycle-safe copy of local diagnostic structures."""

    try:
        depth_limit = max(0, min(int(max_depth), 64))
    except (TypeError, ValueError, OverflowError):
        depth_limit = 12
    seen = set()
    remaining = [512]

    def unique_key(candidate, result):
        if candidate not in result:
            return candidate
        index = 2
        while True:
            suffix = f" [{index}]"
            alternate = f"{candidate[:256 - len(suffix)]}{suffix}"
            if alternate not in result:
                return alternate
            index += 1

    def sanitize(current, depth):
        if remaining[0] <= 0 or depth > depth_limit:
            return _REDACTED
        remaining[0] -= 1
        if isinstance(current, str):
            return sanitize_local_diagnostic(current)
        if current is None or type(current) in {bool, int}:
            return current
        if type(current) is float:
            return (
                current
                if current == current
                and current not in (float("inf"), float("-inf"))
                else _REDACTED
            )
        if isinstance(current, dict):
            identity = id(current)
            if identity in seen:
                return _REDACTED
            seen.add(identity)
            result = {}
            try:
                for key, item in current.items():
                    if remaining[0] <= 0:
                        break
                    remaining[0] -= 1
                    try:
                        display_key = str(key)
                    except BaseException:
                        display_key = ""
                    safe_key = sanitize_local_diagnostic(
                        display_key, max_length=256
                    )
                    safe_key = unique_key(safe_key, result)
                    if _normalized_local_key(key) in _LOCAL_STRUCTURED_SENSITIVE_KEYS:
                        result[safe_key] = _REDACTED
                    else:
                        result[safe_key] = sanitize(item, depth + 1)
            finally:
                seen.discard(identity)
            return result
        if isinstance(current, (list, tuple)):
            identity = id(current)
            if identity in seen:
                return _REDACTED
            seen.add(identity)
            try:
                return [sanitize(item, depth + 1) for item in current[:256]]
            finally:
                seen.discard(identity)
        return sanitize_local_diagnostic(current)

    return sanitize(value, 0)
