import json
from pathlib import Path

import pytest

from src.core.error_report_sanitizer import sanitize_error_text


CONTRACT_DIR = (
    Path(__file__).parents[1] / "contracts" / "error-reporting" / "v1"
)


def _redaction_cases():
    with (CONTRACT_DIR / "redaction-cases.json").open(
        encoding="utf-8"
    ) as contract_file:
        return json.load(contract_file)["cases"]


@pytest.mark.parametrize(
    "case",
    _redaction_cases(),
    ids=lambda case: case["name"],
)
def test_sanitizer_removes_every_forbidden_shared_fixture_value(case):
    sanitized = sanitize_error_text(case["input"])

    assert sanitized
    assert all(value not in sanitized for value in case["forbidden"])


def test_sanitizer_enforces_length_after_expanding_redactions():
    message = "password=synthetic-secret " * 500

    sanitized = sanitize_error_text(message, max_length=73)

    assert len(sanitized) <= 73
    assert "synthetic-secret" not in sanitized


def test_sanitizer_normalizes_unicode_and_removes_controls_and_markdown():
    message = "＃ heading\u2028[link](https://example.invalid)\x00 `value`"

    sanitized = sanitize_error_text(message)

    assert "\u2028" not in sanitized
    assert "\x00" not in sanitized
    assert not any(control in sanitized for control in "#[]()`")
    assert "example.invalid" not in sanitized


@pytest.mark.parametrize("max_length", [0, -1])
def test_sanitizer_returns_empty_text_for_non_positive_bounds(max_length):
    assert sanitize_error_text("secret", max_length=max_length) == ""


def test_sanitizer_fails_closed_when_text_conversion_raises():
    class UnsafeText:
        def __str__(self):
            raise RuntimeError("must not escape")

    assert sanitize_error_text(UnsafeText()) == ""


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        (
            "Access denied for user SyntheticUser on host synthetic-host port 3306",
            ("SyntheticUser", "synthetic-host", "3306"),
        ),
        (
            "database=private_db schema=private_schema table=private_table "
            "column=secret_column",
            (
                "private_db",
                "private_schema",
                "private_table",
                "secret_column",
                "privatedb",
                "privateschema",
                "privatetable",
                "secretcolumn",
            ),
        ),
        (
            "mysql -h synthetic-host -P 3306 -u SyntheticUser -p synthetic-pass",
            ("synthetic-host", "3306", "SyntheticUser", "synthetic-pass"),
        ),
        (
            "SHOW TABLES FROM private_customer_schema",
            ("private_customer_schema", "privatecustomerschema"),
        ),
        (
            "Password was customer-secret-42; backup requested by Customer Alice Example",
            ("customer-secret-42", "Alice", "Example"),
        ),
        (
            r"C:\Users\John Doe\private\dump.sql failed",
            ("John", "Doe", "private", "dump.sql"),
        ),
        (
            "/home/john doe/private/dump.sql failed",
            ("john", "doe", "private", "dump.sql"),
        ),
        (
            r"\\synthetic-server\private share\dump.sql failed",
            ("synthetic-server", "private", "share", "dump.sql"),
        ),
        (
            "Server=db.internal;Port=5432;Database=customer_prod;"
            "Uid=alice;Pwd=synthetic-pass",
            ("db.internal", "5432", "customer_prod", "alice", "synthetic-pass"),
        ),
        (
            "host=db.internal dbname=customer_prod user=alice password=synthetic-pass",
            ("db.internal", "customer_prod", "alice", "synthetic-pass"),
        ),
        (
            "password=correct horse battery staple",
            ("correct", "horse", "battery", "staple"),
        ),
        (
            "COPY customer_orders FROM STDIN",
            ("customer_orders", "customerorders"),
        ),
        (
            "Key (account_number)=(123456789) already exists",
            ("account_number", "accountnumber", "123456789"),
        ),
        (
            "pass*word=UltraSecret42",
            ("UltraSecret42",),
        ),
        (
            "db*name=CustomerPrivate",
            ("CustomerPrivate",),
        ),
    ],
)
def test_sanitizer_removes_unquoted_identity_object_and_cli_values(
    message, forbidden
):
    sanitized = sanitize_error_text(message)

    assert all(value not in sanitized for value in forbidden)


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        ("client -ualice -pshort-secret", ("alice", "short-secret")),
        ("client -ubuild-user -pbrief-key", ("build-user", "brief-key")),
        (
            "Authorization=Bearer short-token",
            ("short-token", "shorttoken"),
        ),
        ("authorization = basic tiny-token", ("tiny-token", "tinytoken")),
        ("PGPASSWORD=short-secret", ("short-secret", "shortsecret")),
        ("mysql_pwd : another-secret", ("another-secret", "anothersecret")),
        ("connection to db-prod-01 failed", ("db-prod-01", "dbprod01")),
        ("connection to API-stage-12 failed", ("API-stage-12", "APIstage12")),
        ("cwd:/home/alice/private", ("alice", "private")),
        ("workdir: /srv/customer-a/dumps", ("customer-a", "dumps")),
    ],
)
def test_sanitizer_redacts_compact_credentials_hosts_and_labeled_paths(
    message, forbidden
):
    sanitized = sanitize_error_text(message)

    assert all(value not in sanitized for value in forbidden)


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        (
            "VACUUM private_schema.private_table",
            ("private_schema", "private_table", "privateschema", "privatetable"),
        ),
        ("ANALYZE private_orders", ("private_orders", "privateorders")),
        (
            "REINDEX INDEX private_order_idx",
            ("private_order_idx", "privateorderidx"),
        ),
        (
            "PRAGMA private_schema.cache_size = 1000",
            ("private_schema", "cache_size", "privateschema", "cachesize"),
        ),
        (
            "LOCK TABLE private_orders IN ACCESS EXCLUSIVE MODE",
            ("private_orders", "privateorders"),
        ),
        (
            "REPLACE INTO private_orders (secret_column) VALUES ('secret-value')",
            ("private_orders", "secret_column", "secret-value"),
        ),
        (
            "relation private_relation view private_view procedure private_proc "
            "trigger private_trigger sequence private_seq",
            (
                "private_relation",
                "private_view",
                "private_proc",
                "private_trigger",
                "private_seq",
                "privaterelation",
                "privateview",
                "privateproc",
                "privatetrigger",
                "privateseq",
            ),
        ),
    ],
)
def test_sanitizer_redacts_additional_sql_and_database_object_forms(
    message, forbidden
):
    sanitized = sanitize_error_text(message)

    assert all(value not in sanitized for value in forbidden)


def test_sanitizer_preserves_ordinary_prose_containing_broad_sql_words():
    message = (
        "Please select the export option and set the retry value before you commit "
        "changes."
    )

    assert sanitize_error_text(message) == message


def test_sanitizer_fails_closed_when_text_conversion_raises_base_exception():
    class HostileTextAccess(BaseException):
        pass

    class UnsafeText:
        def __str__(self):
            raise HostileTextAccess("must not escape")

    assert sanitize_error_text(UnsafeText()) == ""


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        (
            "request failed: pass\u200bword=UltraSecret42 trailing-context",
            ("UltraSecret42", "trailing-context"),
        ),
        (
            "request failed: PaSs\u2060WoRd: MixedCaseSecret trailing-context",
            ("MixedCaseSecret", "trailing-context"),
        ),
        (
            "request failed: Authori\x00zation: Bearer short-token trailing-context",
            ("short-token", "trailing-context"),
        ),
        (
            "request failed: aUtHoRi\u200bzAtIoN=Basic tiny-token trailing-context",
            ("tiny-token", "trailing-context"),
        ),
        (
            "request failed: PGPASS\u2028WORD=short-secret trailing-context",
            ("short-secret", "trailing-context"),
        ),
        (
            "request failed: pgpass\u2029word: another-secret trailing-context",
            ("another-secret", "trailing-context"),
        ),
        (
            "request failed: pass word=space-secret trailing-context",
            ("space-secret", "trailing-context"),
        ),
        (
            "request failed: pass\u034fword=combining-secret trailing-context",
            ("combining-secret", "trailing-context"),
        ),
        (
            "request failed: pass\u0300word=combining-grave-secret trailing-context",
            ("combining-grave-secret", "trailing-context"),
        ),
        (
            "request failed: pass\u0903word=spacing-mark-secret trailing-context",
            ("spacing-mark-secret", "trailing-context"),
        ),
        (
            "request failed: pass\u20ddword=enclosing-mark-secret trailing-context",
            ("enclosing-mark-secret", "trailing-context"),
        ),
        (
            "request failed: Authori\ufe0fzation=Bearer variation-secret trailing-context",
            ("variation-secret", "trailing-context"),
        ),
        (
            'request failed: Authorization="Bearer alpha\\"suffix" trailing-context',
            ("alpha", "suffix", "trailing-context"),
        ),
        (
            "request failed: PGPASSWORD='alpha\\'suffix' trailing-context",
            ("alpha", "suffix", "trailing-context"),
        ),
        (
            'request failed: --password "alpha\\"suffix" trailing-context',
            ("alpha", "suffix", "trailing-context"),
        ),
    ],
)
def test_sanitizer_redacts_remainder_after_obfuscated_sensitive_key(
    message, forbidden
):
    sanitized = sanitize_error_text(message)

    assert sanitized == "request failed: REDACTED"
    assert all(value not in sanitized for value in forbidden)


@pytest.mark.parametrize(
    "host",
    [
        "db-prod",
        "db-prod-acme",
        "postgres-primary",
        "api-stage",
        "mysql-replica",
        "app-stage",
        "cache-primary",
        "db-prod-acme-01",
        "postgres-primary-2",
        "api-stage2",
        "mysql-replica-03",
        "app-stage-4",
        "cache-primary5",
        "db-\u200bprod-acme-01",
        "DB-Prod",
        "prod-db-primary",
        "prod-db",
    ],
)
def test_sanitizer_redacts_digitless_host_role_names(host):
    sanitized = sanitize_error_text(f"connection to {host} failed")

    assert sanitized == "connection to REDACTED failed"


@pytest.mark.parametrize(
    "ordinary_word",
    [
        "retry-policy",
        "well-known",
        "read-only",
        "client-side",
        "web-based",
        "app-level",
        "cache-friendly",
        "api-based",
        "sql-compatible",
        "redis-backed",
        "stage-aware",
        "primary-key",
        "deployment-ready",
        "app-level-2",
        "cache-friendly-2",
        "node-version-20",
    ],
)
def test_sanitizer_does_not_redact_every_hyphenated_word(ordinary_word):
    message = f"the {ordinary_word} setting is valid"

    assert sanitize_error_text(message) == message


@pytest.mark.parametrize(
    "statement",
    [
        "LOCK private_orders IN ACCESS EXCLUSIVE MODE",
        "LOCK ONLY private_orders",
        "lock private_schema.private_orders in share mode;",
        "LOCK private_a, private_b IN ACCESS EXCLUSIVE MODE;",
        "LOCK ONLY private_a, ONLY private_b IN SHARE MODE NOWAIT;",
        "LOCK private_orders NOWAIT",
        "LOCK TABLE private_orders IN ACCESS EXCLUSIVE MODE",
        "LOCK private_orders;",
        "LOCK private_orders",
        "LOCK private_schema.private_orders;",
        'LOCK "private_schema"."private_orders";',
        "LOCK private_orders; diagnostic context must also be removed",
        "LOCK 고객주문; diagnostic context must also be removed",
        "LO\u200bCK private_orders;",
    ],
)
def test_sanitizer_redacts_postgresql_lock_statements(statement):
    sanitized = sanitize_error_text(statement)

    assert sanitized == "REDACTED"
    assert "private" not in sanitized


def test_sanitizer_preserves_ordinary_lock_prose():
    messages = (
        "Please lock everything before retrying.",
        "Please lock tables before retrying.",
        "The retry path should lock private state locally.",
    )

    assert [sanitize_error_text(message) for message in messages] == list(messages)


@pytest.mark.parametrize(
    "message",
    [
        "the user is unable to connect to the database",
        "the user was unable to connect to the database",
        "a user is waiting to retry the connection",
        "The USER is unable to retry the connection",
        "user is alice",
        "username was build-user",
    ],
)
def test_sanitizer_preserves_ordinary_user_prose(message):
    assert sanitize_error_text(message) == message


@pytest.mark.parametrize(
    ("message", "forbidden"),
    [
        ("user=alice trailing-context", ("alice", "trailing-context")),
        ("USERNAME: build-user trailing-context", ("build-user", "trailing-context")),
        ("Access denied for user SyntheticUser", ("SyntheticUser",)),
    ],
)
def test_sanitizer_redacts_user_only_in_assignment_or_credential_context(
    message, forbidden
):
    sanitized = sanitize_error_text(message)

    assert all(value not in sanitized for value in forbidden)


@pytest.mark.parametrize(
    "statement",
    [
        "LOCK TABLES private_a AS a READ;",
        "LOCK TABLES private_a LOW_PRIORITY WRITE;",
        "LOCK TABLES private_a READ LOCAL, private_b WRITE;",
    ],
)
def test_sanitizer_redacts_mysql_lock_table_statements(statement):
    assert sanitize_error_text(statement) == "REDACTED"


def test_sanitizer_preserves_ordinary_sentence_ending_in_lock_tables():
    message = "The operation should lock tables"

    assert sanitize_error_text(message) == message


@pytest.mark.parametrize(
    "cwd_value",
    [
        r"cwd:\Users\alice\private trailing-context",
        r"CWD=C:\Users\alice\private trailing-context",
        r"cwd=\\server\private-share\dump trailing-context",
        "Cwd:/home/alice/private trailing-context",
        "cwd=relative/private/dump trailing-context",
        r"CWD:..\private\dump trailing-context",
        "c\u200bwd=relative/private/dump trailing-context",
    ],
)
def test_sanitizer_redacts_remainder_after_any_cwd_assignment(cwd_value):
    sanitized = sanitize_error_text(f"request failed: {cwd_value}")

    assert sanitized == "request failed: REDACTED"


def test_sanitizer_preserves_cwd_prose_without_an_assignment_delimiter():
    message = "The cwd value is unavailable"

    assert sanitize_error_text(message) == message


def test_sanitizer_preserves_safe_non_ascii_text_when_normalizing():
    message = "연결 실패: 東京 리전에 다시 시도합니다"

    assert sanitize_error_text(message) == message


@pytest.mark.parametrize(
    ("diagnostic", "forbidden"),
    [
        ("table customer_orders failed: password=hunter2", "hunter2"),
        ("table customer_orders failed: token=short-secret", "short-secret"),
        (
            "postgresql://alice:uri-secret@db.internal/customer failed",
            "uri-secret",
        ),
        (
            "dsn=mysql://bob:dsn-secret@localhost/customer failed",
            "dsn-secret",
        ),
        (
            "-----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY----- table customer_orders failed",
            "private-material",
        ),
        (
            "JWT eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c failed",
            "eyJhbGciOiJIUzI1NiJ9",
        ),
        (
            "table customer_orders failed secret="
            "0123456789abcdef0123456789abcdef",
            "0123456789abcdef0123456789abcdef",
        ),
        ("client --password cli-secret customer_orders", "cli-secret"),
        ("database password is sentence-secret", "sentence-secret"),
    ],
)
def test_local_diagnostic_sanitizer_redacts_secrets_but_keeps_context(
    diagnostic, forbidden
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert forbidden not in sanitized
    assert "REDACTED" in sanitized


def test_local_diagnostic_sanitizer_preserves_table_error_and_path_context():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = (
        r"table customer_orders failed at C:\backups\customer\dump.sql: "
        "duplicate key on order_id"
    )

    assert sanitize_local_diagnostic(diagnostic) == diagnostic


def test_local_diagnostic_sanitizer_escapes_controls_and_bidi():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = "table customer_orders\n[FORGED]\x1b[31m\u202ereversed"

    assert sanitize_local_diagnostic(diagnostic) == (
        r"table customer_orders\n[FORGED]\x1b[31m\u202ereversed"
    )


@pytest.mark.parametrize(
    ("diagnostic", "forbidden"),
    [
        (
            "mysql -ualice -pcompact-secret table customer_orders failed",
            ("alice", "compact-secret"),
        ),
        (
            "table customer_orders failed; "
            "credentials={user=alice,password=bundle-secret}",
            ("alice", "bundle-secret"),
        ),
        (
            "Server=db.internal;Uid=alice;Pwd=dsn-secret; "
            "table customer_orders failed",
            ("alice", "dsn-secret"),
        ),
        (
            "table customer_orders failed; password=correct horse battery staple",
            ("correct", "horse", "battery", "staple"),
        ),
        (
            "table customer_orders failed; access token was multi word secret",
            ("multi", "word", "secret"),
        ),
    ],
)
def test_local_diagnostic_sanitizer_redacts_credential_bundles_and_cli_forms(
    diagnostic, forbidden
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert all(secret not in sanitized for secret in forbidden)
    assert "customer_orders" in sanitized


@pytest.mark.parametrize(
    "diagnostic",
    [
        (
            "before -----BEGIN PRIVATE KEY-----\nprivate-material\n"
            "-----END PRIVATE KEY----- after"
        ),
        "before -----BEGIN OPENSSH PRIVATE KEY-----\nunterminated-material",
    ],
)
def test_local_diagnostic_sanitizer_redacts_complete_and_unterminated_private_keys(
    diagnostic,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert "private-material" not in sanitized
    assert "unterminated-material" not in sanitized
    assert "BEGIN" not in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize(
    "diagnostic",
    [
        'password="hunter2"',
        "passwd: hunter2",
        "pwd=hunter2",
        "token is hunter2",
        "access_token=short-token",
        "refresh-token was short-token",
        '"api_key": "short-token"',
        "secret: short-token",
        "connection failed using password hunter2 for customer_orders",
    ],
)
def test_local_diagnostic_sanitizer_redacts_required_sensitive_prose(diagnostic):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert "hunter2" not in sanitized
    assert "short-token" not in sanitized
    assert "REDACTED" in sanitized


def test_local_diagnostic_sanitizer_preserves_normal_session_completion_text():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = "session is complete for table customer_orders"

    assert sanitize_local_diagnostic(diagnostic) == diagnostic


def test_structured_local_sanitizer_redacts_unknown_json_event_recursively():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic_data

    event = {
        "password": "hunter2",
        "nested": {"token": "short-token"},
        "message": "session is complete for table customer_orders",
    }

    sanitized = sanitize_local_diagnostic_data(event)

    assert sanitized == {
        "password": "REDACTED",
        "nested": {"token": "REDACTED"},
        "message": "session is complete for table customer_orders",
    }


def test_local_diagnostic_sanitizer_is_idempotent_after_control_escaping():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = "table customer_orders failed password=hunter2\n[FORGED]\u202ereversed"
    first_pass = sanitize_local_diagnostic(diagnostic)

    assert sanitize_local_diagnostic(first_pass) == first_pass


def test_local_diagnostic_sanitizer_redacts_multiword_using_password_phrase():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = (
        "connection failed using password correct horse battery staple; "
        "table customer_orders failed"
    )

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert all(word not in sanitized for word in ("correct", "horse", "battery", "staple"))
    assert "table customer_orders failed" in sanitized


def test_local_diagnostic_sanitizer_redacts_quoted_using_password_delimiters():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = 'using password "correct;horse"; table customer_orders failed'

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert "correct" not in sanitized
    assert "horse" not in sanitized
    assert "table customer_orders failed" in sanitized


@pytest.mark.parametrize(
    "diagnostic",
    [
        'using password "correct;horse',
        'using password "correct\\"still;horse',
        'password="correct;horse',
        'password="correct\\"still;horse',
    ],
)
def test_local_diagnostic_sanitizer_redacts_unterminated_quoted_credentials(
    diagnostic,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert "correct" not in sanitized
    assert "horse" not in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize(
    "diagnostic",
    [
        'password="hunter2" table customer_orders failed',
        "user=alice\nTable customer_orders failed",
    ],
)
def test_local_diagnostic_second_pass_preserves_table_and_error_context(diagnostic):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    first_pass = sanitize_local_diagnostic(diagnostic)

    assert "customer_orders failed" in first_pass
    assert sanitize_local_diagnostic(first_pass) == first_pass


@pytest.mark.parametrize(
    ("diagnostic", "expected"),
    [
        (
            r'{\"password\":\"hunter2\",\"detail\":\"for table customer_orders\"}',
            r'{\"password\":\"REDACTED\",\"detail\":\"for table customer_orders\"}',
        ),
        (
            r'{\"message\":\"{\\\"password\\\":\\\"hunter2\\\",'
            r'\\\"detail\\\":\\\"for table customer_orders\\\"}\"}',
            r'{\"message\":\"{\\\"password\\\":\\\"REDACTED\\\",'
            r'\\\"detail\\\":\\\"for table customer_orders\\\"}\"}',
        ),
        (
            'connection failed using password hunter2 for table customer_orders',
            'connection failed using password REDACTED for table customer_orders',
        ),
        (
            'connection failed using the password hunter2 for table customer_orders',
            'connection failed using the password REDACTED for table customer_orders',
        ),
        (
            'password=hunter2 for table customer_orders',
            'password=REDACTED for table customer_orders',
        ),
        (
            'password was hunter2 for table customer_orders',
            'password was REDACTED for table customer_orders',
        ),
        (
            'the password was "hunter2" for table customer_orders',
            'the password was "REDACTED" for table customer_orders',
        ),
        (
            'the password is hunter2 for table customer_orders',
            'the password is REDACTED for table customer_orders',
        ),
        (
            'password: "hunter2"; for table customer_orders',
            'password: "REDACTED"; for table customer_orders',
        ),
    ],
)
def test_local_diagnostic_redaction_probes_preserve_context_and_are_idempotent(
    diagnostic, expected
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert sanitized == expected
    assert sanitize_local_diagnostic(sanitized) == sanitized


def test_local_diagnostic_does_not_redact_long_identifier_by_length_alone():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = "failed for table customer_orders_archive_partition_2024"

    assert sanitize_local_diagnostic(diagnostic) == diagnostic


@pytest.mark.parametrize(
    "diagnostic",
    [
        r'passw\u006frd=\"hunter2\" table customer_orders failed',
        r'{\"access_\u0074oken\":\"short-secret\",'
        r'\"table\":\"customer_orders\"}',
        r'{\"message\":\"{\\\"passw\\u006frd\\\":\\\"hunter2\\\",'
        r'\\\"table\\\":\\\"customer_orders\\\"}\"}',
    ],
)
def test_local_diagnostic_recognizes_unicode_escaped_keys_and_escaped_quotes(
    diagnostic,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert "hunter2" not in sanitized
    assert "short-secret" not in sanitized
    assert "customer_orders" in sanitized
    assert "REDACTED" in sanitized


def test_local_structured_diagnostic_recognizes_unicode_escaped_credential_key():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic_data

    sanitized = sanitize_local_diagnostic_data(
        {r"passw\u006frd": "hunter2", "table": "customer_orders"}
    )

    assert sanitized[r"passw\u006frd"] == "REDACTED"
    assert sanitized["table"] == "customer_orders"


def test_structured_local_diagnostic_preserves_escaped_key_spelling_without_collision():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic_data

    sanitized = sanitize_local_diagnostic_data(
        {r"literal\x41": "first", "literalA": "second"}
    )

    assert sanitized == {r"literal\x41": "first", "literalA": "second"}


def test_structured_local_diagnostic_keeps_colliding_display_keys_unique():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic_data

    sanitized = sanitize_local_diagnostic_data(
        {r"key\n": 1, r"key\n [2]": 3, "key\n": 2}
    )

    assert sanitized == {
        r"key\n": 1,
        r"key\n [2]": 3,
        r"key\n [3]": 2,
    }


def test_structured_truncated_sensitive_key_collisions_consume_the_item_budget():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic_data

    class PasswordKey:
        def __init__(self, identity):
            self.identity = identity

        def __hash__(self):
            return hash(self.identity)

        def __eq__(self, other):
            return self is other

        def __str__(self):
            return f"{'!' * 256}password{'!' * self.identity}"

    payload = {PasswordKey(index): "secret" for index in range(600)}

    sanitized = sanitize_local_diagnostic_data(payload)

    assert len(sanitized) == 511
    assert set(sanitized.values()) == {"REDACTED"}


@pytest.mark.parametrize(
    "separator_escape",
    [
        r"\u0000",
        r"\u001f",
        r"\u0027",
        r"\u002e",
        r"\u002f",
        r"\u003b",
        r"\u200b",
        r"\ud800",
        r"\ud83d\udca5",
        r"\udfff",
    ],
)
def test_local_diagnostic_recognizes_escaped_non_alphanumeric_key_separator(
    separator_escape,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(
        f"pass{separator_escape}word=hunter2 for table customer_orders"
    )

    assert "hunter2" not in sanitized
    assert "customer_orders" in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize(
    "separator_escape",
    [
        r"\u0000",
        r"\u001f",
        r"\u0027",
        r"\u002e",
        r"\u002f",
        r"\u003b",
        r"\u200b",
        r"\ud800",
        r"\ud83d\udca5",
        r"\udfff",
    ],
)
def test_structured_local_diagnostic_recognizes_escaped_key_separator(
    separator_escape,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic_data

    sanitized = sanitize_local_diagnostic_data(
        {
            f"pass{separator_escape}word": "hunter2",
            "table": "customer_orders",
        }
    )

    assert "hunter2" not in sanitized.values()
    assert "REDACTED" in sanitized.values()
    assert sanitized["table"] == "customer_orders"


@pytest.mark.parametrize(
    "diagnostic",
    [
        "password=a'hunter2 for table customer_orders",
        "password=REDACTED'hunter2 for table customer_orders",
        r"password=a\u003bhunter2 for table customer_orders",
    ],
)
def test_local_diagnostic_redacts_unquoted_credential_with_embedded_quote(
    diagnostic,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(diagnostic)

    assert "hunter2" not in sanitized
    assert "customer_orders" in sanitized
    assert sanitized == "password=REDACTED for table customer_orders"


@pytest.mark.parametrize(
    "token",
    [
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "github_pat_abcdefghijklmnopqrstuvwxyz123456",
        "glpat-abcdefghijklmnopqrstuvwxyz123456",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
        "xox" + "b-123456789012-abcdefghijklmnopqrstuvwxyz",
        "xox" + "a-123456789012-abcdefghijklmnopqrstuvwxyz",
        "xox" + "p-123456789012-abcdefghijklmnopqrstuvwxyz",
        "xox" + "r-123456789012-abcdefghijklmnopqrstuvwxyz",
        "xox" + "s-123456789012-abcdefghijklmnopqrstuvwxyz",
    ],
)
def test_local_diagnostic_redacts_bounded_known_secret_token_prefixes(token):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(
        f"request failed with {token} for table customer_orders"
    )

    assert token not in sanitized
    assert "customer_orders" in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize(
    "token",
    [
        "ghp_" + "a" * 300,
        "github_pat_" + "a" * 300,
        "glpat-" + "a" * 300,
        "sk-" + "a" * 300,
        "xox" + "b-" + "a" * 300,
        "xoxc-" + "a" * 300,
    ],
)
def test_local_diagnostic_redacts_long_known_secret_token_families(token):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(
        f"request failed with {token} for table customer_orders"
    )

    assert token not in sanitized
    assert "customer_orders" in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize(
    "key",
    [
        "AWS_SECRET_ACCESS_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SESSION_TOKEN",
        "AWS_SECURITY_TOKEN",
    ],
)
def test_local_diagnostic_redacts_scalar_and_structured_aws_credentials(key):
    from src.core.error_report_sanitizer import (
        sanitize_local_diagnostic,
        sanitize_local_diagnostic_data,
    )

    secret = "aws-credential-value"
    scalar = sanitize_local_diagnostic(
        f"{key}={secret} for table customer_orders"
    )
    structured = sanitize_local_diagnostic_data(
        {"outer": [{key: secret}], "table": "customer_orders"}
    )

    assert secret not in scalar
    assert secret not in str(structured)
    assert "customer_orders" in scalar
    assert structured["table"] == "customer_orders"


@pytest.mark.parametrize(
    "escaped_key",
    [
        r"AW\x53_SECRET_ACCESS_KEY",
        r"AWS\nSECRET_ACCESS_KEY",
        r"AWS\rACCESS_KEY_ID",
        r"AWS\tSESSION_TOKEN",
        r"AWS\u005fSECURITY_TOKEN",
    ],
)
def test_local_diagnostic_recognizes_bounded_escapes_in_credential_keys(
    escaped_key,
):
    from src.core.error_report_sanitizer import (
        sanitize_local_diagnostic,
        sanitize_local_diagnostic_data,
    )

    secret = "escaped-aws-credential"
    scalar = sanitize_local_diagnostic(
        f"{escaped_key}={secret} for table customer_orders"
    )
    structured = sanitize_local_diagnostic_data(
        {
            "outer": [{escaped_key: secret}],
            "detail": r"value keeps \x41 and \n escapes",
        }
    )

    assert secret not in scalar
    assert secret not in str(structured)
    assert "customer_orders" in scalar
    assert structured["detail"] == r"value keeps \x41 and \n escapes"


@pytest.mark.parametrize(
    "credential",
    [
        "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq",
        "j4N8vQ2xL7cR5mK9pT3wF6yH1sD0bGzA+eU=",
    ],
)
def test_remote_sanitizer_redacts_while_local_sanitizer_preserves_unprefixed_opaque_values(
    credential,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = f"request id {credential} failed"

    assert sanitize_local_diagnostic(diagnostic) == diagnostic
    assert credential not in sanitize_error_text(diagnostic)


@pytest.mark.parametrize(
    "identifier",
    [
        "customer_orders_archive_partition_20240714",
        "migration_session_identifier_20240714_complete",
        "CustomerOrdersArchivePartitionIdentifier20240714",
        "550e8400e29b41d4a716446655440000",
    ],
)
def test_local_diagnostic_preserves_long_noncredential_identifiers(identifier):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = f"database diagnostic identifier {identifier}"

    assert sanitize_local_diagnostic(diagnostic) == diagnostic


@pytest.mark.parametrize(
    "identifier",
    [
        "req_Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq",
        "session-j4N8vQ2xL7cR5mK9pT3wF6yH1sD0bGzA",
        "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq_customer_orders",
        "0123456789abcdef" * 4,
    ],
)
def test_local_diagnostic_preserves_opaque_request_table_session_and_hash_ids(
    identifier,
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = f"diagnostic identifier {identifier}"

    assert sanitize_local_diagnostic(diagnostic) == diagnostic


@pytest.mark.parametrize(
    "access_key_id",
    [
        "AKIA1234567890ABCDEF",
        "ASIAABCDEFGHIJKLMNOP",
    ],
)
def test_sanitizers_redact_bare_aws_access_key_ids(access_key_id):
    from src.core.error_report_sanitizer import (
        sanitize_local_diagnostic,
        sanitize_local_diagnostic_data,
    )

    diagnostic = f"request failed with {access_key_id}"
    structured = sanitize_local_diagnostic_data(
        {"request": access_key_id, "items": [access_key_id]}
    )

    assert access_key_id not in sanitize_error_text(diagnostic)
    assert access_key_id not in sanitize_local_diagnostic(diagnostic)
    assert access_key_id not in str(structured)


def test_aws_40_character_secret_requires_credential_context():
    from src.core.error_report_sanitizer import (
        sanitize_local_diagnostic,
        sanitize_local_diagnostic_data,
    )

    secret = "AbCdEfGhIjKlMnOpQrStUvWxYz0123456789ABCD"
    assert len(secret) == 40
    opaque = f"request id {secret} failed"
    contextual = f"AWS_SECRET_ACCESS_KEY={secret} for table customer_orders"
    structured = sanitize_local_diagnostic_data({
        "AWS_SESSION_TOKEN": secret,
        "items": [f"AWS_SECRET_ACCESS_KEY={secret}"],
    })

    assert sanitize_local_diagnostic(opaque) == opaque
    assert secret not in sanitize_error_text(opaque)
    assert secret not in sanitize_local_diagnostic(contextual)
    assert secret not in sanitize_error_text(contextual)
    assert secret not in str(structured)


def test_remote_sanitizer_recognizes_literal_escaped_aws_key_only():
    message = (
        r"detail=literal\u005fvalue "
        r"AWS\u005fACCESS\u005fKEY\u005fID=short-credential trailing-context"
    )

    sanitized = sanitize_error_text(message)

    assert r"literal\u005fvalue" in sanitized
    assert "short-credential" not in sanitized
    assert "trailing-context" not in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize("delimiter", (r"\u003d", r"\\u003A", r"\x3D", r"\\x3a"))
def test_sanitizers_recognize_bounded_escaped_assignment_delimiters(delimiter):
    from src.core.error_report_sanitizer import (
        sanitize_local_diagnostic,
        sanitize_local_diagnostic_data,
    )

    secret = "escaped-delimiter-secret"
    message = f"password{delimiter}{secret} for table customer_orders"
    structured = sanitize_local_diagnostic_data({"detail": message})

    assert secret not in sanitize_error_text(message)
    assert secret not in sanitize_local_diagnostic(message)
    assert secret not in structured["detail"]
    assert r"literal\x41" in sanitize_local_diagnostic(r"literal\x41")


def test_remote_sanitizer_redacts_context_free_high_entropy_value():
    token = "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq"

    sanitized = sanitize_error_text(f"request failed {token}")

    assert token not in sanitized
    assert "REDACTED" in sanitized


@pytest.mark.parametrize(
    "token",
    [
        "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq" * 8,
        "0123456789abcdef" * 8,
        "a1b2c3d4e5f6g7h8i9j0" * 2,
    ],
)
def test_remote_sanitizer_redacts_unbounded_diverse_two_class_tokens(token):
    sanitized = sanitize_error_text(token, max_length=2000)

    assert token not in sanitized
    assert "REDACTED" in sanitized


def test_remote_sanitizer_may_preserve_low_diversity_repeated_token():
    token = "a" * 2000

    assert sanitize_error_text(token, max_length=2000) == token


def test_local_sanitizer_preserves_context_free_high_entropy_value():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    token = "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq"
    diagnostic = f"request id {token} failed"

    assert sanitize_local_diagnostic(diagnostic) == diagnostic


def test_bearer_context_still_redacts_opaque_token_without_entropy_guessing():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    token = "Q7mP2vK9xR4tN8cL5sW1yB6dF3hJ0aZq"
    diagnostic = f"request failed with Bearer {token}"

    assert token not in sanitize_error_text(diagnostic)
    assert token not in sanitize_local_diagnostic(diagnostic)


@pytest.mark.parametrize(
    ("dsn", "expected"),
    [
        (
            "postgresql://alice:p@ss@db.internal/customer_orders",
            "postgresql://REDACTED@db.internal/customer_orders",
        ),
        (
            "mysql://service:pa@@word@db.internal/customer_orders?ssl=true",
            "mysql://REDACTED@db.internal/customer_orders?ssl=true",
        ),
    ],
)
def test_local_diagnostic_redacts_complete_dsn_userinfo_through_last_at(
    dsn, expected
):
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    sanitized = sanitize_local_diagnostic(f"connection failed at {dsn}")

    assert sanitized == f"connection failed at {expected}"


def test_local_short_token_assignment_preserves_following_table_boundary():
    from src.core.error_report_sanitizer import sanitize_local_diagnostic

    diagnostic = "token=short-secret table customer_orders failed"

    assert sanitize_local_diagnostic(diagnostic) == (
        "token=REDACTED table customer_orders failed"
    )
