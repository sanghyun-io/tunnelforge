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
    def test_finds_out_of_range(self, fake_connector):
        fake_connector.query_results = {
            "timestamp": [
                {'TABLE_NAME': 'events', 'COLUMN_NAME': 'event_time'}
            ],
            "2038-01-19": [{'cnt': 5}],
        }
        analyzer = MigrationAnalyzer(fake_connector)
        issues = analyzer.check_timestamp_range("test_db")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.TIMESTAMP_RANGE


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
        assert "DELETE FROM" in action.sql
        assert action.action_type == ActionType.DELETE
        assert action.affected_rows == 5
        assert action.dry_run is True

    def test_set_null_action(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="orders", child_column="user_id",
            parent_table="users", parent_column="id",
            orphan_count=3
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.SET_NULL, "test_db")
        assert "SET `user_id` = NULL" in action.sql

    def test_manual_action(self, fake_connector):
        analyzer = MigrationAnalyzer(fake_connector)
        orphan = OrphanRecord(
            child_table="orders", child_column="user_id",
            parent_table="users", parent_column="id",
            orphan_count=1
        )
        action = analyzer.generate_cleanup_sql(orphan, ActionType.MANUAL, "test_db")
        assert "수동 처리" in action.sql


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
                CleanupAction(ActionType.DELETE, "orders", "desc", "DELETE ...", 3)
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

    def _analyze_sql(self, content: str, tmp_path) -> list:
        sql_file = tmp_path / "test.sql"
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
