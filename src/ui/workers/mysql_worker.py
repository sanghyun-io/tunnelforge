"""MySQL Shell 작업 스레드"""
from PyQt6.QtCore import QThread, pyqtSignal

from src.exporters.mysqlsh_exporter import (
    MySQLShellConfig, MySQLShellExporter, MySQLShellImporter
)


class MySQLShellWorker(QThread):
    """MySQL Shell 작업 스레드"""
    progress = pyqtSignal(str)  # 진행 메시지
    table_progress = pyqtSignal(int, int, str)  # current, total, table_name
    finished = pyqtSignal(bool, str)  # success, message

    # 상세 진행 정보 시그널 (Import 개선용)
    detail_progress = pyqtSignal(dict)  # {'percent': 92, 'mb_done': 88.95, 'mb_total': 96.69, 'rows_sec': 285, 'speed': '1.5 MB/s'}
    table_status = pyqtSignal(str, str, str)  # table_name, status ('pending'/'loading'/'done'/'error'), message
    import_finished = pyqtSignal(bool, str, dict)  # success, message, results {'table_name': {'status': 'done'/'error', 'message': ''}}
    raw_output = pyqtSignal(str)  # mysqlsh 실시간 출력

    def __init__(self, task_type: str, config: MySQLShellConfig, **kwargs):
        super().__init__()
        self.task_type = task_type
        self.config = config
        self.kwargs = kwargs

    def run(self):
        def callback(msg):
            self.progress.emit(msg)

        def table_callback(current, total, table_name):
            self.table_progress.emit(current, total, table_name)

        try:
            if self.task_type == "export_schema":
                exporter = MySQLShellExporter(self.config)
                success, msg = exporter.export_full_schema(
                    self.kwargs['schema'],
                    self.kwargs['output_dir'],
                    self.kwargs.get('threads', 4),
                    self.kwargs.get('compression', 'zstd'),
                    callback,
                    table_callback
                )
                self.finished.emit(success, msg)

            elif self.task_type == "export_tables":
                exporter = MySQLShellExporter(self.config)
                success, msg, tables = exporter.export_tables(
                    self.kwargs['schema'],
                    self.kwargs['tables'],
                    self.kwargs['output_dir'],
                    self.kwargs.get('threads', 4),
                    self.kwargs.get('compression', 'zstd'),
                    self.kwargs.get('include_fk_parents', True),
                    callback,
                    table_callback
                )
                self.finished.emit(success, msg)

            elif self.task_type == "import":
                importer = MySQLShellImporter(self.config)

                # 상세 콜백 함수들
                def detail_callback(info: dict):
                    self.detail_progress.emit(info)

                def table_status_callback(table_name: str, status: str, message: str = ""):
                    self.table_status.emit(table_name, status, message)

                def raw_output_callback(line: str):
                    self.raw_output.emit(line)

                success, msg, results = importer.import_dump(
                    self.kwargs['input_dir'],
                    self.kwargs.get('target_schema'),
                    self.kwargs.get('threads', 4),
                    self.kwargs.get('import_mode', 'replace'),
                    self.kwargs.get('timezone_sql'),
                    callback,
                    table_callback,
                    detail_callback,
                    table_status_callback,
                    raw_output_callback,
                    self.kwargs.get('retry_tables')  # 재시도할 테이블 목록
                )
                self.import_finished.emit(success, msg, results)
                self.finished.emit(success, msg)

        except Exception as e:
            self.finished.emit(False, str(e))
