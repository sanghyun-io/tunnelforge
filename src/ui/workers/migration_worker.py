"""마이그레이션 분석 작업 스레드"""
from PyQt6.QtCore import QThread, pyqtSignal

from src.core.db_connector import MySQLConnector
from src.core.migration_analyzer import (
    MigrationAnalyzer, AnalysisResult, CleanupAction, ActionType
)


class MigrationAnalyzerWorker(QThread):
    """마이그레이션 분석 작업 스레드"""
    progress = pyqtSignal(str)  # 진행 메시지
    analysis_complete = pyqtSignal(object)  # AnalysisResult
    finished = pyqtSignal(bool, str)  # success, message

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        check_orphans: bool = True,
        check_charset: bool = True,
        check_keywords: bool = True,
        check_routines: bool = True,
        check_sql_mode: bool = True,
        # MySQL 8.4 Upgrade Checker 옵션
        check_auth_plugins: bool = True,
        check_zerofill: bool = True,
        check_float_precision: bool = True,
        check_fk_name_length: bool = True,
        # 추가 검사 옵션
        check_invalid_dates: bool = True,
        check_year2: bool = True,
        check_deprecated_engines: bool = True,
        check_enum_empty: bool = True,
        check_timestamp_range: bool = True
    ):
        super().__init__()
        self.connector = connector
        self.schema = schema
        self.check_orphans = check_orphans
        self.check_charset = check_charset
        self.check_keywords = check_keywords
        self.check_routines = check_routines
        self.check_sql_mode = check_sql_mode
        # MySQL 8.4 Upgrade Checker 옵션
        self.check_auth_plugins = check_auth_plugins
        self.check_zerofill = check_zerofill
        self.check_float_precision = check_float_precision
        self.check_fk_name_length = check_fk_name_length
        # 추가 검사 옵션
        self.check_invalid_dates = check_invalid_dates
        self.check_year2 = check_year2
        self.check_deprecated_engines = check_deprecated_engines
        self.check_enum_empty = check_enum_empty
        self.check_timestamp_range = check_timestamp_range

    def run(self):
        try:
            analyzer = MigrationAnalyzer(self.connector)
            analyzer.set_progress_callback(lambda msg: self.progress.emit(msg))

            result = analyzer.analyze_schema(
                self.schema,
                check_orphans=self.check_orphans,
                check_charset=self.check_charset,
                check_keywords=self.check_keywords,
                check_routines=self.check_routines,
                check_sql_mode=self.check_sql_mode,
                # MySQL 8.4 Upgrade Checker 옵션
                check_auth_plugins=self.check_auth_plugins,
                check_zerofill=self.check_zerofill,
                check_float_precision=self.check_float_precision,
                check_fk_name_length=self.check_fk_name_length,
                # 추가 검사 옵션
                check_invalid_dates=self.check_invalid_dates,
                check_year2=self.check_year2,
                check_deprecated_engines=self.check_deprecated_engines,
                check_enum_empty=self.check_enum_empty,
                check_timestamp_range=self.check_timestamp_range
            )

            self.analysis_complete.emit(result)
            self.finished.emit(True, "분석 완료")

        except Exception as e:
            self.finished.emit(False, f"분석 오류: {str(e)}")


class CleanupWorker(QThread):
    """정리 작업 실행 스레드"""
    progress = pyqtSignal(str)  # 진행 메시지
    action_complete = pyqtSignal(str, bool, str, int)  # table, success, message, affected_rows
    finished = pyqtSignal(bool, str, dict)  # success, message, results

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        actions: list,  # List[CleanupAction]
        dry_run: bool = True
    ):
        super().__init__()
        if not dry_run:
            raise RuntimeError(
                "Legacy Python cleanup worker actual execution is disabled. "
                "DB mutations must be owned by Rust Core."
            )

        self.connector = connector
        self.schema = schema
        self.actions = actions

    def run(self):
        try:
            analyzer = MigrationAnalyzer(self.connector)
            analyzer.set_progress_callback(lambda msg: self.progress.emit(msg))

            results = {}
            total_affected = 0
            all_success = True

            mode = "[DRY-RUN]"
            self.progress.emit(f"🔧 {mode} 정리 작업 시작 ({len(self.actions)}개)")

            for i, action in enumerate(self.actions, 1):
                self.progress.emit(f"  {mode} 처리 중: {action.table} ({i}/{len(self.actions)})")

                success, msg, affected = analyzer.execute_cleanup(action, dry_run=True)

                results[action.table] = {
                    'success': success,
                    'message': msg,
                    'affected_rows': affected,
                    'action_type': action.action_type.value
                }

                self.action_complete.emit(action.table, success, msg, affected)

                if success:
                    total_affected += affected
                else:
                    all_success = False

            summary = f"✅ {mode} 완료: {total_affected}개 행 영향받음"
            self.progress.emit(summary)
            self.finished.emit(all_success, summary, results)

        except Exception as e:
            self.finished.emit(False, f"정리 작업 오류: {str(e)}", {})
