"""
migration_analyzer.py 단위 테스트

MigrationAnalyzer, DumpFileAnalyzer, TwoPassAnalyzer 검증.
DB 없이 FakeMySQLConnector로 동작을 검증합니다.
"""
import pytest
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, call

from src.core.migration_constants import IssueType, CompatibilityIssue
from src.core.migration_analyzer import (
    MigrationAnalyzer,
    DumpFileAnalyzer,
    AnalysisResult,
    DumpAnalysisResult,
    OrphanRecord,
    CleanupAction,
    ActionType,
    ForeignKeyInfo,
    TwoPassAnalyzer,
)
from tests.conftest import FakeMySQLConnector


# ============================================================
# MigrationAnalyzer 기본 테스트
# ============================================================
class TestMigrationAnalyzerInit:
    """MigrationAnalyzer 초기화 테스트"""

    def test_init(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        assert analyzer.connector is fake_connector

    def test_progress_callback(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        messages = []
        analyzer.set_progress_callback(lambda m: messages.append(m))
        analyzer._log("test message")
        assert "test message" in messages

    def test_no_callback(self, fake_connector):
        """콜백 없으면 _log가 예외 없이 동작"""
        analyzer = MigrationAnalyzer(fake_connector)
        analyzer._log("should not error")


class TestCheckCharsetIssues:
    """check_charset_issues 테스트"""

    def test_finds_utf8mb3_table(self, fake_connector):
        fake_connector.query_results = {
            'TABLE_COLLATION': [
                {'TABLE_NAME': 'users', 'TABLE_COLLATION': 'utf8_general_ci'}
            ],
            'CHARACTER_SET_NAME': [],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_charset_issues("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.CHARSET_ISSUE

    def test_finds_utf8mb3_column(self, fake_connector):
        fake_connector.query_results = {
            'TABLE_COLLATION': [],
            'CHARACTER_SET_NAME': [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'name', 'CHARACTER_SET_NAME': 'utf8', 'COLLATION_NAME': 'utf8_general_ci'}
            ],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_charset_issues("test_db")
        assert len(issues) >= 1
        assert "utf8mb3" in issues[0].description

    def test_no_issues_when_clean(self, fake_connector):
        fake_connector.query_results = {
            'TABLE_COLLATION': [],
            'CHARACTER_SET_NAME': [],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_charset_issues("test_db")
        assert len(issues) == 0


class TestCheckReservedKeywords:
    """check_reserved_keywords 테스트"""

    def test_finds_table_keyword(self, fake_connector):
        fake_connector._tables = {'test_db': ['rank', 'users']}
        fake_connector.query_results = {
            'COLUMN_NAME': []
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_reserved_keywords("test_db")
        keyword_issues = [i for i in issues if i.issue_type == IssueType.RESERVED_KEYWORD]
        assert len(keyword_issues) >= 1
        assert any("RANK" in i.description.upper() for i in keyword_issues)

    def test_finds_column_keyword(self, fake_connector):
        fake_connector._tables = {'test_db': ['users']}
        fake_connector.query_results = {
            'COLUMN_NAME': [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'rank'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_reserved_keywords("test_db")
        keyword_issues = [i for i in issues if i.issue_type == IssueType.RESERVED_KEYWORD]
        assert len(keyword_issues) >= 1

    def test_no_keyword_conflict(self, fake_connector):
        fake_connector._tables = {'test_db': ['users']}
        fake_connector.query_results = {
            'COLUMN_NAME': [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'name'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_reserved_keywords("test_db")
        assert len(issues) == 0


class TestCheckDeprecatedInRoutines:
    """check_deprecated_in_routines 테스트"""

    def test_finds_deprecated_function(self, fake_connector):
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'get_hash',
                    'ROUTINE_TYPE': 'FUNCTION',
                    'ROUTINE_DEFINITION': "SELECT PASSWORD('test')"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.DEPRECATED_FUNCTION

    def test_no_deprecated_functions(self, fake_connector):
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'safe_func',
                    'ROUTINE_TYPE': 'FUNCTION',
                    'ROUTINE_DEFINITION': "SELECT COUNT(*) FROM users"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        assert len(issues) == 0

    def test_password_column_reference_is_not_a_false_positive(self, fake_connector):
        """컬럼/식별자 'password'는 PASSWORD() 함수 호출이 아니므로 오탐하면 안 된다"""
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'get_user',
                    'ROUTINE_TYPE': 'FUNCTION',
                    'ROUTINE_DEFINITION': "SELECT password FROM users WHERE id = 1"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        assert issues == []

    def test_aes_encrypt_is_not_flagged_as_encrypt(self, fake_connector):
        """AES_ENCRYPT()는 ENCRYPT()와 다른 함수이므로 오탐하면 안 된다"""
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'enc_func',
                    'ROUTINE_TYPE': 'FUNCTION',
                    'ROUTINE_DEFINITION': "SELECT AES_ENCRYPT(secret, 'k')"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        assert issues == []

    def test_actual_password_call_still_reported(self, fake_connector):
        """실제 PASSWORD(...) 호출은 여전히 탐지되어야 한다"""
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'get_hash',
                    'ROUTINE_TYPE': 'FUNCTION',
                    'ROUTINE_DEFINITION': "SELECT PASSWORD('test')"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        assert len(issues) == 1

    def test_duplicate_calls_of_same_function_reported_once(self, fake_connector):
        """동일 함수가 여러 번 호출돼도 함수당 이슈는 1개만 보고한다"""
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'paginated_query',
                    'ROUTINE_TYPE': 'PROCEDURE',
                    'ROUTINE_DEFINITION': "SELECT FOUND_ROWS(); SELECT FOUND_ROWS();"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        found_rows_issues = [i for i in issues if "FOUND_ROWS" in i.description]
        assert len(found_rows_issues) == 1

    def test_sql_calc_found_rows_without_parens_not_flagged_by_call_boundary(self, fake_connector):
        """SQL_CALC_FOUND_ROWS는 SELECT 수정자로 괄호 없이 쓰이므로 함수-호출
        경계 검사(뒤에 '(' 필요)에서는 잡히지 않는다 - 알려진 트레이드오프."""
        fake_connector.query_results = {
            'ROUTINE_DEFINITION': [
                {
                    'ROUTINE_NAME': 'legacy_paginate',
                    'ROUTINE_TYPE': 'PROCEDURE',
                    'ROUTINE_DEFINITION': "SELECT SQL_CALC_FOUND_ROWS * FROM users LIMIT 10"
                }
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_in_routines("test_db")
        assert issues == []


class TestCheckSqlModes:
    """check_sql_modes 테스트"""

    def test_finds_deprecated_mode(self, fake_connector):
        fake_connector.query_results = {
            '@@sql_mode': [{'sql_mode': 'ONLY_FULL_GROUP_BY,NO_AUTO_CREATE_USER'}]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_sql_modes()
        mode_issues = [i for i in issues if i.issue_type == IssueType.SQL_MODE_ISSUE]
        assert len(mode_issues) >= 1
        assert "NO_AUTO_CREATE_USER" in mode_issues[0].description

    def test_no_deprecated_modes(self, fake_connector):
        fake_connector.query_results = {
            '@@sql_mode': [{'sql_mode': 'ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES'}]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_sql_modes()
        assert len(issues) == 0

    def test_empty_sql_mode(self, fake_connector):
        fake_connector.query_results = {
            '@@sql_mode': [{'sql_mode': ''}]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_sql_modes()
        assert len(issues) == 0


class TestCheckAuthPlugins:
    """check_auth_plugins 테스트"""

    def test_finds_native_password(self, fake_connector):
        fake_connector.query_results = {
            'mysql.user': [
                {'User': 'admin', 'Host': 'localhost', 'plugin': 'mysql_native_password'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_auth_plugins()
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.AUTH_PLUGIN_ISSUE
        assert issues[0].severity == "error"

    def test_finds_sha256(self, fake_connector):
        fake_connector.query_results = {
            'mysql.user': [
                {'User': 'user1', 'Host': '%', 'plugin': 'sha256_password'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_auth_plugins()
        assert len(issues) >= 1
        assert issues[0].severity == "warning"

    def test_query_failure_no_crash(self, fake_connector):
        """mysql.user 접근 권한 없을 때 예외 처리"""
        fake_connector.fail_on = {'mysql.user': PermissionError("Access denied")}
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_auth_plugins()
        assert len(issues) == 0  # 실패해도 빈 리스트


class TestCheckZerofillColumns:
    def test_finds_zerofill(self, fake_connector):
        fake_connector.query_results = {
            'ZEROFILL': [
                {'TABLE_NAME': 'orders', 'COLUMN_NAME': 'order_num', 'COLUMN_TYPE': 'int(8) unsigned zerofill'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_zerofill_columns("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.ZEROFILL_USAGE


class TestCheckFloatPrecision:
    def test_finds_float_md(self, fake_connector):
        fake_connector.query_results = {
            'float': [
                {'TABLE_NAME': 't', 'COLUMN_NAME': 'val', 'COLUMN_TYPE': 'float(10,2)', 'DATA_TYPE': 'float'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_float_precision("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.FLOAT_PRECISION


class TestCheckDeprecatedEngines:
    def test_finds_myisam(self, fake_connector):
        fake_connector.query_results = {
            'ENGINE': [
                {'TABLE_NAME': 'logs', 'ENGINE': 'MyISAM'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_deprecated_engines("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.DEPRECATED_ENGINE


class TestCheckYear2Type:
    def test_finds_year2(self, fake_connector):
        fake_connector.query_results = {
            "year(2)": [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'birth', 'COLUMN_TYPE': 'year(2)'}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_year2_type("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.YEAR2_TYPE


class TestCheckEnumEmptyValue:
    def test_finds_empty_enum(self, fake_connector):
        fake_connector.query_results = {
            "enum": [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'status', "COLUMN_TYPE": "enum('active','','inactive')"}
            ]
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_enum_empty_value("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.ENUM_EMPTY_VALUE


class TestCheckTimestampRange:
    def test_timestamp_column_reported_as_advisory(self, fake_connector):
        """TIMESTAMP는 '2038-01-19 03:14:07'를 초과하는 값을 애초에 저장할 수
        없으므로, 실데이터를 조회해 초과 여부를 판정하는 것은 항상 0건만
        나오는 무의미한 검사다. 컬럼 존재 자체를 advisory로 보고해야 한다."""
        fake_connector.query_results = {
            "timestamp": [
                {'TABLE_NAME': 'events', 'COLUMN_NAME': 'event_time'}
            ],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_timestamp_range("test_db")
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.TIMESTAMP_RANGE
        assert issues[0].severity == "warning"
        # 항상 거짓인 라이브 데이터 조회를 더 이상 실행하지 않아야 한다
        assert not any(
            "2038-01-19 03:14:07" in (query or "")
            for query, _ in fake_connector.executed_queries
        )

    def test_no_timestamp_columns_no_issues(self, fake_connector):
        fake_connector.query_results = {"timestamp": []}
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_timestamp_range("test_db")
        assert issues == []


class TestCheckIntDisplayWidth:
    """check_int_display_width 직접 호출 + 파이프라인 연결 테스트

    이 메서드는 정의만 되어 있고 analyze_schema/_analyze_schema_impl
    파이프라인 어디에서도 호출되지 않던 죽은 코드였다.
    """

    INT_DISPLAY_WIDTH_QUERY_KEY = "COLUMN_TYPE REGEXP '^(tinyint|smallint|mediumint|int|bigint)"

    def test_finds_int_display_width(self, fake_connector):
        fake_connector.query_results = {
            self.INT_DISPLAY_WIDTH_QUERY_KEY: [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'age', 'COLUMN_TYPE': 'int(11)'}
            ],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_int_display_width("test_db")
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.INT_DISPLAY_WIDTH
        assert issues[0].severity == "info"

    def _pipeline_kwargs(self, enabled: bool) -> dict:
        return dict(
            check_orphans=False, check_charset=False, check_keywords=False,
            check_routines=False, check_sql_mode=False, check_auth_plugins=False,
            check_zerofill=False, check_float_precision=False, check_fk_name_length=False,
            check_invalid_dates=False, check_year2=False, check_deprecated_engines=False,
            check_enum_empty=False, check_timestamp_range=False,
            check_int_display_width=enabled,
        )

    def test_wired_into_pipeline_when_enabled(self, fake_connector):
        fake_connector._tables = {"test_db": []}
        fake_connector.query_results = {
            self.INT_DISPLAY_WIDTH_QUERY_KEY: [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'age', 'COLUMN_TYPE': 'int(11)'}
            ],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        result = analyzer._analyze_schema_impl("test_db", **self._pipeline_kwargs(True))
        int_issues = [i for i in result.compatibility_issues if i.issue_type == IssueType.INT_DISPLAY_WIDTH]
        assert len(int_issues) == 1

    def test_not_run_in_pipeline_when_disabled(self, fake_connector):
        fake_connector._tables = {"test_db": []}
        fake_connector.query_results = {
            self.INT_DISPLAY_WIDTH_QUERY_KEY: [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'age', 'COLUMN_TYPE': 'int(11)'}
            ],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        result = analyzer._analyze_schema_impl("test_db", **self._pipeline_kwargs(False))
        int_issues = [i for i in result.compatibility_issues if i.issue_type == IssueType.INT_DISPLAY_WIDTH]
        assert int_issues == []


class TestCheckInvalidDateValues:
    def test_finds_zero_dates(self, fake_connector):
        fake_connector.query_results = {
            "DATA_TYPE": [
                {'TABLE_NAME': 'orders', 'COLUMN_NAME': 'created', 'DATA_TYPE': 'date', 'COLUMN_DEFAULT': None}
            ],
            "'0000-00-00'": [{'cnt': 10}],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_invalid_date_values("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.INVALID_DATE

    def test_skips_on_query_error(self, fake_connector):
        fake_connector.query_results = {
            "DATA_TYPE": [
                {'TABLE_NAME': 'orders', 'COLUMN_NAME': 'created', 'DATA_TYPE': 'date', 'COLUMN_DEFAULT': None}
            ],
        }
        fake_connector.fail_on = {"'0000-00-00'": Exception("denied")}
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_invalid_date_values("test_db")
        assert len(issues) == 0  # 실패 시 skip


# ============================================================
# CleanupAction / Orphan 테스트
# ============================================================
class TestGenerateCleanupSql:
    def test_delete_action(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="orders", child_column="user_id",
            parent_table="users", parent_column="id",
            orphan_count=5, sample_values=[99, 100]
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.DELETE, "test_db")
        assert "DELETE" in action.sql
        assert "FROM" in action.sql
        assert action.action_type == ActionType.DELETE
        assert action.affected_rows == 5
        assert action.dry_run is True
        assert action.target_schema == "test_db"
        assert action.target_table == "orders"

    def test_delete_action_uses_not_exists_not_not_in(self, fake_connector):
        """NOT IN은 부모 참조 컬럼에 NULL이 있으면 전체가 UNKNOWN이 되어
        실제 고아 레코드가 있어도 0건으로 처리되는 NULL-안전성 문제가 있다."""
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="orders", child_column="user_id",
            parent_table="users", parent_column="id",
            orphan_count=5
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.DELETE, "test_db")
        assert "NOT EXISTS" in action.sql
        assert "NOT IN" not in action.sql

    def test_set_null_action(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="orders", child_column="user_id",
            parent_table="users", parent_column="id",
            orphan_count=3
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.SET_NULL, "test_db")
        assert "SET" in action.sql
        assert "`user_id` = NULL" in action.sql
        assert "NOT EXISTS" in action.sql
        assert "NOT IN" not in action.sql
        assert action.target_schema == "test_db"
        assert action.target_table == "orders"

    def test_manual_action(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="orders", child_column="user_id",
            parent_table="users", parent_column="id",
            orphan_count=1
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.MANUAL, "test_db")
        assert "수동 처리" in action.sql


class TestExecuteCleanup:
    def test_dry_run_cleanup_still_counts_affected_rows(self, fake_connector):
        fake_connector.query_results = {
            "SELECT COUNT(*)": [{"cnt": 7}],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        action = CleanupAction(
            action_type=ActionType.DELETE,
            table="orders",
            description="delete orphan orders",
            sql="DELETE FROM `test_db`.`orders` WHERE `user_id` IS NULL",
            affected_rows=3,
            target_schema="test_db",
            target_table="orders",
        )

        success, message, affected = analyzer.execute_cleanup(action, dry_run=True)

        assert success is True
        assert affected == 7
        assert "[DRY-RUN]" in message
        assert len(fake_connector.executed_queries) == 1

    def test_dry_run_without_metadata_fails_explicitly(self, fake_connector):
        """target_schema/target_table이 없으면 sql 텍스트를 재파싱해 추측하지
        않고 명시적으로 실패한다 (구버전 직렬화 복원 등)."""
        analyzer = MigrationAnalyzer(fake_connector)
        action = CleanupAction(
            action_type=ActionType.DELETE,
            table="orders",
            description="delete orphan orders",
            sql="DELETE FROM `test_db`.`orders` WHERE `user_id` IS NULL",
            affected_rows=3,
        )

        success, message, affected = analyzer.execute_cleanup(action, dry_run=True)

        assert success is False
        assert affected == 0
        assert "메타데이터" in message
        assert fake_connector.executed_queries == []

    def test_dry_run_table_name_containing_from_and_set_keywords(self, fake_connector):
        """테이블명이 SETTINGS/ASSETS처럼 FROM/SET 키워드를 포함해도
        저장된 target_schema/target_table을 그대로 쓰므로 안전하다."""
        fake_connector.query_results = {
            "SELECT COUNT(*)": [{"cnt": 2}],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="ASSETS", child_column="owner_id",
            parent_table="users", parent_column="id",
            orphan_count=2
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.SET_NULL, "test_db")

        success, message, affected = analyzer.execute_cleanup(action, dry_run=True)

        assert success is True
        assert affected == 2
        executed_sql = fake_connector.executed_queries[0][0]
        assert "FROM `test_db`.`ASSETS` AS c" in executed_sql

    def test_actual_cleanup_rejects_legacy_python_mutation_mode(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        action = CleanupAction(
            action_type=ActionType.DELETE,
            table="orders",
            description="delete orphan orders",
            sql="DELETE FROM `test_db`.`orders` WHERE `user_id` IS NULL",
            affected_rows=3,
        )

        with pytest.raises(RuntimeError, match="Rust Core"):
            analyzer.execute_cleanup(action, dry_run=False)

        fake_connector.connection.cursor.assert_not_called()
        fake_connector.connection.commit.assert_not_called()
        fake_connector.connection.rollback.assert_not_called()
        assert fake_connector.executed_queries == []


# ============================================================
# AnalysisResult 직렬화 테스트
# ============================================================
class TestAnalysisResultSerialization:
    def test_to_dict_and_from_dict_roundtrip(self):
        result = AnalysisResult(
            schema="test_db",
            analyzed_at="2024-01-01T00:00:00",
            total_tables=5,
            total_fk_relations=2,
            orphan_records=[
                OrphanRecord("orders", "user_id", "users", "id", 3, [1, 2, 3])
            ],
            compatibility_issues=[
                CompatibilityIssue(
                    issue_type=IssueType.CHARSET_ISSUE,
                    severity="warning",
                    location="test_db.users",
                    description="utf8mb3",
                    suggestion="fix it"
                )
            ],
            cleanup_actions=[
                CleanupAction(
                    ActionType.DELETE, "orders", "desc", "DELETE ...", 3,
                    target_schema="test_db", target_table="orders"
                )
            ],
            fk_tree={"users": ["orders"]}
        )

        d = result.to_dict()
        assert d['schema'] == "test_db"
        assert d['total_tables'] == 5
        assert len(d['orphan_records']) == 1
        assert len(d['compatibility_issues']) == 1

        restored = AnalysisResult.from_dict(d)
        assert restored.schema == "test_db"
        assert len(restored.orphan_records) == 1
        assert restored.orphan_records[0].orphan_count == 3
        assert len(restored.compatibility_issues) == 1
        assert restored.compatibility_issues[0].issue_type == IssueType.CHARSET_ISSUE
        assert len(restored.cleanup_actions) == 1
        assert restored.cleanup_actions[0].target_schema == "test_db"
        assert restored.cleanup_actions[0].target_table == "orders"

    def test_from_dict_defaults_target_metadata_when_absent(self):
        """구버전 직렬화(target_schema/target_table 없음) 복원 시 예외 없이 None으로 채워진다"""
        d = {
            'schema': "test_db",
            'analyzed_at': "2024-01-01T00:00:00",
            'total_tables': 1,
            'total_fk_relations': 0,
            'orphan_records': [],
            'compatibility_issues': [],
            'cleanup_actions': [
                {
                    'action_type': 'delete',
                    'table': 'orders',
                    'description': 'desc',
                    'sql': 'DELETE ...',
                    'affected_rows': 3,
                }
            ],
            'fk_tree': {},
        }
        restored = AnalysisResult.from_dict(d)
        assert restored.cleanup_actions[0].target_schema is None
        assert restored.cleanup_actions[0].target_table is None


# ============================================================
# DumpFileAnalyzer 테스트
# ============================================================
class TestDumpFileAnalyzer:
    """DumpFileAnalyzer SQL 파일 분석 테스트"""

    def test_analyze_sql_file_finds_issues(self, tmp_path, sample_dump_sql):
        """샘플 SQL에서 이슈 탐지"""
        sql_file = tmp_path / "dump.sql"
        sql_file.write_text(sample_dump_sql, encoding='utf-8')

        analyzer = DumpFileAnalyzer()
        issues = analyzer._analyze_sql_file(sql_file)

        # 여러 이슈가 발견되어야 함
        issue_types = {i.issue_type for i in issues}
        # ZEROFILL, FLOAT_PRECISION, FTS_TABLE_PREFIX, SUPER_PRIVILEGE 등
        assert len(issues) >= 3

    def test_analyze_tsv_file_finds_invalid_dates(self, tmp_path):
        """TSV 파일에서 '0000-00-00' 탐지 (quoted)"""
        tsv_file = tmp_path / "data.tsv"
        tsv_file.write_text(
            "1\tJohn\t'2024-01-01'\n"
            "2\tJane\t'0000-00-00'\n"
            "3\tBob\t'2024-06-15'\n",
            encoding='utf-8'
        )

        analyzer = DumpFileAnalyzer()
        issues = analyzer._analyze_tsv_file(tsv_file)
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.INVALID_DATE

    def test_analyze_dump_folder(self, tmp_path, sample_dump_sql):
        """폴더 분석 통합 테스트"""
        sql_file = tmp_path / "schema.sql"
        sql_file.write_text(sample_dump_sql, encoding='utf-8')

        analyzer = DumpFileAnalyzer()
        result = analyzer.analyze_dump_folder(str(tmp_path))

        assert isinstance(result, DumpAnalysisResult)
        assert result.total_sql_files == 1
        assert len(result.compatibility_issues) >= 1

    def test_analyze_nonexistent_folder(self):
        analyzer = DumpFileAnalyzer()
        with pytest.raises(FileNotFoundError):
            analyzer.analyze_dump_folder("/nonexistent/path")

    def test_quick_scan(self, tmp_path, sample_dump_sql):
        sql_file = tmp_path / "schema.sql"
        sql_file.write_text(sample_dump_sql, encoding='utf-8')

        analyzer = DumpFileAnalyzer()
        errors, warnings, infos = analyzer.quick_scan(str(tmp_path))
        assert errors + warnings + infos >= 1

    def test_issue_callback(self, tmp_path, sample_dump_sql):
        """이슈 콜백이 호출되는지 확인"""
        sql_file = tmp_path / "schema.sql"
        sql_file.write_text(sample_dump_sql, encoding='utf-8')

        reported = []
        analyzer = DumpFileAnalyzer()
        analyzer.set_issue_callback(lambda i: reported.append(i))
        analyzer.analyze_dump_folder(str(tmp_path))

        assert len(reported) >= 1


class TestDumpFileAnalyzerSqlPatterns:
    """SQL 파일 내 각 패턴 탐지 상세 테스트"""

    def _analyze_sql(self, content: str, tmp_path, file_name: str = "test.sql") -> list:
        sql_file = tmp_path / file_name
        sql_file.write_text(content, encoding='utf-8')
        return DumpFileAnalyzer()._analyze_sql_file(sql_file)

    def test_zerofill_detection(self, tmp_path):
        issues = self._analyze_sql(
            "CREATE TABLE t (`id` int(8) UNSIGNED ZEROFILL);", tmp_path
        )
        assert any(i.issue_type == IssueType.ZEROFILL_USAGE for i in issues)

    def test_float_precision_detection(self, tmp_path):
        issues = self._analyze_sql(
            "CREATE TABLE t (`val` FLOAT(10,2));", tmp_path
        )
        assert any(i.issue_type == IssueType.FLOAT_PRECISION for i in issues)

    def test_fts_table_prefix(self, tmp_path):
        issues = self._analyze_sql(
            "CREATE TABLE `FTS_config` (`key` VARCHAR(50));", tmp_path
        )
        assert any(i.issue_type == IssueType.FTS_TABLE_PREFIX for i in issues)

    def test_super_privilege(self, tmp_path):
        issues = self._analyze_sql(
            "GRANT SUPER ON *.* TO 'admin'@'localhost';", tmp_path
        )
        assert any(i.issue_type == IssueType.SUPER_PRIVILEGE for i in issues)

    def test_auth_plugin_native(self, tmp_path):
        issues = self._analyze_sql(
            "CREATE USER 'old'@'%' IDENTIFIED WITH mysql_native_password;", tmp_path
        )
        assert any(i.issue_type == IssueType.AUTH_PLUGIN_ISSUE for i in issues)

    def test_reserved_keyword_table(self, tmp_path):
        issues = self._analyze_sql(
            "CREATE TABLE rank (`id` INT);", tmp_path
        )
        assert any(i.issue_type == IssueType.RESERVED_KEYWORD for i in issues)

    def test_sys_var_usage(self, tmp_path):
        issues = self._analyze_sql(
            "SET @@global.binlog_format = 'ROW';", tmp_path
        )
        assert any(i.issue_type == IssueType.REMOVED_SYS_VAR for i in issues)

    def test_trigger_new_old_row_references_not_flagged_as_sys_vars(self, tmp_path):
        issues = self._analyze_sql(
            """
            CREATE TRIGGER trg_orders_bu
            BEFORE UPDATE ON orders
            FOR EACH ROW
            BEGIN
                SET NEW.updated_at = NOW();
                SET OLD.status = 'archived';
            END;
            """,
            tmp_path,
            "orders.triggers.sql",
        )
        assert not any(i.issue_type == IssueType.REMOVED_SYS_VAR for i in issues)


# ============================================================
# TwoPassAnalyzer FK 유니크 참조 정확 매칭 회귀 테스트
# ============================================================
class TestTwoPassAnalyzerFkUniquenessCrossValidation:
    """FK 참조 컬럼이 실제로 PK/UNIQUE에 의해 유니크함을 보장받는지 검증.

    UNIQUE(a,b) 같은 복합 인덱스의 prefix (a)만으로 FK를 참조하는 경우는
    실제로는 유니크함이 보장되지 않으므로 FK_NON_UNIQUE_REF로 잡혀야 한다.
    """

    def test_fk_referencing_prefix_of_composite_unique_is_flagged(self, tmp_path):
        (tmp_path / "schema.sql").write_text(
            """
CREATE TABLE `parent` (
  `tenant_id` int,
  `code` int,
  UNIQUE KEY `uniq_tenant_code` (`tenant_id`, `code`)
);
CREATE TABLE `child` (
  `tenant_id` int,
  CONSTRAINT `fk_child_parent` FOREIGN KEY (`tenant_id`) REFERENCES `parent` (`tenant_id`)
);
""",
            encoding="utf-8",
        )
        analyzer = TwoPassAnalyzer()
        result = analyzer.analyze_dump_folder(str(tmp_path))
        non_unique = [i for i in result.compatibility_issues if i.issue_type == IssueType.FK_NON_UNIQUE_REF]
        assert len(non_unique) == 1

    def test_fk_referencing_exact_unique_column_is_valid(self, tmp_path):
        (tmp_path / "schema.sql").write_text(
            """
CREATE TABLE `parent` (
  `tenant_id` int,
  `code` int,
  UNIQUE KEY `uniq_tenant` (`tenant_id`)
);
CREATE TABLE `child` (
  `tenant_id` int,
  CONSTRAINT `fk_child_parent` FOREIGN KEY (`tenant_id`) REFERENCES `parent` (`tenant_id`)
);
""",
            encoding="utf-8",
        )
        analyzer = TwoPassAnalyzer()
        result = analyzer.analyze_dump_folder(str(tmp_path))
        non_unique = [i for i in result.compatibility_issues if i.issue_type == IssueType.FK_NON_UNIQUE_REF]
        assert non_unique == []
