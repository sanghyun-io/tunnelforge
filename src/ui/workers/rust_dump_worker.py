"""Rust DB Core 작업 스레드"""
from PyQt6.QtCore import QThread, pyqtSignal

from src.exporters.rust_dump_exporter import (
    DEFAULT_DUMP_COMPRESSION, RustDumpConfig, RustDumpExporter, RustDumpImporter
)

CANCELLED_MESSAGE = "작업이 취소되었습니다."


class RustDumpWorker(QThread):
    """Rust DB Core 작업 스레드"""
    progress = pyqtSignal(str)  # 진행 메시지
    table_progress = pyqtSignal(int, int, str)  # current, total, table_name
    finished = pyqtSignal(bool, str)  # success, message

    # 상세 진행 정보 시그널 (Import 개선용)
    detail_progress = pyqtSignal(dict)  # {'percent': 92, 'mb_done': 88.95, 'mb_total': 96.69, 'rows_sec': 285, 'speed': '1.5 MB/s'}
    table_status = pyqtSignal(str, str, str)  # table_name, status ('pending'/'loading'/'done'/'error'), message
    import_finished = pyqtSignal(bool, str, dict)  # success, message, results {'table_name': {'status': 'done'/'error', 'message': ''}}
    raw_output = pyqtSignal(str)  # rust_dump 실시간 출력
    metadata_analyzed = pyqtSignal(dict)  # dump 메타데이터 분석 결과 (chunk_counts, table_sizes, total_bytes, schema)
    table_chunk_progress = pyqtSignal(str, int, int)  # table_name, completed_chunks, total_chunks (테이블별 chunk 진행률)

    def __init__(self, task_type: str, config: RustDumpConfig, **kwargs):
        super().__init__()
        self.task_type = task_type
        self.config = config
        self.kwargs = kwargs
        self._cancel_requested = False
        self._active_runner = None

    def cancel(self) -> bool:
        """실행 중인 dump/import 작업의 취소를 요청한다.

        RustDumpExporter/RustDumpImporter가 이 워커를 위해 전용으로 만든
        DbCoreFacade(`_owns_facade=True`)의 Rust core 프로세스만 직접 terminate()한다.
        DbCoreServiceClient.shutdown()은 request()가 블로킹 중에 잡고 있는 것과
        동일한 락을 요구하므로, dump.run/dump.import가 진행 중일 때 UI 스레드에서
        호출하면 그대로 멈춘다. 프로세스를 직접 종료시키면 워커 스레드가 블로킹 중인
        읽기에서 즉시 깨어나 예외/실패 결과로 빠져나온다.
        """
        self._cancel_requested = True
        runner = self._active_runner
        if runner is not None and getattr(runner, "_owns_facade", False):
            facade = getattr(runner, "facade", None)
            client = getattr(facade, "client", None) if facade is not None else None
            process = getattr(client, "_process", None) if client is not None else None
            if process is not None and process.poll() is None:
                process.terminate()
        return True

    def _is_cancelled_message(self, success: bool, msg: str):
        if self._cancel_requested:
            return False, CANCELLED_MESSAGE
        return success, msg

    def run(self):
        def callback(msg):
            self.progress.emit(msg)

        def table_callback(current, total, table_name):
            self.table_progress.emit(current, total, table_name)

        try:
            if self.task_type == "export_schema":
                exporter = RustDumpExporter(self.config)
                self._active_runner = exporter

                # 상세 콜백 함수들
                def detail_callback(info: dict):
                    self.detail_progress.emit(info)

                def table_status_callback(table_name: str, status: str, message: str = ""):
                    self.table_status.emit(table_name, status, message)

                def raw_output_callback(line: str):
                    self.raw_output.emit(line)

                success, msg = exporter.export_full_schema(
                    self.kwargs['schema'],
                    self.kwargs['output_dir'],
                    self.kwargs.get('threads', 8),
                    self.kwargs.get('compression', DEFAULT_DUMP_COMPRESSION),
                    callback,
                    table_callback,
                    detail_callback,
                    table_status_callback,
                    raw_output_callback
                )
                success, msg = self._is_cancelled_message(success, msg)
                self.finished.emit(success, msg)

            elif self.task_type == "export_tables":
                exporter = RustDumpExporter(self.config)
                self._active_runner = exporter

                # 상세 콜백 함수들
                def detail_callback(info: dict):
                    self.detail_progress.emit(info)

                def table_status_callback(table_name: str, status: str, message: str = ""):
                    self.table_status.emit(table_name, status, message)

                def raw_output_callback(line: str):
                    self.raw_output.emit(line)

                success, msg, tables = exporter.export_tables(
                    self.kwargs['schema'],
                    self.kwargs['tables'],
                    self.kwargs['output_dir'],
                    self.kwargs.get('threads', 8),
                    self.kwargs.get('compression', DEFAULT_DUMP_COMPRESSION),
                    self.kwargs.get('include_fk_parents', True),
                    callback,
                    table_callback,
                    detail_callback,
                    table_status_callback,
                    raw_output_callback
                )
                success, msg = self._is_cancelled_message(success, msg)
                self.finished.emit(success, msg)

            elif self.task_type == "import":
                importer = RustDumpImporter(self.config)
                self._active_runner = importer

                # 상세 콜백 함수들
                def detail_callback(info: dict):
                    self.detail_progress.emit(info)

                def table_status_callback(table_name: str, status: str, message: str = ""):
                    self.table_status.emit(table_name, status, message)

                def raw_output_callback(line: str):
                    self.raw_output.emit(line)

                def metadata_callback(metadata: dict):
                    self.metadata_analyzed.emit(metadata)

                def table_chunk_progress_callback(table_name: str, completed_chunks: int, total_chunks: int):
                    self.table_chunk_progress.emit(table_name, completed_chunks, total_chunks)

                success, msg, results = importer.import_dump(
                    self.kwargs['input_dir'],
                    self.kwargs.get('target_schema'),
                    self.kwargs.get('threads', 8),
                    self.kwargs.get('import_mode', 'replace'),
                    self.kwargs.get('timezone_sql'),
                    callback,
                    table_callback,
                    detail_callback,
                    table_status_callback,
                    raw_output_callback,
                    self.kwargs.get('retry_tables'),  # 재시도할 테이블 목록
                    metadata_callback,
                    table_chunk_progress_callback,
                )
                success, msg = self._is_cancelled_message(success, msg)
                self.import_finished.emit(success, msg, results)
                self.finished.emit(success, msg)

        except Exception as e:
            if self._cancel_requested:
                message = CANCELLED_MESSAGE
                if self.task_type == "import":
                    self.import_finished.emit(False, message, {})
                self.finished.emit(False, message)
            else:
                self.finished.emit(False, str(e))
        finally:
            self._active_runner = None
