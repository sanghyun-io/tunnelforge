"""SQL execution worker backed by Rust DB Core."""

import os

from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_core_service import create_rust_db_connector, normalize_db_engine
from src.core.sql_statement_parser import parse_sql_statements, read_dollar_quote


class SQLExecutionWorker(QThread):
    """SQL 파일 실행 Worker backed by Rust DB Core."""

    progress = pyqtSignal(str)          # 진행 메시지
    output = pyqtSignal(str)            # SQL 실행 출력
    finished = pyqtSignal(bool, str)    # (성공여부, 결과메시지)

    def __init__(self, sql_file: str, host: str, port: int,
                 user: str, password: str, database: str = None,
                 db_engine: str = "mysql", schema: str = "", parent=None):
        super().__init__(parent)
        self.sql_file = sql_file
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.db_engine = normalize_db_engine(db_engine, port)
        self.schema = schema

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
            )

            success, message = connector.connect()
            if not success:
                self.finished.emit(False, f"❌ DB 연결 실패: {message}")
                return

            self.progress.emit(f"🚀 SQL 실행 중: {os.path.basename(self.sql_file)}")
            with open(self.sql_file, "r", encoding="utf-8") as f:
                sql_content = f.read()

            statements = self._parse_sql_statements(sql_content)
            if not statements:
                self.finished.emit(False, "❌ 실행할 SQL 문이 없습니다.")
                return

            total_rows = 0
            with connector.connection.cursor() as cursor:
                for index, statement in enumerate(statements, 1):
                    preview = " ".join(statement.split())
                    if len(preview) > 120:
                        preview = preview[:117] + "..."
                    self.progress.emit(f"  [{index}/{len(statements)}] {preview}")

                    cursor.execute(statement)
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
            self.finished.emit(False, f"❌ SQL 실행 중 오류: {str(e)}")
        finally:
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass

    @staticmethod
    def _parse_sql_statements(sql_text: str) -> list:
        return parse_sql_statements(sql_text)

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
