"""PostgreSQL database connection helpers."""
from typing import Optional, Tuple

from src.core.db_core_service import DbCoreFacade, DbEndpoint, RustDbConnection


class PostgresConnector:
    """Small PostgreSQL connector used for connection tests."""

    def __init__(self, host: str, port: int, user: str, password: str, database: str = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database or "postgres"
        self.engine = "postgresql"
        self.facade = DbCoreFacade()
        self.connection = None

    def connect(self) -> Tuple[bool, str]:
        try:
            endpoint = DbEndpoint(
                engine="postgresql",
                host=self.host,
                port=self.port,
                user=self.user,
                password=self.password,
                database=self.database,
            )
            connection_id = self.facade.open_connection(endpoint)
            self.connection = RustDbConnection(endpoint, self.facade, connection_id)
            return True, "연결 성공"
        except Exception as exc:
            return False, f"PostgreSQL 오류: {exc}"

    def disconnect(self):
        if self.connection:
            try:
                self.connection.close()
            except Exception:
                pass
            finally:
                self.connection = None

    def schema_exists(self, schema_name: Optional[str]) -> bool:
        if not schema_name or not self.connection:
            return True
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                    (schema_name,),
                )
                return cursor.fetchone() is not None
        except Exception:
            return False
