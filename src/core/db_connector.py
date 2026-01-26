"""
MySQL 데이터베이스 연결 클래스
"""
import pymysql
from typing import List, Dict, Any, Optional, Tuple


class MySQLConnector:
    """MySQL 데이터베이스 연결 및 쿼리 실행 클래스"""

    def __init__(self, host: str, port: int, user: str, password: str, database: str = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.connection: Optional[pymysql.Connection] = None

    def connect(self) -> Tuple[bool, str]:
        """데이터베이스 연결"""
        try:
            self.connection = pymysql.connect(
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor,
                connect_timeout=10
            )
            return True, "연결 성공"
        except pymysql.Error as e:
            error_code = e.args[0] if e.args else 0
            error_msg = e.args[1] if len(e.args) > 1 else str(e)
            return False, f"MySQL 오류 ({error_code}): {error_msg}"
        except Exception as e:
            return False, f"연결 오류: {str(e)}"

    def disconnect(self):
        """연결 종료"""
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            finally:
                self.connection = None

    def is_connected(self) -> bool:
        """연결 상태 확인"""
        if self.connection:
            try:
                self.connection.ping(reconnect=False)
                return True
            except Exception:
                return False
        return False

    def use_database(self, database: str) -> Tuple[bool, str]:
        """데이터베이스 선택"""
        try:
            self.connection.select_db(database)
            self.database = database
            return True, f"데이터베이스 '{database}' 선택됨"
        except Exception as e:
            return False, str(e)

    def get_schemas(self) -> List[str]:
        """스키마(데이터베이스) 목록 조회 (시스템 DB 제외)"""
        if not self.connection:
            return []

        try:
            with self.connection.cursor() as cursor:
                cursor.execute("SHOW DATABASES")
                exclude = {'information_schema', 'mysql', 'performance_schema', 'sys'}
                return [row['Database'] for row in cursor.fetchall()
                        if row['Database'] not in exclude]
        except Exception as e:
            print(f"스키마 조회 오류: {e}")
            return []

    def get_tables(self, schema: str = None) -> List[str]:
        """테이블 목록 조회"""
        if not self.connection:
            return []

        try:
            with self.connection.cursor() as cursor:
                if schema:
                    cursor.execute(f"SHOW TABLES FROM `{schema}`")
                else:
                    cursor.execute("SHOW TABLES")

                return [list(row.values())[0] for row in cursor.fetchall()]
        except Exception as e:
            print(f"테이블 조회 오류: {e}")
            return []

    def execute(self, query: str, params: tuple = None) -> List[Dict[str, Any]]:
        """쿼리 실행 및 결과 반환"""
        if not self.connection:
            return []

        try:
            with self.connection.cursor() as cursor:
                cursor.execute(query, params)
                return cursor.fetchall()
        except Exception as e:
            print(f"쿼리 실행 오류: {e}")
            return []

    def execute_many(self, query: str, data: List[tuple]) -> int:
        """배치 쿼리 실행"""
        if not self.connection:
            return 0

        try:
            with self.connection.cursor() as cursor:
                cursor.executemany(query, data)
                self.connection.commit()
                return cursor.rowcount
        except Exception as e:
            print(f"배치 쿼리 오류: {e}")
            return 0

    def get_table_columns(self, table: str, schema: str = None) -> List[Dict[str, Any]]:
        """테이블 컬럼 정보 조회"""
        target_schema = schema or self.database
        if not target_schema:
            return []

        query = """
        SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, COLUMN_KEY, COLUMN_DEFAULT, EXTRA
        FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s
        ORDER BY ORDINAL_POSITION
        """
        return self.execute(query, (target_schema, table))

    def get_create_table_statement(self, table: str, schema: str = None) -> str:
        """CREATE TABLE 문 조회"""
        if not self.connection:
            return ""

        try:
            # 스키마가 지정되면 해당 스키마로 전환
            if schema and schema != self.database:
                self.connection.select_db(schema)

            with self.connection.cursor() as cursor:
                cursor.execute(f"SHOW CREATE TABLE `{table}`")
                result = cursor.fetchone()
                if result:
                    return result.get('Create Table', '')
            return ""
        except Exception as e:
            print(f"CREATE TABLE 조회 오류: {e}")
            return ""

    def get_table_data(self, table: str, schema: str = None, limit: int = None) -> List[Dict[str, Any]]:
        """테이블 데이터 조회"""
        if schema and schema != self.database:
            self.connection.select_db(schema)

        query = f"SELECT * FROM `{table}`"
        if limit:
            query += f" LIMIT {limit}"

        return self.execute(query)

    def get_row_count(self, table: str, schema: str = None) -> int:
        """테이블 행 수 조회"""
        if schema and schema != self.database:
            self.connection.select_db(schema)

        result = self.execute(f"SELECT COUNT(*) as cnt FROM `{table}`")
        if result:
            return result[0].get('cnt', 0)
        return 0

    def __enter__(self):
        """컨텍스트 매니저 진입"""
        self.connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """컨텍스트 매니저 종료"""
        self.disconnect()
        return False


def test_mysql_connection(host: str, port: int, user: str, password: str) -> Tuple[bool, str]:
    """MySQL 연결 테스트 (유틸리티 함수)"""
    connector = MySQLConnector(host, port, user, password)
    success, msg = connector.connect()
    if success:
        connector.disconnect()
    return success, msg
