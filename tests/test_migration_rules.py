"""
migration_rules 단위 테스트

DataIntegrityRules, SchemaRules, StorageRules 검증.
"""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.core.migration_constants import IssueType
from src.core.migration_rules.data_rules import DataIntegrityRules
from src.core.migration_rules.schema_rules import SchemaRules
from src.core.migration_rules.storage_rules import StorageRules
from tests.conftest import FakeMySQLConnector


# ============================================================
# DataIntegrityRules 테스트
# ============================================================
class TestDataIntegrityRulesSQL:
    """SQL 파일 기반 데이터 무결성 검사"""

    def test_enum_empty_in_sql(self):
        rules = DataIntegrityRules()
        content = "`status` ENUM('active','','inactive')"
        issues = rules.check_enum_empty_in_sql(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.ENUM_EMPTY_VALUE

    def test_enum_empty_not_found(self):
        rules = DataIntegrityRules()
        content = "`status` ENUM('active','inactive')"
        issues = rules.check_enum_empty_in_sql(content, "test.sql")
        assert len(issues) == 0

    def test_enum_empty_insert(self):
        rules = DataIntegrityRules()
        content = "INSERT INTO users VALUES (1, '', 'active');"
        issues = rules.check_enum_empty_insert(content, "test.sql")
        assert len(issues) >= 1

    def test_enum_numeric_index(self):
        """D03은 스키마 정보 없이 감지 어려우므로 빈 리스트 반환"""
        rules = DataIntegrityRules()
        content = "INSERT INTO users VALUES (1, 2, 'active');"
        issues = rules.check_enum_numeric_index(content, "test.sql")
        assert len(issues) == 0

    def test_check_all_sql_content(self):
        rules = DataIntegrityRules()
        content = (
            "INSERT INTO users VALUES (1, '', 'active');\n"
            "`status` ENUM('','inactive')\n"
        )
        issues = rules.check_all_sql_content(content, "test.sql")
        assert len(issues) >= 2


class TestDataIntegrityRulesFiles:
    """데이터 파일 기반 검사"""

    def test_4byte_utf8_detection(self, tmp_path):
        # 4바이트 UTF-8: 이모지
        data_file = tmp_path / "data.tsv"
        data_file.write_bytes(b"1\thello \xf0\x9f\x98\x80\n2\tworld\n")

        rules = DataIntegrityRules()
        issues = rules.check_4byte_utf8_in_data(data_file)
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.DATA_4BYTE_UTF8

    def test_4byte_utf8_clean_file(self, tmp_path):
        data_file = tmp_path / "data.tsv"
        data_file.write_bytes(b"1\thello\n2\tworld\n")

        rules = DataIntegrityRules()
        issues = rules.check_4byte_utf8_in_data(data_file)
        assert len(issues) == 0

    def test_null_byte_detection(self, tmp_path):
        data_file = tmp_path / "data.tsv"
        data_file.write_bytes(b"1\thello\x00world\n2\tnormal\n")

        rules = DataIntegrityRules()
        issues = rules.check_null_byte_in_data(data_file)
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.DATA_NULL_BYTE

    def test_null_byte_clean(self, tmp_path):
        data_file = tmp_path / "data.tsv"
        data_file.write_bytes(b"1\thello\n2\tworld\n")

        rules = DataIntegrityRules()
        issues = rules.check_null_byte_in_data(data_file)
        assert len(issues) == 0

    def test_timestamp_range(self, tmp_path):
        data_file = tmp_path / "data.tsv"
        data_file.write_text(
            "1\t'2024-01-15 10:00:00'\n"
            "2\t'2050-06-15 12:00:00'\n"
            "3\t'1960-01-01 00:00:00'\n",
            encoding='utf-8'
        )

        rules = DataIntegrityRules()
        issues = rules.check_timestamp_range(data_file)
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.TIMESTAMP_RANGE

    def test_invalid_datetime(self, tmp_path):
        data_file = tmp_path / "data.tsv"
        data_file.write_text(
            "1\t'2024-01-15'\n"
            "2\t'0000-00-00'\n",
            encoding='utf-8'
        )

        rules = DataIntegrityRules()
        issues = rules.check_invalid_datetime(data_file)
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.INVALID_DATE

    def test_check_all_data_file(self, tmp_path):
        data_file = tmp_path / "data.tsv"
        data_file.write_bytes(b"1\t0000-00-00\thello\x00\n")

        rules = DataIntegrityRules()
        issues = rules.check_all_data_file(data_file)
        assert len(issues) >= 1


class TestDataIntegrityRulesLiveDB:
    """라이브 DB 기반 검사 (FakeMySQLConnector 사용)"""

    def test_enum_empty_value_definition(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            "enum": [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'status', 'COLUMN_TYPE': "enum('active','','inactive')"}
            ]
        }
        rules = DataIntegrityRules(connector=conn)
        issues = rules.check_enum_empty_value_definition("test_db")
        assert len(issues) >= 1

    def test_enum_element_length(self):
        long_value = 'x' * 300
        conn = FakeMySQLConnector()
        conn.query_results = {
            "enum": [
                {'TABLE_NAME': 't', 'COLUMN_NAME': 'c', 'COLUMN_TYPE': f"enum('short','{long_value}')"}
            ]
        }
        rules = DataIntegrityRules(connector=conn)
        issues = rules.check_enum_element_length("test_db")
        assert len(issues) >= 1

    def test_set_element_length(self):
        long_value = 'x' * 300
        conn = FakeMySQLConnector()
        conn.query_results = {
            "set": [
                {'TABLE_NAME': 't', 'COLUMN_NAME': 'c', 'COLUMN_TYPE': f"set('short','{long_value}')"}
            ]
        }
        rules = DataIntegrityRules(connector=conn)
        issues = rules.check_set_element_length("test_db")
        assert len(issues) >= 1

    def test_no_connector_returns_empty(self):
        rules = DataIntegrityRules()  # no connector
        assert rules.check_enum_empty_value_definition("db") == []
        assert rules.check_enum_element_length("db") == []
        assert rules.check_set_element_length("db") == []
        assert rules.check_latin1_non_ascii("db") == []
        assert rules.check_zerofill_data_dependency("db") == []
        assert rules.check_all_live_db("db") == []

    def test_extract_enum_elements(self):
        rules = DataIntegrityRules()
        elements = rules._extract_enum_elements("enum('a','b','c''s')")
        assert elements == ['a', 'b', "c's"]

    def test_extract_enum_empty(self):
        rules = DataIntegrityRules()
        elements = rules._extract_enum_elements("enum('','active')")
        assert '' in elements


# ============================================================
# SchemaRules 테스트
# ============================================================
class TestSchemaRulesSQL:
    """SQL 파일 기반 스키마 검사"""

    def test_year2_in_sql(self):
        rules = SchemaRules()
        content = "`birth` YEAR(2) DEFAULT NULL"
        issues = rules.check_year2_in_sql(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.YEAR2_TYPE

    def test_groupby_asc_desc(self):
        rules = SchemaRules()
        content = "SELECT * FROM t GROUP BY col ASC;"
        issues = rules.check_groupby_asc_desc(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.GROUPBY_ASC_DESC

    def test_sql_calc_found_rows(self):
        rules = SchemaRules()
        content = "SELECT SQL_CALC_FOUND_ROWS * FROM t LIMIT 10;"
        issues = rules.check_sql_calc_found_rows(content, "test.sql")
        assert len(issues) >= 1

    def test_found_rows_func(self):
        rules = SchemaRules()
        content = "SELECT FOUND_ROWS();"
        issues = rules.check_sql_calc_found_rows(content, "test.sql")
        assert len(issues) >= 1

    def test_dollar_sign_names(self):
        rules = SchemaRules()
        content = "CREATE TABLE t (`price$usd` INT);"
        issues = rules.check_dollar_sign_names(content, "test.sql")
        assert len(issues) >= 1

    def test_trailing_space_names(self):
        rules = SchemaRules()
        content = "CREATE TABLE t (`name ` VARCHAR(50));"
        issues = rules.check_trailing_space_names(content, "test.sql")
        assert len(issues) >= 1

    def test_control_char_names(self):
        rules = SchemaRules()
        content = "CREATE TABLE t (`na\x00me` VARCHAR(50));"
        issues = rules.check_control_char_names(content, "test.sql")
        assert len(issues) >= 1

    def test_blob_text_default(self):
        rules = SchemaRules()
        content = "`data` TEXT DEFAULT 'hello'"
        issues = rules.check_blob_text_default(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.BLOB_TEXT_DEFAULT

    def test_generated_column_functions(self):
        rules = SchemaRules()
        content = "`hash` VARCHAR(100) GENERATED ALWAYS AS (PASSWORD('test'))"
        issues = rules.check_generated_column_functions(content, "test.sql")
        assert len(issues) >= 1

    def test_check_all_sql_content(self):
        rules = SchemaRules()
        content = (
            "`birth` YEAR(2) DEFAULT NULL\n"
            "SELECT SQL_CALC_FOUND_ROWS * FROM t;\n"
            "`data` TEXT DEFAULT 'hello'\n"
        )
        issues = rules.check_all_sql_content(content, "test.sql")
        assert len(issues) >= 3


class TestSchemaRulesLiveDB:
    """라이브 DB 기반 스키마 검사"""

    def test_year2_type(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            "year(2)": [
                {'TABLE_NAME': 't', 'COLUMN_NAME': 'yr', 'COLUMN_TYPE': 'year(2)'}
            ]
        }
        rules = SchemaRules(connector=conn)
        issues = rules.check_year2_type("db")
        assert len(issues) >= 1

    def test_latin1_charset(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            "latin1_": [
                {'TABLE_NAME': 't', 'TABLE_COLLATION': 'latin1_swedish_ci'}
            ],
            "'latin1'": [
                {'TABLE_NAME': 't', 'COLUMN_NAME': 'c', 'CHARACTER_SET_NAME': 'latin1'}
            ],
        }
        rules = SchemaRules(connector=conn)
        issues = rules.check_latin1_charset("db")
        assert len(issues) >= 2

    def test_mysql_schema_conflict(self):
        conn = FakeMySQLConnector()
        conn._tables = {'db': ['tables', 'users']}  # 'tables'는 mysql 스키마와 충돌
        rules = SchemaRules(connector=conn)
        issues = rules.check_mysql_schema_conflict("db")
        assert len(issues) >= 1

    def test_no_connector_returns_empty(self):
        rules = SchemaRules()
        assert rules.check_year2_type("db") == []
        assert rules.check_latin1_charset("db") == []
        assert rules.check_index_too_large("db") == []
        assert rules.check_old_geometry_types("db") == []
        assert rules.check_mysql_schema_conflict("db") == []
        assert rules.check_routine_definer_missing("db") == []
        assert rules.check_view_definer_missing("db") == []
        assert rules.check_all_live_db("db") == []


class TestSchemaRulesIndexSize:
    """인덱스 크기 계산 테스트"""

    def test_varchar_utf8mb4(self):
        rules = SchemaRules()
        col = {
            'DATA_TYPE': 'varchar',
            'CHARACTER_MAXIMUM_LENGTH': 255,
            'CHARACTER_SET_NAME': 'utf8mb4',
            'SUB_PART': None,
        }
        size = rules.calculate_column_byte_size(col)
        assert size == 255 * 4 + 2  # 4 bytes per char + 2 length bytes

    def test_int_type(self):
        rules = SchemaRules()
        col = {
            'DATA_TYPE': 'int',
            'CHARACTER_MAXIMUM_LENGTH': None,
            'CHARACTER_SET_NAME': None,
            'SUB_PART': None,
        }
        assert rules.calculate_column_byte_size(col) == 4

    def test_varchar_with_prefix(self):
        rules = SchemaRules()
        col = {
            'DATA_TYPE': 'varchar',
            'CHARACTER_MAXIMUM_LENGTH': 255,
            'CHARACTER_SET_NAME': 'utf8mb4',
            'SUB_PART': 50,
        }
        size = rules.calculate_column_byte_size(col)
        assert size == 50 * 4 + 2

    def test_date_type(self):
        rules = SchemaRules()
        col = {
            'DATA_TYPE': 'date',
            'CHARACTER_MAXIMUM_LENGTH': None,
            'CHARACTER_SET_NAME': None,
            'SUB_PART': None,
        }
        assert rules.calculate_column_byte_size(col) == 3


# ============================================================
# StorageRules 테스트
# ============================================================
class TestStorageRulesSQL:
    """SQL 파일 기반 스토리지 엔진 검사"""

    def test_finds_myisam(self):
        rules = StorageRules()
        content = "CREATE TABLE `t` (`id` INT) ENGINE=MyISAM;"
        issues = rules.check_deprecated_engines_in_sql(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].issue_type == IssueType.DEPRECATED_ENGINE

    def test_finds_archive(self):
        rules = StorageRules()
        content = "CREATE TABLE `t` (`id` INT) ENGINE=ARCHIVE;"
        issues = rules.check_deprecated_engines_in_sql(content, "test.sql")
        assert len(issues) >= 1

    def test_no_deprecated_engine(self):
        rules = StorageRules()
        content = "CREATE TABLE `t` (`id` INT) ENGINE=InnoDB;"
        issues = rules.check_deprecated_engines_in_sql(content, "test.sql")
        assert len(issues) == 0

    def test_ha_partition(self):
        rules = StorageRules()
        content = "-- ha_partition reference"
        issues = rules.check_partition_non_native(content, "test.sql")
        assert len(issues) >= 1

    def test_check_all_sql_content(self):
        rules = StorageRules()
        content = "CREATE TABLE `t` (`id` INT) ENGINE=MyISAM;\nha_partition"
        issues = rules.check_all_sql_content(content, "test.sql")
        assert len(issues) >= 2


class TestStorageRulesLiveDB:
    """라이브 DB 기반 스토리지 엔진 검사"""

    def test_deprecated_engines(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            "ENGINE IN": [
                {'TABLE_NAME': 'logs', 'ENGINE': 'MyISAM'},
                {'TABLE_NAME': 'archive', 'ENGINE': 'ARCHIVE'},
            ]
        }
        rules = StorageRules(connector=conn)
        issues = rules.check_deprecated_engines("db")
        assert len(issues) >= 2

    def test_no_deprecated_engines(self):
        conn = FakeMySQLConnector()
        conn.query_results = {'ENGINE IN': []}
        rules = StorageRules(connector=conn)
        issues = rules.check_deprecated_engines("db")
        assert len(issues) == 0

    def test_no_connector(self):
        rules = StorageRules()
        assert rules.check_deprecated_engines("db") == []
        assert rules.check_partition_shared_tablespace("db") == []
        assert rules.check_all_live_db("db") == []
        assert rules.get_engine_statistics("db") == {}

    def test_engine_statistics(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'GROUP BY ENGINE': [
                {'ENGINE': 'InnoDB', 'table_count': 50},
                {'ENGINE': 'MyISAM', 'table_count': 5},
            ]
        }
        rules = StorageRules(connector=conn)
        stats = rules.get_engine_statistics("db")
        assert stats['InnoDB'] == 50
        assert stats['MyISAM'] == 5

    def test_progress_callback(self):
        conn = FakeMySQLConnector()
        conn.query_results = {'ENGINE IN': []}
        rules = StorageRules(connector=conn)

        messages = []
        rules.set_progress_callback(lambda m: messages.append(m))
        rules.check_deprecated_engines("db")
        assert len(messages) >= 1
