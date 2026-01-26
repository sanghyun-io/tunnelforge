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
                success, msg = importer.import_dump(
                    self.kwargs['input_dir'],
                    self.kwargs.get('target_schema'),
                    self.kwargs.get('threads', 4),
                    self.kwargs.get('drop_existing_tables', True),
                    callback,
                    table_callback
                )
                self.finished.emit(success, msg)

        except Exception as e:
            self.finished.emit(False, str(e))
