"""PostgreSQL database connection helpers."""
from typing import Any, Optional, Tuple

from src.core.db_core_service import (
    DbEndpoint,
    RustDbConnection,
    RustDbConnector,
    get_shared_db_core_facade,
)


class PostgresConnector:
    """Small PostgreSQL connector used for connection tests."""

    def __init__(self, host: str, port: int, user: str, password: str, database: str = None,
                 facade: Optional[Any] = None):
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database or "postgres"
        self.engine = "postgresql"
        # 앱 공유 facade 재사용 — 커넥터별 전용 Rust 서브프로세스를 띄우지 않는다.
        self.facade = facade if facade is not None else get_shared_db_core_facade()
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
        """스키마 존재 여부 확인 (조회는 RustDbConnector delegate에 위임)"""
        if not schema_name or not self.connection:
            return True
        try:
            delegate = RustDbConnector(
                "postgresql", self.host, self.port, self.user, self.password,
                database=self.database, facade=self.facade,
            )
            delegate.connection = self.connection
            return delegate.schema_exists(schema_name)
        except Exception:
            return False
