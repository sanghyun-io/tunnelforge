"""
SchemaExtractor, SchemaComparator, SyncScriptGenerator 단위 테스트
"""
import pytest
from unittest.mock import MagicMock


# =====================================================================
# 데이터클래스 테스트
# =====================================================================

class TestColumnInfo:
    """ColumnInfo 데이터클래스 테스트"""

    def test_to_sql_definition_basic(self):
        """기본 컬럼 SQL 정의 생성"""
        from src.core.schema_diff import ColumnInfo

        col = ColumnInfo(
            name='id',
            data_type='int',
            nullable=False,
            default=None,
            extra='AUTO_INCREMENT',
            key='PRI'
        )
        sql = col.to_sql_definition()
        assert '`id`' in sql
        assert 'int' in sql
        assert 'NOT NULL' in sql
        assert 'AUTO_INCREMENT' in sql

    def test_to_sql_definition_nullable_with_default(self):
        """NULL 허용, 기본값 있는 컬럼 SQL 정의"""
        from src.core.schema_diff import ColumnInfo

        col = ColumnInfo(
            name='status',
            data_type='varchar(50)',
            nullable=True,
            default='active'
        )
        sql = col.to_sql_definition()
        assert 'NULL' in sql
        assert "DEFAULT 'active'" in sql

    def test_to_sql_definition_current_timestamp_default(self):
        """CURRENT_TIMESTAMP 기본값 처리"""
        from src.core.schema_diff import ColumnInfo

        col = ColumnInfo(
            name='created_at',
            data_type='datetime',
            nullable=False,
            default='CURRENT_TIMESTAMP'
        )
        sql = col.to_sql_definition()
        assert 'DEFAULT CURRENT_TIMESTAMP' in sql
        # 따옴표 없이 출력되어야 함
        assert "DEFAULT 'CURRENT_TIMESTAMP'" not in sql

    def test_to_sql_definition_with_charset(self):
        """charset 포함 컬럼 SQL 정의"""
        from src.core.schema_diff import ColumnInfo

        col = ColumnInfo(
            name='name',
            data_type='varchar(255)',
            nullable=True,
            default=None,
            charset='utf8mb4'
        )
        sql = col.to_sql_definition()
        assert 'CHARACTER SET utf8mb4' in sql


class TestIndexInfo:
    """IndexInfo 데이터클래스 테스트"""

    def test_to_sql_definition_primary_key(self):
        """Primary Key SQL 정의"""
        from src.core.schema_diff import IndexInfo

        idx = IndexInfo(name='PRIMARY', columns=['id'], unique=True)
        sql = idx.to_sql_definition('users')
        assert 'PRIMARY KEY' in sql
        assert '`id`' in sql

    def test_to_sql_definition_unique_index(self):
        """UNIQUE 인덱스 SQL 정의"""
        from src.core.schema_diff import IndexInfo

        idx = IndexInfo(name='uniq_email', columns=['email'], unique=True, type='BTREE')
        sql = idx.to_sql_definition('users')
        assert 'UNIQUE INDEX' in sql
        assert '`uniq_email`' in sql

    def test_to_sql_definition_regular_index(self):
        """일반 인덱스 SQL 정의"""
        from src.core.schema_diff import IndexInfo

        idx = IndexInfo(name='idx_name', columns=['first_name', 'last_name'], unique=False)
        sql = idx.to_sql_definition('users')
        assert 'INDEX' in sql
        assert '`first_name`' in sql
        assert '`last_name`' in sql


class TestForeignKeyInfo:
    """ForeignKeyInfo 데이터클래스 테스트"""

    def test_to_sql_definition(self):
        """FK SQL 정의 생성"""
        from src.core.schema_diff import ForeignKeyInfo

        fk = ForeignKeyInfo(
            name='fk_user_id',
            columns=['user_id'],
            ref_table='users',
            ref_columns=['id'],
            on_delete='CASCADE',
            on_update='RESTRICT'
        )
        sql = fk.to_sql_definition()
        assert 'CONSTRAINT `fk_user_id`' in sql
        assert 'FOREIGN KEY' in sql
        assert 'REFERENCES `users`' in sql
        assert 'ON DELETE CASCADE' in sql
        assert 'ON UPDATE RESTRICT' in sql


class TestTableSchema:
    """TableSchema 데이터클래스 테스트"""

    def test_get_column_found(self):
        """이름으로 컬럼 조회 성공"""
        from src.core.schema_diff import TableSchema, ColumnInfo

        table = TableSchema(name='users')
        col = ColumnInfo(name='email', data_type='varchar(255)', nullable=False, default=None)
        table.columns.append(col)

        found = table.get_column('email')
        assert found is col

    def test_get_column_case_insensitive(self):
        """대소문자 무시 컬럼 조회"""
        from src.core.schema_diff import TableSchema, ColumnInfo

        table = TableSchema(name='users')
        col = ColumnInfo(name='Email', data_type='varchar(255)', nullable=False, default=None)
        table.columns.append(col)

        assert table.get_column('email') is col
        assert table.get_column('EMAIL') is col

    def test_get_column_not_found(self):
        """존재하지 않는 컬럼 조회 시 None"""
        from src.core.schema_diff import TableSchema

        table = TableSchema(name='users')
        assert table.get_column('nonexistent') is None

    def test_get_index_found(self):
        """이름으로 인덱스 조회 성공"""
        from src.core.schema_diff import TableSchema, IndexInfo

        table = TableSchema(name='users')
        idx = IndexInfo(name='idx_name', columns=['name'], unique=False)
        table.indexes.append(idx)

        assert table.get_index('idx_name') is idx

    def test_get_foreign_key_found(self):
        """이름으로 FK 조회 성공"""
        from src.core.schema_diff import TableSchema, ForeignKeyInfo

        table = TableSchema(name='orders')
        fk = ForeignKeyInfo(
            name='fk_user', columns=['user_id'],
            ref_table='users', ref_columns=['id']
        )
        table.foreign_keys.append(fk)

        assert table.get_foreign_key('fk_user') is fk


class TestTableDiff:
    """TableDiff 데이터클래스 테스트"""

    def test_has_differences_added(self):
        """ADDED 타입은 차이 있음"""
        from src.core.schema_diff import TableDiff, DiffType

        diff = TableDiff(table_name='new_table', diff_type=DiffType.ADDED)
        assert diff.has_differences() is True

    def test_has_differences_removed(self):
        """REMOVED 타입은 차이 있음"""
        from src.core.schema_diff import TableDiff, DiffType

        diff = TableDiff(table_name='old_table', diff_type=DiffType.REMOVED)
        assert diff.has_differences() is True

    def test_has_differences_unchanged_no_sub_diffs(self):
        """UNCHANGED 타입, 하위 차이 없으면 False"""
        from src.core.schema_diff import TableDiff, DiffType

        diff = TableDiff(table_name='stable_table', diff_type=DiffType.UNCHANGED)
        assert diff.has_differences() is False


# =====================================================================
# SchemaExtractor 테스트
# =====================================================================

class TestSchemaExtractor:
    """SchemaExtractor 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from src.core.schema_diff import SchemaExtractor

        self.mock_connector = MagicMock()
        self.extractor = SchemaExtractor(self.mock_connector)

    def test_extract_table_schema_success(self):
        """테이블 스키마 추출 성공"""
        from src.core.schema_diff import SchemaExtractor

        # _get_columns, _get_indexes, _get_foreign_keys, _get_table_options, _get_row_count 모킹
        self.extractor._get_columns = MagicMock(return_value=[])
        self.extractor._get_indexes = MagicMock(return_value=[])
        self.extractor._get_foreign_keys = MagicMock(return_value=[])
        self.extractor._get_table_options = MagicMock(return_value=('InnoDB', 'utf8mb4', 'utf8mb4_general_ci'))
        self.extractor._get_row_count = MagicMock(return_value=100)

        result = self.extractor.extract_table_schema('mydb', 'users')

        assert result is not None
        assert result.name == 'users'
        assert result.engine == 'InnoDB'
        assert result.row_count == 100

    def test_extract_table_schema_exception_returns_none(self):
        """추출 중 예외 발생 시 None 반환"""
        self.extractor._get_columns = MagicMock(side_effect=Exception("DB error"))

        result = self.extractor.extract_table_schema('mydb', 'broken_table')
        assert result is None

    def test_extract_all_tables(self):
        """전체 테이블 스키마 추출"""
        self.mock_connector.execute.return_value = [
            {'TABLE_NAME': 'users'},
            {'TABLE_NAME': 'orders'},
        ]

        # extract_table_schema를 모킹
        mock_schema = MagicMock()
        self.extractor.extract_table_schema = MagicMock(return_value=mock_schema)

        result = self.extractor.extract_all_tables('mydb')

        assert 'users' in result
        assert 'orders' in result
        assert self.extractor.extract_table_schema.call_count == 2

    def test_extract_all_tables_skips_failed(self):
        """extract_table_schema가 None 반환 시 스킵"""
        self.mock_connector.execute.return_value = [
            {'TABLE_NAME': 'users'},
            {'TABLE_NAME': 'broken'},
        ]

        def side_effect(schema, table):
            if table == 'broken':
                return None
            return MagicMock()

        self.extractor.extract_table_schema = MagicMock(side_effect=side_effect)

        result = self.extractor.extract_all_tables('mydb')

        assert 'users' in result
        assert 'broken' not in result

    def test_get_columns_parses_result(self):
        """컬럼 정보 파싱 확인"""
        self.mock_connector.execute.return_value = [
            {
                'COLUMN_NAME': 'id',
                'COLUMN_TYPE': 'int',
                'IS_NULLABLE': 'NO',
                'COLUMN_DEFAULT': None,
                'EXTRA': 'auto_increment',
                'COLUMN_KEY': 'PRI',
                'CHARACTER_SET_NAME': None,
                'COLLATION_NAME': None
            }
        ]

        columns = self.extractor._get_columns('mydb', 'users')

        assert len(columns) == 1
        assert columns[0].name == 'id'
        assert columns[0].nullable is False
        assert columns[0].extra == 'auto_increment'

    def test_get_indexes_parses_result(self):
        """인덱스 정보 파싱 확인 (멀티 컬럼 인덱스 포함)"""
        self.mock_connector.execute.return_value = [
            {'INDEX_NAME': 'idx_name', 'COLUMN_NAME': 'first_name', 'NON_UNIQUE': 1, 'INDEX_TYPE': 'BTREE'},
            {'INDEX_NAME': 'idx_name', 'COLUMN_NAME': 'last_name', 'NON_UNIQUE': 1, 'INDEX_TYPE': 'BTREE'},
            {'INDEX_NAME': 'PRIMARY', 'COLUMN_NAME': 'id', 'NON_UNIQUE': 0, 'INDEX_TYPE': 'BTREE'},
        ]

        indexes = self.extractor._get_indexes('mydb', 'users')

        assert len(indexes) == 2
        idx_map = {i.name: i for i in indexes}
        assert 'idx_name' in idx_map
        assert 'first_name' in idx_map['idx_name'].columns
        assert 'last_name' in idx_map['idx_name'].columns

    def test_get_foreign_keys_parses_result(self):
        """FK 정보 파싱 확인"""
        self.mock_connector.execute.return_value = [
            {
                'CONSTRAINT_NAME': 'fk_user',
                'COLUMN_NAME': 'user_id',
                'REFERENCED_TABLE_NAME': 'users',
                'REFERENCED_COLUMN_NAME': 'id',
                'DELETE_RULE': 'CASCADE',
                'UPDATE_RULE': 'RESTRICT'
            }
        ]

        fks = self.extractor._get_foreign_keys('mydb', 'orders')

        assert len(fks) == 1
        assert fks[0].name == 'fk_user'
        assert fks[0].ref_table == 'users'
        assert fks[0].on_delete == 'CASCADE'

    def test_get_table_options_success(self):
        """테이블 옵션 조회 성공"""
        self.mock_connector.execute.return_value = [
            {'ENGINE': 'InnoDB', 'TABLE_COLLATION': 'utf8mb4_unicode_ci'}
        ]

        engine, charset, collation = self.extractor._get_table_options('mydb', 'users')

        assert engine == 'InnoDB'
        assert charset == 'utf8mb4'
        assert collation == 'utf8mb4_unicode_ci'

    def test_get_table_options_defaults_on_failure(self):
        """테이블 옵션 조회 실패 시 기본값 반환"""
        self.mock_connector.execute.side_effect = Exception("DB error")

        engine, charset, collation = self.extractor._get_table_options('mydb', 'users')

        assert engine == 'InnoDB'
        assert charset == 'utf8mb4'

    def test_get_row_count_success(self):
        """행 수 조회 성공"""
        self.mock_connector.execute.return_value = [{'cnt': 42}]

        count = self.extractor._get_row_count('mydb', 'users')
        assert count == 42

    def test_get_row_count_exception_returns_zero(self):
        """행 수 조회 실패 시 0 반환"""
        self.mock_connector.execute.side_effect = Exception("Table not found")

        count = self.extractor._get_row_count('mydb', 'missing_table')
        assert count == 0


# =====================================================================
# SchemaComparator 테스트
# =====================================================================

class TestSchemaComparator:
    """SchemaComparator 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self):
        from src.core.schema_diff import SchemaComparator, TableSchema, ColumnInfo

        self.comparator = SchemaComparator()

        # 기본 소스/타겟 스키마 생성
        self.source = TableSchema(name='users')
        self.target = TableSchema(name='users')

        self.col_id = ColumnInfo(name='id', data_type='int', nullable=False, default=None, extra='auto_increment')
        self.col_name = ColumnInfo(name='name', data_type='varchar(255)', nullable=True, default=None)
        self.col_email = ColumnInfo(name='email', data_type='varchar(255)', nullable=False, default=None)

    def test_compare_tables_identical(self):
        """동일한 테이블 비교 시 UNCHANGED"""
        from src.core.schema_diff import DiffType

        self.source.columns = [self.col_id, self.col_name]
        self.target.columns = [self.col_id, self.col_name]

        diff = self.comparator.compare_tables(self.source, self.target)

        assert diff.table_name == 'users'
        assert diff.has_differences() is False

    def test_compare_tables_column_in_source_only(self):
        """소스에만 있는 컬럼은 ADDED (타겟에 추가 필요)"""
        from src.core.schema_diff import DiffType

        # source: id, name, email / target: id, name
        # email은 소스에만 있음 → ADDED (타겟에 추가 필요)
        self.source.columns = [self.col_id, self.col_name, self.col_email]
        self.target.columns = [self.col_id, self.col_name]

        diff = self.comparator.compare_tables(self.source, self.target)

        added = [d for d in diff.column_diffs if d.diff_type == DiffType.ADDED]
        assert len(added) == 1
        assert added[0].column_name == 'email'

    def test_compare_tables_column_in_target_only(self):
        """타겟에만 있는 컬럼은 REMOVED (타겟에서 제거 필요)"""
        from src.core.schema_diff import DiffType

        # source: id / target: id, name
        # name은 타겟에만 있음 → REMOVED
        self.source.columns = [self.col_id]
        self.target.columns = [self.col_id, self.col_name]

        diff = self.comparator.compare_tables(self.source, self.target)

        removed = [d for d in diff.column_diffs if d.diff_type == DiffType.REMOVED]
        assert len(removed) == 1
        assert removed[0].column_name == 'name'

    def test_compare_schemas_all_added(self):
        """소스에 있고 타겟에 없는 테이블 - ADDED"""
        from src.core.schema_diff import DiffType, TableSchema

        source_tables = {
            'users': TableSchema(name='users'),
            'orders': TableSchema(name='orders'),
        }
        target_tables = {}

        diffs = self.comparator.compare_schemas(source_tables, target_tables)

        assert len(diffs) == 2
        assert all(d.diff_type == DiffType.ADDED for d in diffs)

    def test_compare_schemas_some_removed(self):
        """타겟에만 있고 소스에 없는 테이블 - REMOVED"""
        from src.core.schema_diff import DiffType, TableSchema

        source_tables = {}
        target_tables = {
            'old_table': TableSchema(name='old_table'),
        }

        diffs = self.comparator.compare_schemas(source_tables, target_tables)

        assert len(diffs) == 1
        assert diffs[0].diff_type == DiffType.REMOVED


# =====================================================================
# SeveritySummary 테스트
# =====================================================================

class TestSeveritySummary:
    """SeveritySummary 데이터클래스 테스트"""

    def test_has_critical_true(self):
        """critical > 0 이면 True"""
        from src.core.schema_diff import SeveritySummary

        summary = SeveritySummary(critical=1, warning=0, info=0)
        assert summary.has_critical is True

    def test_has_critical_false(self):
        """critical == 0 이면 False"""
        from src.core.schema_diff import SeveritySummary

        summary = SeveritySummary(critical=0, warning=5, info=3)
        assert summary.has_critical is False
