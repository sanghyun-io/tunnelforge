"""SQL execution worker backed by Rust DB Core."""

import os

from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.query_limits import CANCEL_ERROR_CODES, build_query_limits, truncation_notice
from src.core.sql_statement_parser import parse_sql_statements, read_dollar_quote


class SQLExecutionWorker(QThread):
    """SQL 파일 실행 Worker backed by Rust DB Core."""

    progress = pyqtSignal(str)          # 진행 메시지
    output = pyqtSignal(str)            # SQL 실행 출력
    finished = pyqtSignal(bool, str)    # (성공여부, 결과메시지)

    def __init__(self, sql_file: str, host: str, port: int,
                 user: str, password: str, database: str = None,
                 db_engine: str = "mysql", schema: str = "", parent=None, limits=None,
                 read_only: bool = False):
        super().__init__(parent)
        self.read_only = read_only  # TF-STATUS-128: production windows run read-only sessions
        self.limits = dict(limits) if limits is not None else build_query_limits()
        self._connector = None
        self.sql_file = sql_file
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.db_engine = normalize_db_engine(db_engine, port)
        self.schema = schema

    def cancel_query(self):
        """UI 스레드에서 호출: 실행 중인 문장을 서버에서 취소하고 남은 문장 실행을 중단한다."""
        self.requestInterruption()
        cancel = getattr(getattr(self._connector, "connection", None), "cancel_running_query", None)
        if cancel is None:
            return
        try:
            cancel()
        except Exception:
            pass  # 서버 취소 실패는 워커 종료 흐름을 막지 않는다

    def run(self):
        connector = None
        try:
            self.progress.emit("🔌 Rust DB Core 연결 중...")
            connector = create_rust_db_connector(
                self.db_engine,
                self.host,
                int(self.port),
                self.user,
                self.password,
                self.database,
                schema=self.schema if self.db_engine == "postgresql" else "",
                **({"read_only": True} if self.read_only else {}),
            )

            self._connector = connector
            success, message = connector.connect()
            if not success:
                self.finished.emit(False, f"❌ DB 연결 실패: {message}")
                return

            self.progress.emit(f"🚀 SQL 실행 중: {os.path.basename(self.sql_file)}")
            with open(self.sql_file, "r", encoding="utf-8") as f:
                sql_content = f.read()

            statements = self._parse_sql_statements(sql_content, self.db_engine)
            if not statements:
                self.finished.emit(False, "❌ 실행할 SQL 문이 없습니다.")
                return

            total_rows = 0
            connector.connection.query_limits = dict(self.limits)
            with connector.connection.cursor() as cursor:
                for index, statement in enumerate(statements, 1):
                    if self.isInterruptionRequested():
                        self.finished.emit(False, "⚠️ SQL 실행이 취소되었습니다")
                        return
                    preview = " ".join(statement.split())
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    self.progress.emit(f"  [{index}/{len(statements)}] {preview}")

                    cursor.execute(statement)
                    if getattr(cursor, "truncated", False):
                        self.progress.emit("⚠️ " + truncation_notice(cursor.truncated_by, self.limits))
                    rows = cursor.fetchall()
                    if rows:
                        total_rows += len(rows)
                        self.output.emit(self._format_rows(rows))

            self.finished.emit(
                True,
                f"✅ SQL 실행 완료: {len(statements)}개 문장"
                + (f", 결과 {total_rows}행" if total_rows else ""),
            )
        except Exception as e:
            if getattr(e, "error_code", None) in CANCEL_ERROR_CODES:
                self.finished.emit(False, f"⚠️ SQL 실행이 취소되었습니다: {str(e)}")
                return
            self.finished.emit(False, f"❌ SQL 실행 중 오류: {str(e)}")
        finally:
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass

    @staticmethod
    def _parse_sql_statements(sql_text: str, dialect: str = "mysql") -> list:
        return parse_sql_statements(sql_text, dialect)

    @staticmethod
    def _read_dollar_quote(sql_text: str, start: int) -> str:
        return read_dollar_quote(sql_text, start)

    @staticmethod
    def _format_rows(rows: list) -> str:
        if not rows:
            return ""
        columns = list(rows[0].keys()) if isinstance(rows[0], dict) else []
        if not columns:
            return "\n".join(str(row) for row in rows)
        lines = ["\t".join(columns)]
        for row in rows:
            lines.append("\t".join("" if row.get(col) is None else str(row.get(col)) for col in columns))
        return "\n".join(lines)
