"""
migration_parsers.py 단위 테스트

CreateTableParser, CreateUserParser, GrantParser, SQLParser 검증.
순수 정규식/문자열 파싱이므로 DB 의존성 없음.
"""
import pytest

from src.core.migration_parsers import (
    ParsedColumn,
    ParsedIndex,
    ParsedForeignKey,
    ParsedPartition,
    ParsedTable,
    ParsedUser,
    ParsedGrant,
    CreateTableParser,
    CreateUserParser,
    GrantParser,
    SQLParser,
)


# ============================================================
# ParsedColumn 데이터클래스 테스트
# ============================================================
class TestParsedColumn:
    def test_full_type_basic(self):
        col = ParsedColumn(name="id", data_type="INT")
        assert col.full_type == "INT"

    def test_full_type_with_params(self):
        col = ParsedColumn(name="name", data_type="VARCHAR", type_params="255")
        assert col.full_type == "VARCHAR(255)"

    def test_full_type_unsigned(self):
        col = ParsedColumn(name="age", data_type="INT", unsigned=True)
        assert col.full_type == "INT UNSIGNED"

    def test_full_type_zerofill(self):
        col = ParsedColumn(name="code", data_type="INT", type_params="5", zerofill=True)
        assert col.full_type == "INT(5) ZEROFILL"

    def test_full_type_unsigned_zerofill(self):
        col = ParsedColumn(name="x", data_type="INT", type_params="10", unsigned=True, zerofill=True)
        assert col.full_type == "INT(10) UNSIGNED ZEROFILL"

    def test_defaults(self):
        col = ParsedColumn(name="x", data_type="INT")
        assert col.nullable is True
        assert col.default is None
        assert col.extra is None
        assert col.charset is None
        assert col.collation is None
        assert col.comment is None
        assert col.generated is None
        assert col.unsigned is False
        assert col.zerofill is False


# ============================================================
# ParsedIndex 데이터클래스 테스트
# ============================================================
class TestParsedIndex:
    def test_covers_columns_exact(self):
        idx = ParsedIndex(name="idx1", columns=["a", "b", "c"])
        assert idx.covers_columns(["a", "b", "c"]) is True

    def test_covers_columns_prefix(self):
        idx = ParsedIndex(name="idx1", columns=["a", "b", "c"])
        assert idx.covers_columns(["a", "b"]) is True

    def test_covers_columns_single(self):
        idx = ParsedIndex(name="idx1", columns=["a", "b"])
        assert idx.covers_columns(["a"]) is True

    def test_covers_columns_wrong_order(self):
        idx = ParsedIndex(name="idx1", columns=["a", "b"])
        assert idx.covers_columns(["b", "a"]) is False

    def test_covers_columns_case_insensitive(self):
        idx = ParsedIndex(name="idx1", columns=["Name", "Email"])
        assert idx.covers_columns(["name", "email"]) is True

    def test_covers_columns_empty(self):
        idx = ParsedIndex(name="idx1", columns=["a"])
        assert idx.covers_columns([]) is True

    def test_defaults(self):
        idx = ParsedIndex(name="pk", columns=["id"])
        assert idx.is_primary is False
        assert idx.is_unique is False
        assert idx.is_fulltext is False
        assert idx.is_spatial is False
        assert idx.index_type is None


# ============================================================
# ParsedTable 데이터클래스 테스트
# ============================================================
class TestParsedTable:
    def test_get_column_found(self):
        col = ParsedColumn(name="email", data_type="VARCHAR")
        table = ParsedTable(name="users", columns=[col])
        assert table.get_column("email") is col

    def test_get_column_case_insensitive(self):
        col = ParsedColumn(name="Email", data_type="VARCHAR")
        table = ParsedTable(name="users", columns=[col])
        assert table.get_column("email") is col

    def test_get_column_not_found(self):
        table = ParsedTable(name="users", columns=[])
        assert table.get_column("missing") is None

    def test_get_primary_key_found(self):
        pk = ParsedIndex(name="PRIMARY", columns=["id"], is_primary=True)
        idx = ParsedIndex(name="idx1", columns=["name"])
        table = ParsedTable(name="users", indexes=[pk, idx])
        assert table.get_primary_key() is pk

    def test_get_primary_key_not_found(self):
        idx = ParsedIndex(name="idx1", columns=["name"])
        table = ParsedTable(name="users", indexes=[idx])
        assert table.get_primary_key() is None

    def test_get_unique_indexes(self):
        pk = ParsedIndex(name="PRIMARY", columns=["id"], is_primary=True, is_unique=True)
        uniq = ParsedIndex(name="uniq_email", columns=["email"], is_unique=True)
        normal = ParsedIndex(name="idx1", columns=["name"])
        table = ParsedTable(name="users", indexes=[pk, uniq, normal])
        result = table.get_unique_indexes()
        assert len(result) == 2
        assert pk in result
        assert uniq in result

    def test_get_unique_indexes_empty(self):
        table = ParsedTable(name="users", indexes=[])
        assert table.get_unique_indexes() == []


# ============================================================
# CreateTableParser 테스트
# ============================================================
class TestCreateTableParser:
    @pytest.fixture
    def parser(self):
        return CreateTableParser()

    # --- 테이블명 추출 ---
    def test_parse_simple_table(self, parser):
        sql = "CREATE TABLE `users` (\n  `id` INT NOT NULL\n) ENGINE=InnoDB;"
        result = parser.parse(sql)
        assert result is not None
        assert result.name == "users"
        assert result.schema is None

    def test_parse_with_schema(self, parser):
        sql = "CREATE TABLE `mydb`.`users` (\n  `id` INT\n);"
        result = parser.parse(sql)
        assert result.name == "users"
        assert result.schema == "mydb"

    def test_parse_without_backticks(self, parser):
        sql = "CREATE TABLE users (\n  `id` INT\n);"
        result = parser.parse(sql)
        assert result.name == "users"

    def test_parse_if_not_exists(self, parser):
        sql = "CREATE TABLE IF NOT EXISTS `users` (\n  `id` INT\n);"
        result = parser.parse(sql)
        assert result.name == "users"

    def test_parse_temporary_table(self, parser):
        sql = "CREATE TEMPORARY TABLE `tmp` (\n  `id` INT\n);"
        result = parser.parse(sql)
        assert result.name == "tmp"

    def test_parse_invalid_sql(self, parser):
        result = parser.parse("SELECT * FROM users")
        assert result is None

    def test_parse_empty_string(self, parser):
        result = parser.parse("")
        assert result is None

    # --- 컬럼 파싱 ---
    def test_parse_column_basic(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT NOT NULL AUTO_INCREMENT,\n  `name` VARCHAR(255) DEFAULT 'test'\n);"
        result = parser.parse(sql)
        assert len(result.columns) == 2

        id_col = result.columns[0]
        assert id_col.name == "id"
        assert id_col.data_type == "INT"
        assert id_col.nullable is False
        assert id_col.extra == "AUTO_INCREMENT"

        name_col = result.columns[1]
        assert name_col.name == "name"
        assert name_col.data_type == "VARCHAR"
        assert name_col.type_params == "255"
        assert name_col.default == "'test'"

    def test_parse_column_unsigned_zerofill(self, parser):
        sql = "CREATE TABLE `t` (\n  `code` INT(5) UNSIGNED ZEROFILL\n);"
        result = parser.parse(sql)
        col = result.columns[0]
        assert col.unsigned is True
        assert col.zerofill is True
        assert col.type_params == "5"

    def test_parse_column_charset_collation(self, parser):
        sql = "CREATE TABLE `t` (\n  `name` VARCHAR(100) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci\n);"
        result = parser.parse(sql)
        col = result.columns[0]
        assert col.charset == "utf8mb4"
        assert col.collation == "utf8mb4_unicode_ci"

    def test_parse_column_comment(self, parser):
        sql = "CREATE TABLE `t` (\n  `name` VARCHAR(100) COMMENT 'user name'\n);"
        result = parser.parse(sql)
        col = result.columns[0]
        assert col.comment == "user name"

    def test_parse_column_generated(self, parser):
        # 단순 표현식 (중첩 괄호 없음 — 파서가 [^)]+ 패턴 사용)
        sql = "CREATE TABLE `t` (\n  `doubled` INT GENERATED ALWAYS AS (col1 + col2) STORED\n);"
        result = parser.parse(sql)
        col = result.columns[0]
        assert col.generated is not None
        expr, is_stored = col.generated
        assert "col1 + col2" in expr
        assert is_stored is True

    def test_parse_column_generated_virtual(self, parser):
        sql = "CREATE TABLE `t` (\n  `hash_val` BIGINT GENERATED ALWAYS AS (col1 * 2) VIRTUAL\n);"
        result = parser.parse(sql)
        col = result.columns[0]
        assert col.generated is not None
        _, is_stored = col.generated
        assert is_stored is False

    def test_parse_column_nullable_explicit(self, parser):
        sql = "CREATE TABLE `t` (\n  `optional` VARCHAR(50) NULL\n);"
        result = parser.parse(sql)
        assert result.columns[0].nullable is True

    def test_parse_decimal_params(self, parser):
        sql = "CREATE TABLE `t` (\n  `price` DECIMAL(10,2) NOT NULL\n);"
        result = parser.parse(sql)
        col = result.columns[0]
        assert col.data_type == "DECIMAL"
        assert col.type_params == "10,2"

    # --- PRIMARY KEY 파싱 ---
    def test_parse_primary_key(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT,\n  PRIMARY KEY (`id`)\n);"
        result = parser.parse(sql)
        pk = result.get_primary_key()
        assert pk is not None
        assert pk.columns == ["id"]
        assert pk.is_primary is True
        assert pk.is_unique is True

    def test_parse_composite_primary_key(self, parser):
        sql = "CREATE TABLE `t` (\n  `a` INT,\n  `b` INT,\n  PRIMARY KEY (`a`, `b`)\n);"
        result = parser.parse(sql)
        pk = result.get_primary_key()
        assert pk.columns == ["a", "b"]

    # --- 인덱스 파싱 ---
    def test_parse_unique_index(self, parser):
        sql = "CREATE TABLE `t` (\n  `email` VARCHAR(255),\n  UNIQUE KEY `idx_email` (`email`)\n);"
        result = parser.parse(sql)
        # UNIQUE KEY는 INDEX_PATTERN으로 파싱됨
        unique_idxs = [i for i in result.indexes if i.is_unique and not i.is_primary]
        assert len(unique_idxs) >= 1
        assert unique_idxs[0].name == "idx_email"
        assert unique_idxs[0].columns == ["email"]

    def test_parse_fulltext_index(self, parser):
        sql = "CREATE TABLE `t` (\n  `content` TEXT,\n  FULLTEXT KEY `ft_content` (`content`)\n);"
        result = parser.parse(sql)
        ft = [i for i in result.indexes if i.is_fulltext]
        assert len(ft) == 1
        assert ft[0].name == "ft_content"

    def test_parse_spatial_index(self, parser):
        sql = "CREATE TABLE `t` (\n  `geo` GEOMETRY,\n  SPATIAL KEY `sp_geo` (`geo`)\n);"
        result = parser.parse(sql)
        sp = [i for i in result.indexes if i.is_spatial]
        assert len(sp) == 1
        assert sp[0].name == "sp_geo"

    def test_parse_index_with_prefix_length(self, parser):
        # _parse_index_columns에서 prefix 길이 파싱 확인 (별도 호출로 검증)
        columns, prefix_lengths = parser._parse_index_columns("`data`(100)")
        assert columns == ["data"]
        assert prefix_lengths == [100]

    def test_parse_index_prefix_via_method(self, parser):
        # INDEX_PATTERN은 `data`(100) 전체를 group(3)에 포함하고
        # _parse_index_columns에서 파싱하여 prefix_lengths에 설정함
        cols, prefixes = parser._parse_index_columns("col1(50), col2")
        assert cols == ["col1", "col2"]
        assert prefixes == [50, None]

    def test_parse_composite_index(self, parser):
        sql = "CREATE TABLE `t` (\n  `a` INT,\n  `b` INT,\n  KEY `idx_ab` (`a`, `b`)\n);"
        result = parser.parse(sql)
        indexes = [i for i in result.indexes if not i.is_primary]
        assert len(indexes) >= 1
        assert indexes[0].columns == ["a", "b"]

    # --- FK 파싱 ---
    def test_parse_foreign_key(self, parser):
        sql = """CREATE TABLE `orders` (
  `id` INT,
  `user_id` INT,
  CONSTRAINT `fk_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
);"""
        result = parser.parse(sql)
        assert len(result.foreign_keys) == 1
        fk = result.foreign_keys[0]
        assert fk.name == "fk_user"
        assert fk.columns == ["user_id"]
        assert fk.ref_table == "users"
        assert fk.ref_columns == ["id"]
        assert fk.on_delete == "RESTRICT"
        assert fk.on_update == "RESTRICT"

    def test_parse_foreign_key_with_actions(self, parser):
        sql = """CREATE TABLE `orders` (
  `id` INT,
  `user_id` INT,
  CONSTRAINT `fk_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE ON UPDATE SET NULL
);"""
        result = parser.parse(sql)
        fk = result.foreign_keys[0]
        assert fk.on_delete == "CASCADE"
        assert fk.on_update == "SET_NULL"

    def test_parse_foreign_key_cross_schema(self, parser):
        sql = """CREATE TABLE `orders` (
  `id` INT,
  `user_id` INT,
  CONSTRAINT `fk_user` FOREIGN KEY (`user_id`) REFERENCES `other_db`.`users` (`id`)
);"""
        result = parser.parse(sql)
        fk = result.foreign_keys[0]
        assert fk.ref_schema == "other_db"
        assert fk.ref_table == "users"

    def test_parse_simple_foreign_key(self, parser):
        """CONSTRAINT 없는 FK"""
        sql = """CREATE TABLE `orders` (
  `id` INT,
  `user_id` INT,
  FOREIGN KEY (`user_id`) REFERENCES `users` (`id`)
);"""
        result = parser.parse(sql)
        assert len(result.foreign_keys) >= 1
        fk = result.foreign_keys[0]
        assert fk.columns == ["user_id"]
        assert fk.ref_table == "users"

    def test_parse_composite_foreign_key(self, parser):
        sql = """CREATE TABLE `t` (
  `a` INT,
  `b` INT,
  CONSTRAINT `fk_ab` FOREIGN KEY (`a`, `b`) REFERENCES `ref_t` (`x`, `y`)
);"""
        result = parser.parse(sql)
        fk = result.foreign_keys[0]
        assert fk.columns == ["a", "b"]
        assert fk.ref_columns == ["x", "y"]

    def test_parse_no_duplicate_fk(self, parser):
        """CONSTRAINT FK와 simple FK가 동일하면 중복 제거"""
        sql = """CREATE TABLE `t` (
  `id` INT,
  `uid` INT,
  CONSTRAINT `fk1` FOREIGN KEY (`uid`) REFERENCES `users` (`id`)
);"""
        result = parser.parse(sql)
        # CONSTRAINT 패턴으로 1개 파싱, simple 패턴도 매칭하지만 중복 제거
        assert len(result.foreign_keys) == 1

    # --- 테이블 옵션 파싱 ---
    def test_parse_engine(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) ENGINE=InnoDB;"
        result = parser.parse(sql)
        assert result.engine == "InnoDB"

    def test_parse_charset(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) DEFAULT CHARSET=utf8mb4;"
        result = parser.parse(sql)
        assert result.charset == "utf8mb4"

    def test_parse_charset_without_default(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) CHARSET=utf8;"
        result = parser.parse(sql)
        assert result.charset == "utf8"

    def test_parse_character_set(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) CHARACTER SET=latin1;"
        result = parser.parse(sql)
        assert result.charset == "latin1"

    def test_parse_collation(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) COLLATE=utf8mb4_general_ci;"
        result = parser.parse(sql)
        assert result.collation == "utf8mb4_general_ci"

    def test_parse_row_format(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) ROW_FORMAT=DYNAMIC;"
        result = parser.parse(sql)
        assert result.row_format == "DYNAMIC"

    def test_parse_tablespace(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) TABLESPACE=`ts1`;"
        result = parser.parse(sql)
        assert result.tablespace == "ts1"

    def test_parse_table_comment(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) COMMENT='user table';"
        result = parser.parse(sql)
        assert result.comment == "user table"

    def test_parse_multiple_options(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n) ENGINE=MyISAM DEFAULT CHARSET=utf8 COLLATE=utf8_general_ci;"
        result = parser.parse(sql)
        assert result.engine == "MyISAM"
        assert result.charset == "utf8"
        assert result.collation == "utf8_general_ci"

    # --- _extract_body ---
    def test_extract_body_no_parens(self, parser):
        assert parser._extract_body("CREATE TABLE t") is None

    def test_extract_body_nested_parens(self, parser):
        sql = "CREATE TABLE t (id INT, price DECIMAL(10,2), PRIMARY KEY (id))"
        body = parser._extract_body(sql)
        assert "DECIMAL(10,2)" in body
        assert "PRIMARY KEY" in body

    # --- _split_definitions ---
    def test_split_definitions_basic(self, parser):
        body = "`id` INT, `name` VARCHAR(255), PRIMARY KEY (`id`)"
        defs = parser._split_definitions(body)
        assert len(defs) == 3

    def test_split_definitions_with_nested_parens(self, parser):
        body = "`id` INT, `price` DECIMAL(10,2), `data` ENUM('a','b','c')"
        defs = parser._split_definitions(body)
        assert len(defs) == 3
        assert "DECIMAL(10,2)" in defs[1]
        assert "ENUM('a','b','c')" in defs[2]

    def test_split_definitions_empty(self, parser):
        defs = parser._split_definitions("")
        assert defs == []

    # --- _parse_index_columns ---
    def test_parse_index_columns_simple(self, parser):
        cols, prefixes = parser._parse_index_columns("`col1`, `col2`")
        assert cols == ["col1", "col2"]
        assert prefixes == [None, None]

    def test_parse_index_columns_with_prefix(self, parser):
        cols, prefixes = parser._parse_index_columns("`data`(100), `name`")
        assert cols == ["data", "name"]
        assert prefixes == [100, None]

    def test_parse_index_columns_with_asc_desc(self, parser):
        cols, _ = parser._parse_index_columns("`a` ASC, `b` DESC")
        assert cols == ["a", "b"]

    # --- 종합 파싱 ---
    def test_parse_complex_table(self, parser):
        sql = """CREATE TABLE IF NOT EXISTS `mydb`.`orders` (
  `id` BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_id` INT NOT NULL,
  `total` DECIMAL(10,2) NOT NULL DEFAULT '0.00',
  `status` ENUM('pending','shipped','done') DEFAULT 'pending',
  `created_at` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `note` TEXT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_unique_user_status` (`user_id`, `status`),
  KEY `idx_created` (`created_at`),
  CONSTRAINT `fk_orders_user` FOREIGN KEY (`user_id`) REFERENCES `users` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci ROW_FORMAT=DYNAMIC COMMENT='order table';"""
        result = parser.parse(sql)

        # 테이블 정보
        assert result.name == "orders"
        assert result.schema == "mydb"
        assert result.engine == "InnoDB"
        assert result.charset == "utf8mb4"
        assert result.collation == "utf8mb4_0900_ai_ci"
        assert result.row_format == "DYNAMIC"
        assert result.comment == "order table"

        # 컬럼
        assert len(result.columns) >= 5
        id_col = result.get_column("id")
        assert id_col is not None
        assert id_col.unsigned is True
        assert id_col.nullable is False
        assert id_col.extra == "AUTO_INCREMENT"

        total_col = result.get_column("total")
        assert total_col.type_params == "10,2"

        note_col = result.get_column("note")
        assert note_col.charset == "utf8mb4"

        # 인덱스
        pk = result.get_primary_key()
        assert pk is not None
        assert pk.columns == ["id"]

        uniq = result.get_unique_indexes()
        assert any(i.name == "idx_unique_user_status" for i in uniq)

        # FK
        assert len(result.foreign_keys) == 1
        fk = result.foreign_keys[0]
        assert fk.name == "fk_orders_user"
        assert fk.on_delete == "CASCADE"


# ============================================================
# CreateUserParser 테스트
# ============================================================
class TestCreateUserParser:
    @pytest.fixture
    def parser(self):
        return CreateUserParser()

    def test_parse_basic_user(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED BY 'password123';"
        result = parser.parse(sql)
        assert result is not None
        assert result.user == "admin"
        assert result.host == "localhost"

    def test_parse_user_with_plugin(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED WITH mysql_native_password BY 'pass';"
        result = parser.parse(sql)
        assert result.auth_plugin == "mysql_native_password"

    def test_parse_user_with_hash(self, parser):
        sql = "CREATE USER 'admin'@'%' IDENTIFIED WITH mysql_native_password AS '*HASH123';"
        result = parser.parse(sql)
        assert result.password_hash == "*HASH123"

    def test_parse_user_require_ssl(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass' REQUIRE SSL;"
        result = parser.parse(sql)
        assert result.require_ssl is True

    def test_parse_user_require_x509(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass' REQUIRE X509;"
        result = parser.parse(sql)
        assert result.require_ssl is True

    def test_parse_user_account_lock(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass' ACCOUNT LOCK;"
        result = parser.parse(sql)
        assert result.account_locked is True

    def test_parse_user_password_expire(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass' PASSWORD EXPIRE;"
        result = parser.parse(sql)
        assert result.password_expired is True

    def test_parse_user_if_not_exists(self, parser):
        sql = "CREATE USER IF NOT EXISTS 'testuser'@'%' IDENTIFIED BY 'pass';"
        result = parser.parse(sql)
        assert result.user == "testuser"
        assert result.host == "%"

    def test_parse_user_no_password(self, parser):
        sql = "CREATE USER 'reader'@'localhost';"
        result = parser.parse(sql)
        assert result is not None
        assert result.user == "reader"
        assert result.password_hash is None

    def test_parse_invalid_sql(self, parser):
        result = parser.parse("SELECT 1;")
        assert result is None

    def test_parse_defaults(self, parser):
        sql = "CREATE USER 'u'@'localhost';"
        result = parser.parse(sql)
        assert result.require_ssl is False
        assert result.account_locked is False
        assert result.password_expired is False


# ============================================================
# GrantParser 테스트
# ============================================================
class TestGrantParser:
    @pytest.fixture
    def parser(self):
        return GrantParser()

    def test_parse_all_privileges(self, parser):
        sql = "GRANT ALL PRIVILEGES ON *.* TO 'admin'@'localhost';"
        result = parser.parse(sql)
        assert result is not None
        assert "ALL PRIVILEGES" in result.privileges
        assert result.grantee_user == "admin"
        assert result.grantee_host == "localhost"
        assert result.object_type == "*.*"
        assert result.database is None
        assert result.table is None

    def test_parse_specific_privileges(self, parser):
        sql = "GRANT SELECT, INSERT, UPDATE ON `mydb`.* TO 'user'@'%';"
        result = parser.parse(sql)
        assert "SELECT" in result.privileges
        assert "INSERT" in result.privileges
        assert "UPDATE" in result.privileges
        assert result.database == "mydb"
        assert result.table is None

    def test_parse_table_level_grant(self, parser):
        sql = "GRANT SELECT ON `mydb`.`users` TO 'reader'@'localhost';"
        result = parser.parse(sql)
        assert result.database == "mydb"
        assert result.table == "users"
        assert result.object_type == "mydb.users"

    def test_parse_with_grant_option(self, parser):
        sql = "GRANT ALL PRIVILEGES ON *.* TO 'admin'@'localhost' WITH GRANT OPTION;"
        result = parser.parse(sql)
        assert result.with_grant_option is True

    def test_parse_without_grant_option(self, parser):
        sql = "GRANT SELECT ON *.* TO 'reader'@'localhost';"
        result = parser.parse(sql)
        assert result.with_grant_option is False

    def test_parse_invalid_sql(self, parser):
        result = parser.parse("REVOKE ALL ON *.* FROM 'user'@'%';")
        assert result is None


# ============================================================
# SQLParser 팩토리 테스트
# ============================================================
class TestSQLParser:
    @pytest.fixture
    def parser(self):
        return SQLParser()

    # --- detect_and_parse ---
    def test_detect_create_table(self, parser):
        sql = "CREATE TABLE `t` (\n  `id` INT\n);"
        result = parser.detect_and_parse(sql)
        assert isinstance(result, ParsedTable)
        assert result.name == "t"

    def test_detect_create_user(self, parser):
        sql = "CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass';"
        result = parser.detect_and_parse(sql)
        assert isinstance(result, ParsedUser)
        assert result.user == "admin"

    def test_detect_grant(self, parser):
        sql = "GRANT SELECT ON *.* TO 'reader'@'localhost';"
        result = parser.detect_and_parse(sql)
        assert isinstance(result, ParsedGrant)

    def test_detect_unknown(self, parser):
        result = parser.detect_and_parse("ALTER TABLE t ADD COLUMN x INT;")
        assert result is None

    def test_detect_empty(self, parser):
        result = parser.detect_and_parse("")
        assert result is None

    def test_detect_with_leading_whitespace(self, parser):
        sql = "  \n  CREATE TABLE `t` (\n  `id` INT\n);"
        result = parser.detect_and_parse(sql)
        assert isinstance(result, ParsedTable)

    # --- parse_table, parse_user, parse_grant shortcuts ---
    def test_parse_table_shortcut(self, parser):
        result = parser.parse_table("CREATE TABLE `t` (\n  `id` INT\n);")
        assert isinstance(result, ParsedTable)

    def test_parse_user_shortcut(self, parser):
        result = parser.parse_user("CREATE USER 'u'@'localhost';")
        assert isinstance(result, ParsedUser)

    def test_parse_grant_shortcut(self, parser):
        result = parser.parse_grant("GRANT SELECT ON *.* TO 'u'@'localhost';")
        assert isinstance(result, ParsedGrant)

    # --- extract 메서드 ---
    def test_extract_create_table_statements(self, parser):
        content = """
-- comment
CREATE TABLE `users` (
  `id` INT NOT NULL AUTO_INCREMENT,
  PRIMARY KEY (`id`)
) ENGINE=InnoDB;

INSERT INTO users VALUES (1, 'test');

CREATE TABLE `orders` (
  `id` INT,
  `user_id` INT
) ENGINE=InnoDB;
"""
        stmts = parser.extract_create_table_statements(content)
        assert len(stmts) == 2
        assert "users" in stmts[0]
        assert "orders" in stmts[1]

    def test_extract_create_user_statements(self, parser):
        content = """
CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass';
CREATE USER 'reader'@'%' IDENTIFIED BY 'pass2';
GRANT ALL ON *.* TO 'admin'@'localhost';
"""
        stmts = parser.extract_create_user_statements(content)
        assert len(stmts) == 2

    def test_extract_grant_statements(self, parser):
        content = """
CREATE USER 'admin'@'localhost' IDENTIFIED BY 'pass';
GRANT ALL PRIVILEGES ON *.* TO 'admin'@'localhost' WITH GRANT OPTION;
GRANT SELECT ON `mydb`.* TO 'reader'@'%';
"""
        stmts = parser.extract_grant_statements(content)
        assert len(stmts) == 2

    def test_extract_create_table_empty(self, parser):
        assert parser.extract_create_table_statements("") == []

    def test_extract_from_content_with_no_matches(self, parser):
        content = "SELECT 1; INSERT INTO t VALUES (1);"
        assert parser.extract_create_table_statements(content) == []
        assert parser.extract_create_user_statements(content) == []
        assert parser.extract_grant_statements(content) == []

    # --- 종합 시나리오 ---
    def test_full_dump_extraction_and_parse(self, parser):
        """SQL 덤프에서 추출 후 파싱까지"""
        dump = """
-- MySQL dump
CREATE TABLE `users` (
  `id` INT NOT NULL AUTO_INCREMENT,
  `email` VARCHAR(255) NOT NULL,
  PRIMARY KEY (`id`),
  UNIQUE KEY `idx_email` (`email`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE USER 'app'@'%' IDENTIFIED WITH mysql_native_password BY 'secret';

GRANT SELECT, INSERT, UPDATE ON `mydb`.* TO 'app'@'%';
"""
        # 테이블
        tables = parser.extract_create_table_statements(dump)
        assert len(tables) == 1
        table = parser.parse_table(tables[0])
        assert table.name == "users"
        assert len(table.columns) == 2
        assert table.get_primary_key() is not None

        # 유저
        users = parser.extract_create_user_statements(dump)
        assert len(users) == 1
        user = parser.parse_user(users[0])
        assert user.user == "app"
        assert user.auth_plugin == "mysql_native_password"

        # 권한
        grants = parser.extract_grant_statements(dump)
        assert len(grants) == 1
        grant = parser.parse_grant(grants[0])
        assert "SELECT" in grant.privileges
        assert grant.database == "mydb"


# ============================================================
# 엣지 케이스 테스트
# ============================================================
class TestParserEdgeCases:
    @pytest.fixture
    def table_parser(self):
        return CreateTableParser()

    def test_table_with_no_body(self, table_parser):
        """괄호 없는 CREATE TABLE"""
        sql = "CREATE TABLE `t`;"
        result = table_parser.parse(sql)
        # TABLE_NAME_PATTERN은 \( 필요하므로 매치 안됨
        assert result is None

    def test_column_with_enum(self, table_parser):
        sql = "CREATE TABLE `t` (\n  `status` ENUM('a','b','c') NOT NULL DEFAULT 'a'\n);"
        result = table_parser.parse(sql)
        col = result.columns[0]
        assert col.data_type == "ENUM"
        assert col.nullable is False

    def test_column_with_set(self, table_parser):
        sql = "CREATE TABLE `t` (\n  `tags` SET('x','y','z')\n);"
        result = table_parser.parse(sql)
        col = result.columns[0]
        assert col.data_type == "SET"

    def test_multiple_fk_different_tables(self, table_parser):
        sql = """CREATE TABLE `t` (
  `id` INT,
  `uid` INT,
  `oid` INT,
  CONSTRAINT `fk_u` FOREIGN KEY (`uid`) REFERENCES `users` (`id`),
  CONSTRAINT `fk_o` FOREIGN KEY (`oid`) REFERENCES `orders` (`id`)
);"""
        result = table_parser.parse(sql)
        assert len(result.foreign_keys) == 2
        names = {fk.name for fk in result.foreign_keys}
        assert "fk_u" in names
        assert "fk_o" in names

    def test_index_without_name(self, table_parser):
        sql = "CREATE TABLE `t` (\n  `a` INT,\n  KEY (`a`)\n);"
        result = table_parser.parse(sql)
        indexes = [i for i in result.indexes if not i.is_primary]
        assert len(indexes) >= 1

    def test_fk_on_delete_no_action(self, table_parser):
        sql = """CREATE TABLE `t` (
  `id` INT,
  `uid` INT,
  CONSTRAINT `fk1` FOREIGN KEY (`uid`) REFERENCES `users` (`id`) ON DELETE NO ACTION ON UPDATE NO ACTION
);"""
        result = table_parser.parse(sql)
        fk = result.foreign_keys[0]
        assert fk.on_delete == "NO_ACTION"
        assert fk.on_update == "NO_ACTION"

    def test_fk_set_default(self, table_parser):
        sql = """CREATE TABLE `t` (
  `id` INT,
  `uid` INT,
  CONSTRAINT `fk1` FOREIGN KEY (`uid`) REFERENCES `users` (`id`) ON DELETE SET DEFAULT
);"""
        result = table_parser.parse(sql)
        fk = result.foreign_keys[0]
        assert fk.on_delete == "SET_DEFAULT"

    def test_large_sql(self, table_parser):
        """많은 컬럼이 있는 테이블"""
        cols = ", ".join(f"`col{i}` INT" for i in range(50))
        sql = f"CREATE TABLE `big` ({cols});"
        result = table_parser.parse(sql)
        assert len(result.columns) == 50

    def test_default_with_escaped_quote(self, table_parser):
        sql = "CREATE TABLE `t` (\n  `note` VARCHAR(100) DEFAULT 'it\\'s a test'\n);"
        result = table_parser.parse(sql)
        col = result.columns[0]
        assert col.default is not None

    # line 208: body가 빈 문자열일 때 parse()가 조기 반환
    def test_parse_returns_table_when_body_empty(self, table_parser):
        """TABLE_NAME_PATTERN은 매칭되지만 닫는 괄호가 없으면 body=""→ 조기 반환"""
        sql = "CREATE TABLE `t` ("  # 여는 괄호만 있고 닫는 괄호 없음
        result = table_parser.parse(sql)
        assert result is not None
        assert result.name == "t"
        assert result.columns == []  # body 없으므로 컬럼 없음

    # line 258: _parse_columns에서 빈 definition skip
    def test_parse_columns_skips_empty_definitions(self, table_parser):
        """_split_definitions가 빈 문자열을 반환할 때 skip"""
        # 이중 콤마 → 빈 definition 발생
        body = "`id` INT,,`name` VARCHAR(255)"
        cols = table_parser._parse_columns(body)
        # 빈 definition은 skip되고 정상 컬럼만 파싱됨
        col_names = [c.name for c in cols]
        assert "id" in col_names
        assert "name" in col_names

    # line 452: _parse_table_options에서 닫는 괄호 없으면 조기 반환
    def test_parse_table_options_no_closing_paren(self, table_parser):
        """SQL에 ) 가 없으면 rfind 반환값 -1 → 즉시 return"""
        from src.core.migration_parsers import ParsedTable
        table = ParsedTable(name="t")
        table_parser._parse_table_options("no paren here at all", table)
        # 옵션 파싱 없이 정상 반환 (예외 없음)
        assert table.engine is None
