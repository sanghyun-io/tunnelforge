"""
SQL Validator 유닛 테스트
- SchemaMetadata: 테이블/컬럼 존재 여부, 유사 이름 제안
- SQLValidator: 테이블/컬럼 검증, 버전 호환성
- SQLAutoCompleter: 컨텍스트 기반 자동완성
"""
import unittest
import sys
import os

# 프로젝트 루트를 path에 추가
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.sql_validator import (
    SchemaMetadata, SchemaMetadataProvider, SQLValidator, SQLAutoCompleter,
    ValidationIssue, IssueSeverity
)


class TestSchemaMetadata(unittest.TestCase):
    """SchemaMetadata 클래스 테스트"""

    def setUp(self):
        """테스트용 메타데이터 설정"""
        self.metadata = SchemaMetadata()
        # 임의의 테이블 설정
        self.metadata.tables = {'users', 'orders', 'products', 'categories', 'order_items'}
        # 임의의 컬럼 설정
        self.metadata.columns = {
            'users': {'id', 'name', 'email', 'created_at', 'updated_at'},
            'orders': {'id', 'user_id', 'total_amount', 'status', 'created_at'},
            'products': {'id', 'name', 'price', 'category_id', 'stock'},
            'categories': {'id', 'name', 'parent_id'},
            'order_items': {'id', 'order_id', 'product_id', 'quantity', 'price'},
        }
        self.metadata.db_version = (8, 0, 32)

    # =========================================================================
    # 테이블 존재 여부 테스트
    # =========================================================================
    def test_has_table_success(self):
        """[SUCCESS] 존재하는 테이블 확인"""
        self.assertTrue(self.metadata.has_table('users'))
        self.assertTrue(self.metadata.has_table('orders'))
        self.assertTrue(self.metadata.has_table('products'))

    def test_has_table_success_case_insensitive(self):
        """[SUCCESS] 대소문자 무시 테이블 확인"""
        self.assertTrue(self.metadata.has_table('USERS'))
        self.assertTrue(self.metadata.has_table('Users'))
        self.assertTrue(self.metadata.has_table('uSeRs'))

    def test_has_table_fail(self):
        """[FAIL] 존재하지 않는 테이블 확인"""
        self.assertFalse(self.metadata.has_table('userss'))  # 오타
        self.assertFalse(self.metadata.has_table('user'))    # 단수형
        self.assertFalse(self.metadata.has_table('accounts'))
        self.assertFalse(self.metadata.has_table(''))

    def test_get_table_name_success(self):
        """[SUCCESS] 실제 테이블명 반환"""
        # 대소문자 다르게 입력해도 원본 반환
        result = self.metadata.get_table_name('USERS')
        self.assertIn(result.lower(), ['users'])

    def test_get_table_name_fail(self):
        """[FAIL] 없는 테이블은 None 반환"""
        self.assertIsNone(self.metadata.get_table_name('nonexistent'))
        self.assertIsNone(self.metadata.get_table_name('userss'))

    # =========================================================================
    # 컬럼 존재 여부 테스트
    # =========================================================================
    def test_has_column_success(self):
        """[SUCCESS] 존재하는 컬럼 확인"""
        self.assertTrue(self.metadata.has_column('users', 'id'))
        self.assertTrue(self.metadata.has_column('users', 'name'))
        self.assertTrue(self.metadata.has_column('users', 'email'))
        self.assertTrue(self.metadata.has_column('orders', 'user_id'))

    def test_has_column_success_case_insensitive(self):
        """[SUCCESS] 대소문자 무시 컬럼 확인"""
        self.assertTrue(self.metadata.has_column('users', 'ID'))
        self.assertTrue(self.metadata.has_column('users', 'NAME'))
        self.assertTrue(self.metadata.has_column('USERS', 'email'))

    def test_has_column_fail_wrong_column(self):
        """[FAIL] 존재하지 않는 컬럼"""
        self.assertFalse(self.metadata.has_column('users', 'nmae'))     # 오타
        self.assertFalse(self.metadata.has_column('users', 'username'))  # 없는 컬럼
        self.assertFalse(self.metadata.has_column('users', 'password'))

    def test_has_column_fail_wrong_table(self):
        """[FAIL] 존재하지 않는 테이블의 컬럼"""
        self.assertFalse(self.metadata.has_column('userss', 'id'))
        self.assertFalse(self.metadata.has_column('nonexistent', 'name'))

    def test_has_column_fail_column_in_different_table(self):
        """[FAIL] 다른 테이블의 컬럼"""
        self.assertFalse(self.metadata.has_column('users', 'total_amount'))  # orders 컬럼
        self.assertFalse(self.metadata.has_column('products', 'user_id'))    # orders 컬럼

    # =========================================================================
    # 유사 이름 제안 테스트
    # =========================================================================
    def test_get_similar_tables_success(self):
        """[SUCCESS] 유사 테이블명 제안"""
        # 'userss' → 'users' 제안
        suggestions = self.metadata.get_similar_tables('userss')
        self.assertIn('users', suggestions)

        # 'order' → 'orders' 제안
        suggestions = self.metadata.get_similar_tables('order')
        self.assertIn('orders', suggestions)

    def test_get_similar_tables_no_match(self):
        """[FAIL] 유사 테이블명 없음"""
        suggestions = self.metadata.get_similar_tables('xyzabc')
        self.assertEqual(len(suggestions), 0)

    def test_get_similar_columns_success(self):
        """[SUCCESS] 유사 컬럼명 제안"""
        # 'nmae' → 'name' 제안
        suggestions = self.metadata.get_similar_columns('users', 'nmae')
        self.assertIn('name', suggestions)

        # 'emial' → 'email' 제안
        suggestions = self.metadata.get_similar_columns('users', 'emial')
        self.assertIn('email', suggestions)

    def test_get_similar_columns_no_match(self):
        """[FAIL] 유사 컬럼명 없음"""
        suggestions = self.metadata.get_similar_columns('users', 'xyzabc')
        self.assertEqual(len(suggestions), 0)


class TestSQLValidator(unittest.TestCase):
    """SQLValidator 클래스 테스트"""

    def setUp(self):
        """테스트용 Validator 설정"""
        self.provider = SchemaMetadataProvider()

        # Mock 메타데이터 직접 설정
        metadata = SchemaMetadata()
        metadata.tables = {'users', 'orders', 'products'}
        metadata.columns = {
            'users': {'id', 'name', 'email', 'created_at'},
            'orders': {'id', 'user_id', 'total_amount', 'status'},
            'products': {'id', 'name', 'price', 'category_id'},
        }
        metadata.db_version = (5, 7, 44)  # MySQL 5.7 (8.0 기능 미지원)
        self.provider._metadata = metadata

        self.validator = SQLValidator(self.provider)

    # =========================================================================
    # 테이블 검증 테스트
    # =========================================================================
    def test_validate_table_success(self):
        """[SUCCESS] 존재하는 테이블 - 이슈 없음"""
        sql = "SELECT * FROM users"
        issues = self.validator.validate(sql)

        # 테이블 관련 ERROR 없어야 함
        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_table_success_multiple(self):
        """[SUCCESS] 여러 테이블 JOIN"""
        sql = "SELECT u.name, o.total_amount FROM users u JOIN orders o ON u.id = o.user_id"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_table_fail_not_exists(self):
        """[FAIL] 존재하지 않는 테이블"""
        sql = "SELECT * FROM userss"  # 오타
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 1)
        self.assertIn('userss', table_errors[0].message)
        # 제안에 'users' 포함
        self.assertIn('users', table_errors[0].suggestions)

    def test_validate_table_fail_multiple_errors(self):
        """[FAIL] 여러 테이블 오류"""
        sql = "SELECT * FROM userss u JOIN orderss o ON u.id = o.user_id"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 2)

    def test_validate_table_insert(self):
        """[FAIL] INSERT INTO 존재하지 않는 테이블"""
        sql = "INSERT INTO userss (name, email) VALUES ('test', 'test@test.com')"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 1)

    def test_validate_table_update(self):
        """[FAIL] UPDATE 존재하지 않는 테이블"""
        sql = "UPDATE userss SET name = 'test' WHERE id = 1"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 1)

    def test_validate_table_delete(self):
        """[FAIL] DELETE FROM 존재하지 않는 테이블"""
        sql = "DELETE FROM userss WHERE id = 1"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 1)

    def test_validate_system_schema_information_schema(self):
        """[SUCCESS] INFORMATION_SCHEMA 시스템 스키마는 검증 제외"""
        sql = """
        SELECT TABLESPACE_NAME, FILE_NAME, FILE_TYPE
        FROM INFORMATION_SCHEMA.FILES
        WHERE FILE_NAME NOT LIKE CONCAT(@@datadir, '%')
        """
        issues = self.validator.validate(sql)

        # INFORMATION_SCHEMA.FILES는 시스템 스키마이므로 에러 없어야 함
        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_system_schema_mysql(self):
        """[SUCCESS] mysql 시스템 스키마는 검증 제외"""
        sql = "SELECT * FROM mysql.user"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_system_schema_performance_schema(self):
        """[SUCCESS] performance_schema 시스템 스키마는 검증 제외"""
        sql = "SELECT * FROM performance_schema.threads"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_system_schema_sys(self):
        """[SUCCESS] sys 시스템 스키마는 검증 제외"""
        sql = "SELECT * FROM sys.version"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_system_schema_ndbinfo(self):
        """[SUCCESS] ndbinfo 시스템 스키마는 검증 제외 (NDB Cluster)"""
        sql = "SELECT * FROM ndbinfo.nodes"
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_system_schema_case_insensitive(self):
        """[SUCCESS] 시스템 스키마 대소문자 구분 없음"""
        # 다양한 대소문자 조합 테스트
        sqls = [
            "SELECT * FROM INFORMATION_SCHEMA.TABLES",
            "SELECT * FROM information_schema.tables",
            "SELECT * FROM Information_Schema.Tables",
            "SELECT * FROM MYSQL.user",
            "SELECT * FROM MySQL.User",
        ]
        for sql in sqls:
            issues = self.validator.validate(sql)
            table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR and '테이블' in i.message]
            self.assertEqual(len(table_errors), 0, f"Failed for: {sql}")

    # =========================================================================
    # 컬럼 검증 테스트
    # =========================================================================
    def test_validate_column_success(self):
        """[SUCCESS] 존재하는 컬럼 - table.column 형식"""
        sql = "SELECT users.id, users.name FROM users"
        issues = self.validator.validate(sql)

        column_warnings = [i for i in issues if i.severity == IssueSeverity.WARNING and '컬럼' in i.message]
        self.assertEqual(len(column_warnings), 0)

    def test_validate_column_fail_not_exists(self):
        """[FAIL] 존재하지 않는 컬럼"""
        sql = "SELECT users.id, users.nmae FROM users"  # nmae 오타
        issues = self.validator.validate(sql)

        column_warnings = [i for i in issues if i.severity == IssueSeverity.WARNING and '컬럼' in i.message]
        self.assertEqual(len(column_warnings), 1)
        self.assertIn('nmae', column_warnings[0].message)
        # 제안에 'name' 포함
        self.assertIn('name', column_warnings[0].suggestions)

    def test_validate_column_fail_wrong_table(self):
        """[FAIL] 다른 테이블의 컬럼 사용"""
        sql = "SELECT users.id, users.total_amount FROM users"  # total_amount는 orders 컬럼
        issues = self.validator.validate(sql)

        column_warnings = [i for i in issues if i.severity == IssueSeverity.WARNING and '컬럼' in i.message]
        self.assertEqual(len(column_warnings), 1)

    def test_validate_column_with_alias(self):
        """[SUCCESS] 테이블 별칭 사용"""
        sql = "SELECT u.id, u.name FROM users u"
        issues = self.validator.validate(sql)

        column_warnings = [i for i in issues if i.severity == IssueSeverity.WARNING and '컬럼' in i.message]
        self.assertEqual(len(column_warnings), 0)

    # =========================================================================
    # 문자열 내부 무시 테스트
    # =========================================================================
    def test_validate_ignore_string_literal(self):
        """[SUCCESS] 문자열 내부는 검증하지 않음"""
        sql = "SELECT * FROM users WHERE name = 'FROM nonexistent_table'"
        issues = self.validator.validate(sql)

        # 문자열 내 'nonexistent_table'은 무시해야 함
        table_errors = [i for i in issues if 'nonexistent_table' in i.message]
        self.assertEqual(len(table_errors), 0)

    def test_validate_ignore_string_with_column(self):
        """[SUCCESS] 문자열 내 컬럼 패턴 무시"""
        sql = "SELECT * FROM users WHERE email LIKE '%users.fake_column%'"
        issues = self.validator.validate(sql)

        # 문자열 내부는 무시
        column_warnings = [i for i in issues if 'fake_column' in i.message]
        self.assertEqual(len(column_warnings), 0)

    # =========================================================================
    # DB 버전 호환성 테스트 (MySQL 5.7)
    # =========================================================================
    def test_validate_version_mysql8_keyword_warning(self):
        """[WARNING] MySQL 5.7에서 8.0+ 키워드 사용"""
        sql = "SELECT id, ROW_NUMBER() OVER (ORDER BY id) as rn FROM users"
        issues = self.validator.validate(sql)

        version_warnings = [i for i in issues if '8.0' in i.message]
        self.assertGreater(len(version_warnings), 0)

    def test_validate_version_mysql8_function_warning(self):
        """[WARNING] MySQL 5.7에서 8.0+ 함수 사용"""
        sql = "SELECT * FROM users WHERE REGEXP_LIKE(name, '^test')"
        issues = self.validator.validate(sql)

        version_warnings = [i for i in issues if '8.0' in i.message]
        self.assertGreater(len(version_warnings), 0)

    def test_validate_version_no_warning_on_mysql8(self):
        """[SUCCESS] MySQL 8.0에서는 경고 없음"""
        # MySQL 8.0으로 변경
        self.provider._metadata.db_version = (8, 0, 32)

        sql = "SELECT id, ROW_NUMBER() OVER (ORDER BY id) as rn FROM users"
        issues = self.validator.validate(sql)

        version_warnings = [i for i in issues if '8.0' in i.message]
        self.assertEqual(len(version_warnings), 0)

    # =========================================================================
    # 위치 정보 테스트
    # =========================================================================
    def test_validate_issue_position(self):
        """이슈의 줄/컬럼 위치 정확성"""
        sql = "SELECT * FROM userss"  # userss는 14번째 위치 (0-based)
        issues = self.validator.validate(sql)

        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0].line, 0)
        self.assertEqual(issues[0].column, 14)
        self.assertEqual(issues[0].end_column, 20)  # 'userss' 길이 6

    def test_validate_multiline_position(self):
        """멀티라인 SQL 위치 정확성"""
        sql = """SELECT *
FROM userss
WHERE id = 1"""
        issues = self.validator.validate(sql)

        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        self.assertEqual(len(table_errors), 1)
        self.assertEqual(table_errors[0].line, 1)  # 두 번째 줄

    # =========================================================================
    # 복합 쿼리 테스트
    # =========================================================================
    def test_validate_complex_query(self):
        """복합 쿼리 검증"""
        sql = """
        SELECT u.id, u.name, o.total_amount
        FROM users u
        LEFT JOIN orders o ON u.id = o.user_id
        WHERE u.email LIKE '%@gmail.com'
        ORDER BY o.created_at DESC
        """
        issues = self.validator.validate(sql)

        # orders.created_at은 없음 (status, id, user_id, total_amount만 있음)
        # 하지만 o.created_at 형태이므로 검증 대상
        column_warnings = [i for i in issues if 'created_at' in i.message and 'orders' in i.message]
        self.assertEqual(len(column_warnings), 1)


class TestSQLAutoCompleter(unittest.TestCase):
    """SQLAutoCompleter 클래스 테스트"""

    def setUp(self):
        """테스트용 AutoCompleter 설정"""
        self.provider = SchemaMetadataProvider()

        metadata = SchemaMetadata()
        metadata.tables = {'users', 'orders', 'products'}
        metadata.columns = {
            'users': {'id', 'name', 'email'},
            'orders': {'id', 'user_id', 'total_amount'},
            'products': {'id', 'name', 'price'},
        }
        self.provider._metadata = metadata

        self.completer = SQLAutoCompleter(self.provider)

    # =========================================================================
    # 컨텍스트별 자동완성 테스트
    # =========================================================================
    def test_autocomplete_after_from(self):
        """[SUCCESS] FROM 뒤 → 테이블 목록"""
        sql = "SELECT * FROM "
        completions = self.completer.get_completions(sql, len(sql))

        # 테이블 타입만 필터
        table_completions = [c for c in completions if c['type'] == 'table']
        table_labels = [c['label'] for c in table_completions]

        self.assertIn('users', table_labels)
        self.assertIn('orders', table_labels)
        self.assertIn('products', table_labels)

    def test_autocomplete_after_join(self):
        """[SUCCESS] JOIN 뒤 → 테이블 목록"""
        sql = "SELECT * FROM users u JOIN "
        completions = self.completer.get_completions(sql, len(sql))

        table_completions = [c for c in completions if c['type'] == 'table']
        self.assertGreater(len(table_completions), 0)

    def test_autocomplete_after_table_dot(self):
        """[SUCCESS] table. 뒤 → 해당 테이블 컬럼만 (키워드/함수 제외)"""
        sql = "SELECT users."
        completions = self.completer.get_completions(sql, len(sql))

        column_completions = [c for c in completions if c['type'] == 'column']
        column_labels = [c['label'] for c in column_completions]

        self.assertIn('id', column_labels)
        self.assertIn('name', column_labels)
        self.assertIn('email', column_labels)
        # 다른 테이블 컬럼은 없어야 함
        self.assertNotIn('total_amount', column_labels)

        # 키워드와 함수는 포함되지 않아야 함 (table. 뒤에서는 컬럼만)
        types = set(c['type'] for c in completions)
        self.assertNotIn('keyword', types, "table. 뒤에서 키워드가 포함되면 안됨")
        self.assertNotIn('function', types, "table. 뒤에서 함수가 포함되면 안됨")

    def test_autocomplete_after_select(self):
        """[SUCCESS] SELECT 뒤 → 컬럼/키워드"""
        sql = "SELECT "
        completions = self.completer.get_completions(sql, len(sql))

        # 키워드와 함수 포함
        types = set(c['type'] for c in completions)
        self.assertIn('keyword', types)
        self.assertIn('function', types)

    def test_autocomplete_keyword_only(self):
        """[SUCCESS] 빈 입력 → 키워드 목록"""
        sql = ""
        completions = self.completer.get_completions(sql, 0)

        keyword_completions = [c for c in completions if c['type'] == 'keyword']
        keyword_labels = [c['label'] for c in keyword_completions]

        self.assertIn('SELECT', keyword_labels)
        self.assertIn('INSERT', keyword_labels)
        self.assertIn('UPDATE', keyword_labels)

    # =========================================================================
    # 접두사 필터링 테스트
    # =========================================================================
    def test_autocomplete_prefix_filter(self):
        """[SUCCESS] 입력 접두사로 필터링"""
        sql = "SELECT * FROM us"
        completions = self.completer.get_completions(sql, len(sql))

        table_completions = [c for c in completions if c['type'] == 'table']
        table_labels = [c['label'] for c in table_completions]

        # 'us'로 시작하는 테이블만
        self.assertIn('users', table_labels)
        self.assertNotIn('orders', table_labels)
        self.assertNotIn('products', table_labels)

    def test_autocomplete_column_prefix_filter(self):
        """[SUCCESS] 컬럼 접두사 필터링"""
        sql = "SELECT users.na"
        completions = self.completer.get_completions(sql, len(sql))

        column_completions = [c for c in completions if c['type'] == 'column']
        column_labels = [c['label'] for c in column_completions]

        self.assertIn('name', column_labels)
        self.assertNotIn('id', column_labels)
        self.assertNotIn('email', column_labels)

    # =========================================================================
    # 함수 자동완성 테스트
    # =========================================================================
    def test_autocomplete_functions(self):
        """[SUCCESS] SQL 함수 자동완성"""
        sql = "SELECT COU"
        completions = self.completer.get_completions(sql, len(sql))

        function_completions = [c for c in completions if c['type'] == 'function']
        function_labels = [c['label'] for c in function_completions]

        # COUNT()가 포함되어야 함
        self.assertTrue(any('COUNT' in label for label in function_labels))


class TestValidationIssue(unittest.TestCase):
    """ValidationIssue 데이터클래스 테스트"""

    def test_issue_length(self):
        """이슈 범위 길이 계산"""
        issue = ValidationIssue(
            line=0,
            column=10,
            end_column=16,
            message="테스트 오류",
            severity=IssueSeverity.ERROR
        )
        self.assertEqual(issue.length, 6)

    def test_issue_with_suggestions(self):
        """제안 포함 이슈"""
        issue = ValidationIssue(
            line=0,
            column=0,
            end_column=5,
            message="테이블 없음",
            severity=IssueSeverity.ERROR,
            suggestions=['users', 'orders']
        )
        self.assertEqual(len(issue.suggestions), 2)
        self.assertIn('users', issue.suggestions)


class TestEdgeCases(unittest.TestCase):
    """엣지 케이스 테스트"""

    def setUp(self):
        self.provider = SchemaMetadataProvider()
        metadata = SchemaMetadata()
        metadata.tables = {'users', 'user_logs', 'user_settings'}
        metadata.columns = {
            'users': {'id', 'name'},
            'user_logs': {'id', 'user_id', 'action'},
            'user_settings': {'id', 'user_id', 'key', 'value'},
        }
        self.provider._metadata = metadata
        self.validator = SQLValidator(self.provider)

    def test_empty_sql(self):
        """빈 SQL"""
        issues = self.validator.validate("")
        self.assertEqual(len(issues), 0)

    def test_whitespace_only(self):
        """공백만 있는 SQL"""
        issues = self.validator.validate("   \n\t  ")
        self.assertEqual(len(issues), 0)

    def test_comment_only(self):
        """주석만 있는 SQL"""
        issues = self.validator.validate("-- SELECT * FROM nonexistent")
        self.assertEqual(len(issues), 0)

    def test_backtick_table_name(self):
        """백틱으로 감싼 테이블명"""
        issues = self.validator.validate("SELECT * FROM `users`")
        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        self.assertEqual(len(table_errors), 0)

    def test_schema_qualified_table(self):
        """스키마.테이블 형식"""
        # 스키마 부분은 현재 검증하지 않음, 테이블명만 검증
        issues = self.validator.validate("SELECT * FROM mydb.users")
        table_errors = [i for i in issues if i.severity == IssueSeverity.ERROR]
        self.assertEqual(len(table_errors), 0)

    def test_escaped_quotes_in_string(self):
        """이스케이프된 따옴표 처리"""
        sql = "SELECT * FROM users WHERE name = 'O''Brien'"
        issues = self.validator.validate(sql)
        # 문자열 처리가 올바르게 되어야 함
        self.assertEqual(len([i for i in issues if 'Brien' in i.message]), 0)

    def test_double_quoted_string(self):
        """쌍따옴표 문자열"""
        sql = 'SELECT * FROM users WHERE name = "FROM nonexistent"'
        issues = self.validator.validate(sql)
        # 문자열 내부는 무시
        self.assertEqual(len([i for i in issues if 'nonexistent' in i.message]), 0)


if __name__ == '__main__':
    # 테스트 실행
    unittest.main(verbosity=2)
