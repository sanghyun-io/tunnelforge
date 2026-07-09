"""
migration_rules 단위 테스트

DataIntegrityRules, SchemaRules, StorageRules 검증.
"""
import pytest
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

from src.core.migration_constants import IssueType, ENGINE_POLICIES
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

    def test_enum_numeric_index_parses_enum_after_varchar_default(self):
        """varchar(255) DEFAULT ... 같은 중첩 괄호 이후에 오는 ENUM 컬럼도
        정규식 단독 스캔이 아닌 CreateTableParser 기반 파싱으로 인식해야 한다"""
        rules = DataIntegrityRules()
        content = (
            "CREATE TABLE `t` (\n"
            "  `id` int(11) NOT NULL,\n"
            "  `memo` varchar(255) DEFAULT NULL,\n"
            "  `status` enum('new','done') DEFAULT 'new'\n"
            ") ENGINE=InnoDB;\n"
            "\n"
            "INSERT INTO `t` (`id`, `memo`, `status`) VALUES (1, 'hello', 2);\n"
        )
        issues = rules.check_enum_numeric_index(content, "test.sql")
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.ENUM_NUMERIC_INDEX
        assert issues[0].column_name == "status"

    def test_enum_numeric_index_does_not_cross_statement_boundary(self):
        """한 INSERT 문의 튜플 검사가 다음 문장의 튜플까지 읽어서는 안 된다"""
        rules = DataIntegrityRules()
        content = (
            "CREATE TABLE `t` (\n"
            "  `id` int(11) NOT NULL,\n"
            "  `status` enum('new','done') DEFAULT 'new'\n"
            ") ENGINE=InnoDB;\n"
            "\n"
            "INSERT INTO `t` (`id`, `status`) VALUES (1, 'new');\n"
            "INSERT INTO `unrelated` (`a`, `b`) VALUES (1, 2);\n"
        )
        issues = rules.check_enum_numeric_index(content, "test.sql")
        assert issues == []

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
        # 덤프 파일만으로는 TIMESTAMP/DATETIME 컬럼 여부를 구분할 수 없으므로
        # error가 아닌 warning이어야 한다
        assert issues[0].severity == "warning"
        assert "컬럼 타입 미확인" in issues[0].description

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

    def test_invalid_datetime_single_value_not_double_counted(self, tmp_path):
        """같은 위치를 여러 패턴이 중복 매치해도 행 단위로 1회만 카운트해야 한다"""
        data_file = tmp_path / "data.tsv"
        data_file.write_text("1\t'0000-00-00'\n", encoding='utf-8')

        rules = DataIntegrityRules()
        issues = rules.check_invalid_datetime(data_file)
        assert len(issues) == 1
        assert "1개 행" in issues[0].description
        assert "2개" not in issues[0].description

    def test_invalid_datetime_file_read_failure_returns_info_issue(self, tmp_path):
        """파일 읽기 실패(존재하지 않는 경로) 시 형제 검사와 동일하게
        info severity 이슈 1건을 반환해야 한다 (CC-088)"""
        missing = tmp_path / "does_not_exist.tsv"

        rules = DataIntegrityRules()
        issues = rules.check_invalid_datetime(missing)
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert issues[0].issue_type == IssueType.INVALID_DATE
        assert "DATETIME 스캔 미완료" in issues[0].description

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
        assert issues[0].severity == "error"

    def test_enum_empty_value_definition_ignores_escaped_apostrophe(self):
        """enum('don''t','other')는 이스케이프된 작은따옴표이지 빈 값이 아니다"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            "enum": [
                {'TABLE_NAME': 'users', 'COLUMN_NAME': 'note', 'COLUMN_TYPE': "enum('don''t','other')"}
            ]
        }
        rules = DataIntegrityRules(connector=conn)
        issues = rules.check_enum_empty_value_definition("test_db")
        assert issues == []

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

    def test_zerofill_data_dependency_uses_limited_subquery(self):
        """ZEROFILL 배치 쿼리는 집계 레벨 LIMIT이 아니라, 내부 서브쿼리에
        행 수 상한을 적용한 bounded subquery를 사용해야 한다"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            "ZEROFILL": [
                {'TABLE_NAME': 't', 'COLUMN_NAME': 'code', 'COLUMN_TYPE': 'int(8) unsigned zerofill'}
            ],
            "FROM (SELECT": [
                {'code': 1}
            ],
        }
        rules = DataIntegrityRules(connector=conn)
        issues = rules.check_zerofill_data_dependency("test_db")
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.ZEROFILL_USAGE

        batch_query = conn.executed_queries[-1][0]
        assert "FROM (SELECT" in batch_query
        assert "LIMIT 100000" in batch_query
        assert not batch_query.rstrip().endswith("LIMIT 100")

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

    def test_generated_column_functions_password_reports_once(self):
        """ALL_REMOVED_FUNCTIONS에 중복이 섞여 있어도 PASSWORD는 한 번만 보고돼야 한다"""
        rules = SchemaRules()
        content = "`hash` VARCHAR(100) GENERATED ALWAYS AS (PASSWORD('test'))"
        issues = rules.check_generated_column_functions(content, "test.sql")
        assert len(issues) == 1
        assert issues[0].description.endswith("PASSWORD")

    def test_generated_column_functions_no_false_positive_on_identifier_substring(self):
        """shift_rate처럼 함수명을 부분 문자열로 포함하는 식별자는 오탐하면 안 된다"""
        rules = SchemaRules()
        content = "`total` DECIMAL(10,2) GENERATED ALWAYS AS (price * shift_rate)"
        issues = rules.check_generated_column_functions(content, "test.sql")
        assert issues == []

    def test_generated_column_functions_ifnull_not_duplicated_as_if(self):
        """IFNULL(...) 사용 시 IF와 IFNULL 두 건으로 중복 보고되면 안 된다"""
        rules = SchemaRules()
        content = "`x` INT GENERATED ALWAYS AS (IFNULL(a,b))"
        issues = rules.check_generated_column_functions(content, "test.sql")
        reported_funcs = [issue.description.split(":")[-1].strip() for issue in issues]
        assert reported_funcs == ["IFNULL"]

    def test_generated_column_functions_case_still_detected(self):
        """CASE는 함수 호출이 아닌 키워드이므로 괄호 없이도 계속 감지돼야 한다"""
        rules = SchemaRules()
        content = "`x` INT GENERATED ALWAYS AS (CASE WHEN a THEN b ELSE c END)"
        issues = rules.check_generated_column_functions(content, "test.sql")
        assert any(issue.description.endswith("CASE") for issue in issues)

    def test_check_all_sql_content(self):
        rules = SchemaRules()
        content = (
            "`birth` YEAR(2) DEFAULT NULL\n"
            "SELECT SQL_CALC_FOUND_ROWS * FROM t;\n"
            "`data` TEXT DEFAULT 'hello'\n"
        )
        issues = rules.check_all_sql_content(content, "test.sql")
        assert len(issues) >= 3

    def test_multiline_create_table_no_identifier_false_positives(self):
        """여러 줄 CREATE TABLE은 인접 식별자 사이를 오탐하지 않아야 한다"""
        rules = SchemaRules()
        content = (
            "CREATE TABLE `t` (\n"
            "  `id` int NOT NULL,\n"
            "  `name` varchar(255) DEFAULT NULL\n"
            ")"
        )
        issues = rules.check_all_sql_content(content, "test.sql")
        false_positive_types = {
            IssueType.DOLLAR_SIGN_NAME,
            IssueType.TRAILING_SPACE_NAME,
            IssueType.CONTROL_CHAR_NAME,
        }
        assert not any(issue.issue_type in false_positive_types for issue in issues)

    def test_invalid_57_name_multiple_dots_no_match_in_insert_data(self):
        rules = SchemaRules()
        content = "INSERT INTO t VALUES ('see notes..thanks');"
        issues = rules.check_invalid_57_name_multiple_dots(content, "test.sql")
        assert issues == []

    def test_invalid_57_name_multiple_dots_matches_schema_reference(self):
        rules = SchemaRules()
        content = "SELECT * FROM schema..table;"
        issues = rules.check_invalid_57_name_multiple_dots(content, "test.sql")
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.INVALID_57_NAME_MULTIPLE_DOTS


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

    def test_routine_definer_missing_mysql_user_permission_failure_returns_info_only(self):
        """mysql.user 조회 권한이 없으면 모든 definer를 '존재하지 않음'으로
        오판(spam)하지 말고, 검증 불가 info 이슈 1건만 반환해야 한다"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            "INFORMATION_SCHEMA.ROUTINES": [
                {'ROUTINE_NAME': 'r1', 'ROUTINE_TYPE': 'PROCEDURE', 'DEFINER': 'app@localhost'},
                {'ROUTINE_NAME': 'r2', 'ROUTINE_TYPE': 'FUNCTION', 'DEFINER': 'app@localhost'},
            ],
        }
        conn.fail_on = {"mysql.user": PermissionError("denied")}
        rules = SchemaRules(connector=conn)
        issues = rules.check_routine_definer_missing("test_db")
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert "Definer 검증 불가" in issues[0].description
        assert "존재하지 않음" not in issues[0].description

    def test_view_definer_missing_mysql_user_permission_failure_returns_info_only(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            "INFORMATION_SCHEMA.VIEWS": [
                {'TABLE_NAME': 'v1', 'DEFINER': 'app@localhost'},
                {'TABLE_NAME': 'v2', 'DEFINER': 'app@localhost'},
            ],
        }
        conn.fail_on = {"mysql.user": PermissionError("denied")}
        rules = SchemaRules(connector=conn)
        issues = rules.check_view_definer_missing("test_db")
        assert len(issues) == 1
        assert issues[0].severity == "info"
        assert "Definer 검증 불가" in issues[0].description
        assert "존재하지 않음" not in issues[0].description

    def test_routine_definer_missing_normal_behavior_still_flags_missing_user(self):
        """mysql.user 조회가 정상 동작하면 기존처럼 누락된 definer를 warning으로 보고해야 한다"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            "INFORMATION_SCHEMA.ROUTINES": [
                {'ROUTINE_NAME': 'r1', 'ROUTINE_TYPE': 'PROCEDURE', 'DEFINER': 'ghost@localhost'},
            ],
            "mysql.user": [
                {'definer': 'app@localhost'},
            ],
        }
        rules = SchemaRules(connector=conn)
        issues = rules.check_routine_definer_missing("test_db")
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "존재하지 않음" in issues[0].description

    def test_view_definer_missing_normal_behavior_still_flags_missing_user(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            "INFORMATION_SCHEMA.VIEWS": [
                {'TABLE_NAME': 'v1', 'DEFINER': 'ghost@localhost'},
            ],
            "mysql.user": [
                {'definer': 'app@localhost'},
            ],
        }
        rules = SchemaRules(connector=conn)
        issues = rules.check_view_definer_missing("test_db")
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "존재하지 않음" in issues[0].description

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

    def test_merge_engine_uses_engine_policies_severity(self):
        rules = StorageRules()
        content = "CREATE TABLE `t` (`id` INT) ENGINE=MERGE;"
        issues = rules.check_deprecated_engines_in_sql(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].severity == "error"
        assert issues[0].suggestion == ENGINE_POLICIES["MERGE"]["suggestion"]

    def test_csv_engine_detected_with_engine_policies(self):
        rules = StorageRules()
        content = "CREATE TABLE `t` (`id` INT) ENGINE=CSV;"
        issues = rules.check_deprecated_engines_in_sql(content, "test.sql")
        assert len(issues) >= 1
        assert issues[0].severity == ENGINE_POLICIES["CSV"]["severity"]
        assert issues[0].suggestion == ENGINE_POLICIES["CSV"]["suggestion"]


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

    def test_merge_and_csv_use_engine_policies_not_hardcoded(self):
        """MERGE/CSV의 severity/suggestion은 ENGINE_POLICIES에서 와야 하며
        과거처럼 하드코딩된 warning 텍스트를 사용해서는 안 된다"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            "ENGINE IN": [
                {'TABLE_NAME': 'merged', 'ENGINE': 'MERGE'},
                {'TABLE_NAME': 'logs_csv', 'ENGINE': 'CSV'},
            ]
        }
        rules = StorageRules(connector=conn)
        issues = rules.check_deprecated_engines("db")
        by_table = {issue.table_name: issue for issue in issues}

        assert by_table['merged'].severity == ENGINE_POLICIES['MERGE']['severity']
        assert by_table['merged'].suggestion == ENGINE_POLICIES['MERGE']['suggestion']
        assert by_table['logs_csv'].severity == ENGINE_POLICIES['CSV']['severity']
        assert by_table['logs_csv'].suggestion == ENGINE_POLICIES['CSV']['suggestion']
