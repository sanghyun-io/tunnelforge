"""
migration_analyzer.py 단위 테스트

MigrationAnalyzer(Rust upgrade.analyze 결과 변환/위임), DumpFileAnalyzer 검증.
호환성 검사 규칙 자체는 migration_core/src/upgrade_analyze.rs 와 live_upgrade_analyze.rs 가 검증한다.
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
    SchemaCheckOptions,
)


# ============================================================
# MigrationAnalyzer: Rust core 위임
# ============================================================
CORE_RESULT = {
    "event": "result", "command": "upgrade.analyze", "success": True,
    "schema": "app", "total_tables": 3, "total_fk_relations": 2,
    "fk_tree": {"parent": ["child"]},
    "issues": [
        {"issue_type": "charset_issue", "severity": "warning", "location": "app.legacy",
         "description": "테이블이 utf8mb3 collation 사용 중: utf8mb3_general_ci",
         "suggestion": "ALTER TABLE ... CONVERT TO CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci",
         "table_name": None, "column_name": None, "fix_query": None},
        {"issue_type": "invalid_date", "severity": "error", "location": "app.orders.created",
         "description": "잘못된 날짜값 3개 발견 (0000-00-00 등)", "suggestion": "NULL로 변경",
         "table_name": "orders", "column_name": "created",
         "fix_query": "UPDATE `app`.`orders` SET `created` = NULL WHERE ..."},
    ],
    "orphan_records": [{
        "constraint_name": "fk_pair", "child_table": "child", "child_column": "a, b",
        "parent_table": "parent", "parent_column": "x, y", "child_columns": ["a", "b"], "parent_columns": ["x", "y"],
        "orphan_count": 1, "sample_values": ["(1, 9)"],
        "cleanup_sql": {"delete": "DELETE c FROM `app`.`child` AS c\nWHERE ...",
                        "set_null": "UPDATE `app`.`child` AS c\nSET c.`a` = NULL, c.`b` = NULL\nWHERE ...",
                        "count": "SELECT COUNT(*) AS cnt FROM `app`.`child` AS c\nWHERE ..."},
    }],
}


def _connector_with_facade(result=None):
    from src.core.db_core_facade import DbEndpoint
    connector = MagicMock()
    connector.connection.endpoint = DbEndpoint("mysql", "127.0.0.1", 3306, "u", "p", "other_db")
    connector.connection.facade.analyze_upgrade.return_value = result or CORE_RESULT
    return connector


class TestAnalyzeSchemaDelegatesToRust:
    """분석은 Rust core upgrade.analyze 가 하고, 결과를 AnalysisResult 로 바꾼다"""

    def test_calls_core_with_schema_endpoint_and_options_and_forwards_progress(self):
        connector = _connector_with_facade()

        def fake_analyze(endpoint, options, on_event):
            on_event({"event": "progress", "message": "📌 [1/15] 고아 레코드 검사 시작..."})
            on_event({"event": "phase", "message": "ignored"})
            return CORE_RESULT

        connector.connection.facade.analyze_upgrade.side_effect = fake_analyze
        analyzer = MigrationAnalyzer(connector)
        messages = []
        analyzer.set_progress_callback(messages.append)

        result = analyzer.analyze_schema("app", check_orphans=False)

        endpoint, options = connector.connection.facade.analyze_upgrade.call_args.args
        assert endpoint.database == "app" and endpoint.host == "127.0.0.1"
        assert options == {"check_orphans": False}
        assert messages == ["📌 [1/15] 고아 레코드 검사 시작..."]
        assert result.schema == "app" and result.total_tables == 3 and result.total_fk_relations == 2

    def test_maps_issues_orphans_tree_and_default_cleanup_actions(self):
        result = MigrationAnalyzer(_connector_with_facade()).analyze_schema("app")

        assert [i.issue_type for i in result.compatibility_issues] == [IssueType.CHARSET_ISSUE, IssueType.INVALID_DATE]
        date_issue = result.compatibility_issues[1]
        assert (date_issue.severity, date_issue.table_name, date_issue.column_name) == ("error", "orders", "created")
        assert date_issue.fix_query.startswith("UPDATE `app`.`orders`")
        assert result.fk_tree == {"parent": ["child"]}

        orphan = result.orphan_records[0]
        assert (orphan.child_column, orphan.orphan_count, orphan.sample_values) == ("a, b", 1, ["(1, 9)"])
        action = result.cleanup_actions[0]
        assert action.action_type == ActionType.DELETE and action.sql.startswith("DELETE c FROM")
        assert action.count_sql.startswith("SELECT COUNT(*) AS cnt")

    def test_rejects_unknown_check_options(self):
        with pytest.raises(TypeError, match="check_typo"):
            MigrationAnalyzer(_connector_with_facade()).analyze_schema("app", check_typo=True)

    def test_requires_a_connected_connector(self):
        connector = MagicMock()
        connector.connection = None
        with pytest.raises(RuntimeError, match="연결"):
            MigrationAnalyzer(connector).analyze_schema("app")


class TestGenerateCleanupSql:
    """정리 SQL 은 분석 시 Rust core 가 만든 것을 쓴다"""

    def _orphan(self, cleanup_sql=None):
        return OrphanRecord("child", "a, b", "parent", "x, y", 1, [], cleanup_sql if cleanup_sql is not None else CORE_RESULT["orphan_records"][0]["cleanup_sql"])

    def test_delete_and_set_null_use_core_sql(self):
        analyzer = MigrationAnalyzer(MagicMock())
        delete = analyzer.generate_cleanup_sql(self._orphan(), ActionType.DELETE, "app")
        set_null = analyzer.generate_cleanup_sql(self._orphan(), ActionType.SET_NULL, "app")
        assert delete.sql.startswith("DELETE c FROM") and "1개 삭제" in delete.description
        assert "SET c.`a` = NULL, c.`b` = NULL" in set_null.sql
        assert delete.count_sql == set_null.count_sql and delete.target_table == "child"

    def test_manual_action(self):
        manual = MigrationAnalyzer(MagicMock()).generate_cleanup_sql(self._orphan(), ActionType.MANUAL, "app")
        assert manual.sql.startswith("-- 수동 처리 필요") and manual.count_sql is None

    def test_results_saved_by_older_versions_ask_for_a_new_analysis(self):
        action = MigrationAnalyzer(MagicMock()).generate_cleanup_sql(self._orphan({}), ActionType.DELETE, "app")
        assert "다시 분석" in action.sql and action.count_sql is None


class TestExecuteCleanup:
    def _action(self, count_sql="SELECT COUNT(*) AS cnt FROM t", action_type=ActionType.DELETE):
        return CleanupAction(action_type, "child", "d", "DELETE ...", 1, True, "app", "child", count_sql)

    def test_dry_run_counts_rows_with_the_core_count_sql(self):
        connector = MagicMock()
        connector.execute.return_value = [{"cnt": 7}]
        ok, message, affected = MigrationAnalyzer(connector).execute_cleanup(self._action())
        connector.execute.assert_called_once_with("SELECT COUNT(*) AS cnt FROM t")
        assert (ok, affected) == (True, 7) and "7개 행" in message

    def test_dry_run_without_count_sql_fails_explicitly(self):
        ok, message, affected = MigrationAnalyzer(MagicMock()).execute_cleanup(self._action(count_sql=None))
        assert (ok, affected) == (False, 0) and "메타데이터" in message

    def test_manual_action_needs_no_query(self):
        connector = MagicMock()
        assert MigrationAnalyzer(connector).execute_cleanup(self._action(action_type=ActionType.MANUAL))[0] is True
        connector.execute.assert_not_called()

    def test_actual_cleanup_rejects_legacy_python_mutation_mode(self):
        with pytest.raises(RuntimeError, match="Rust Core"):
            MigrationAnalyzer(MagicMock()).execute_cleanup(self._action(), dry_run=False)


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
