"""
SQL 검증 워커
- QThread 기반 비동기 검증
- 취소 지원
"""
from PyQt6.QtCore import QThread, pyqtSignal
from typing import List, Optional


class ValidationWorker(QThread):
    """SQL 검증 비동기 워커

    Signals:
        validation_completed: 검증 완료 시 ValidationIssue 목록 전달
        error_occurred: 오류 발생 시 에러 메시지 전달
    """
    validation_completed = pyqtSignal(list)  # List[ValidationIssue]
    error_occurred = pyqtSignal(str)

    def __init__(self, validator, sql: str, schema: str = None):
        """
        Args:
            validator: SQLValidator 인스턴스
            sql: 검증할 SQL 문자열
            schema: 대상 스키마 (optional)
        """
        super().__init__()
        self.validator = validator
        self.sql = sql
        self.schema = schema
        self._cancelled = False

    def run(self):
        """검증 실행 (비동기)"""
        if self._cancelled:
            return

        try:
            issues = self.validator.validate(self.sql, self.schema)

            if not self._cancelled:
                self.validation_completed.emit(issues)

        except Exception as e:
            if not self._cancelled:
                self.error_occurred.emit(str(e))

    def cancel(self):
        """검증 취소"""
        self._cancelled = True


class MetadataLoadWorker(QThread):
    """스키마 메타데이터 로드 워커

    DB 연결 후 테이블/컬럼 정보를 백그라운드에서 로드

    Signals:
        load_completed: 로드 완료 시 SchemaMetadata 전달
        error_occurred: 오류 발생 시 에러 메시지 전달
        progress: 진행 상태 메시지 전달
    """
    load_completed = pyqtSignal(object)  # SchemaMetadata
    error_occurred = pyqtSignal(str)
    progress = pyqtSignal(str)

    def __init__(self, connector, schema: str = None):
        """
        Args:
            connector: MySQLConnector 인스턴스 (연결된 상태)
            schema: 대상 스키마 (optional)
        """
        super().__init__()
        self.connector = connector
        self.schema = schema
        self._cancelled = False

    def run(self):
        """메타데이터 로드 실행"""
        from src.core.sql_validator import SchemaMetadata

        if self._cancelled:
            return

        metadata = SchemaMetadata()

        try:
            self.progress.emit("DB 버전 확인 중...")
            metadata.db_version = self.connector.get_db_version()

            if self._cancelled:
                return

            self.progress.emit("테이블 목록 조회 중...")
            tables = self.connector.get_tables(self.schema)
            metadata.tables = set(tables)

            if self._cancelled:
                return

            total = len(tables)
            for i, table in enumerate(tables):
                if self._cancelled:
                    return

                self.progress.emit(f"컬럼 정보 로드 중... ({i+1}/{total})")
                columns = self.connector.get_column_names(table, self.schema)
                metadata.columns[table] = set(columns)

            if not self._cancelled:
                self.progress.emit("메타데이터 로드 완료")
                self.load_completed.emit(metadata)

        except Exception as e:
            if not self._cancelled:
                self.error_occurred.emit(str(e))

    def cancel(self):
        """로드 취소"""
        self._cancelled = True


class AutoCompleteWorker(QThread):
    """자동완성 목록 조회 워커

    Signals:
        completions_ready: 자동완성 목록 준비 시 전달
        error_occurred: 오류 발생 시 에러 메시지 전달
    """
    completions_ready = pyqtSignal(list)  # List[Dict]
    error_occurred = pyqtSignal(str)

    def __init__(self, completer, sql: str, cursor_pos: int, schema: str = None):
        """
        Args:
            completer: SQLAutoCompleter 인스턴스
            sql: SQL 문자열
            cursor_pos: 커서 위치
            schema: 대상 스키마 (optional)
        """
        super().__init__()
        self.completer = completer
        self.sql = sql
        self.cursor_pos = cursor_pos
        self.schema = schema
        self._cancelled = False

    def run(self):
        """자동완성 목록 조회"""
        if self._cancelled:
            return

        try:
            completions = self.completer.get_completions(
                self.sql, self.cursor_pos, self.schema
            )

            if not self._cancelled:
                self.completions_ready.emit(completions)

        except Exception as e:
            if not self._cancelled:
                self.error_occurred.emit(str(e))

    def cancel(self):
        """조회 취소"""
        self._cancelled = True
