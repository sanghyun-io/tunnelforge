"""
One-Click MySQL 8.0 → 8.4 마이그레이션 다이얼로그

한 번의 클릭으로 Pre-flight → Analysis → Execution → Validation까지
전체 마이그레이션 프로세스를 자동으로 실행합니다.
"""
import threading
from datetime import datetime
from typing import Optional, List, Any

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QStackedWidget,
    QWidget, QLabel, QPushButton, QProgressBar,
    QTextEdit, QGroupBox, QMessageBox, QFileDialog,
    QCheckBox, QScrollArea, QFrame
)
from PyQt6.QtCore import QThread, pyqtSignal, Qt
from PyQt6.QtGui import QFont, QColor

from src.core.db_connector import MySQLConnector
from src.core.migration_preflight import PreflightChecker, PreflightResult, CheckSeverity
from src.core.migration_auto_recommend import AutoRecommendationEngine
from src.core.migration_state_tracker import (
    MigrationStateTracker, MigrationState, MigrationPhase, get_state_tracker
)
from src.core.migration_validator import PostMigrationValidator, MigrationReport
from src.core.oneclick_log import create_oneclick_logger, close_oneclick_logger


# 스타일 상수
STYLE_SUCCESS = "color: #27ae60; font-weight: bold;"
STYLE_ERROR = "color: #e74c3c; font-weight: bold;"
STYLE_WARNING = "color: #f39c12; font-weight: bold;"
STYLE_INFO = "color: #3498db;"
STYLE_MUTED = "color: #7f8c8d;"


class OneClickMigrationWorker(QThread):
    """전체 마이그레이션 프로세스 실행 Worker.

    This hidden workflow is allowed to run only on the Rust DB Core connector.
    Python keeps orchestration, recommendation, and UI logging here; every DB
    operation must go through the Rust-backed connector facade.
    """

    phase_changed = pyqtSignal(str, str)  # phase, phase_name
    progress = pyqtSignal(int, str)  # percent, message
    log_message = pyqtSignal(str, str)  # message, style
    preflight_result = pyqtSignal(object)  # PreflightResult
    analysis_result = pyqtSignal(int, int, int)  # total, auto_fixable, manual
    execution_plan_ready = pyqtSignal(object, object)  # steps, summary (Phase 3 완료 후 일시 정지)
    finished = pyqtSignal(bool, object)  # success, MigrationReport

    def __init__(
        self,
        connector: MySQLConnector,
        schema: str,
        dry_run: bool = False,
        backup_confirmed: bool = False
    ):
        super().__init__()
        self.connector = connector
        self.schema = schema
        self.dry_run = dry_run
        self.backup_confirmed = backup_confirmed
        self._is_cancelled = False
        self._started_at: Optional[datetime] = None
        self._pre_issues: List[Any] = []
        self._execution_gate = threading.Event()  # Phase 3→4 사용자 확인 게이트

    def cancel(self):
        """작업 취소 요청"""
        self._is_cancelled = True
        self._execution_gate.set()  # 대기 중이면 즉시 해제

    def resume_execution(self):
        """실행 재개 (ExecutionPlanWidget에서 호출)"""
        self._execution_gate.set()

    def run(self):
        """전체 프로세스 실행"""
        _mig_logger = None
        _log_path = ""
        try:
            self._started_at = datetime.now()

            # per-run 로그 파일 생성
            _mig_logger, _log_path = create_oneclick_logger(self.schema)
            _mig_logger.info(f"=== One-Click 마이그레이션 시작: schema={self.schema}, dry_run={self.dry_run} ===")

            self._ensure_rust_core_connector()
            self.log_message.emit("🦀 Rust DB Core 연결 확인 완료", STYLE_MUTED)
            _mig_logger.info("Rust DB Core connector verified")

            # Phase 1: Pre-flight
            self.phase_changed.emit(MigrationPhase.PREFLIGHT, "사전 검사")
            self.log_message.emit("🔍 Pre-flight 검사 시작...", STYLE_INFO)
            _mig_logger.info("[Phase 1] Pre-flight 검사 시작")

            preflight = PreflightChecker(self.connector)
            preflight.set_progress_callback(lambda msg: self.log_message.emit(msg, STYLE_MUTED))
            result = preflight.check_all(self.schema, self.backup_confirmed)

            self.preflight_result.emit(result)

            if not result.passed:
                self.log_message.emit("❌ Pre-flight 검사 실패", STYLE_ERROR)
                for error in result.errors:
                    self.log_message.emit(f"  - {error}", STYLE_ERROR)
                _mig_logger.error(f"[Phase 1] Pre-flight 실패: {result.errors}")
                self.finished.emit(False, None)
                return

            self.log_message.emit("✅ Pre-flight 검사 통과", STYLE_SUCCESS)
            _mig_logger.info("[Phase 1] Pre-flight 통과")
            self.progress.emit(20, "Pre-flight 완료")

            if self._is_cancelled:
                self.log_message.emit("⚠️ 작업이 취소되었습니다.", STYLE_WARNING)
                self.finished.emit(False, None)
                return

            # Phase 2: Analysis
            self.phase_changed.emit(MigrationPhase.ANALYSIS, "분석")
            self.log_message.emit("📊 스키마 분석 중...", STYLE_INFO)
            _mig_logger.info("[Phase 2] 스키마 분석 시작")

            from src.core.migration_analyzer import MigrationAnalyzer
            analyzer = MigrationAnalyzer(self.connector)
            analyzer.set_progress_callback(lambda msg: self.log_message.emit(msg, STYLE_MUTED))
            analysis = analyzer.analyze_schema(self.schema)

            self._pre_issues = analysis.compatibility_issues
            issue_count = len(self._pre_issues)

            self.log_message.emit(f"📋 발견된 이슈: {issue_count}개", STYLE_INFO)
            _mig_logger.info(f"[Phase 2] 분석 완료 - 이슈 {issue_count}개")
            self.progress.emit(40, f"분석 완료 - {issue_count}개 이슈")

            if issue_count == 0:
                self.log_message.emit("✅ 호환성 이슈가 없습니다!", STYLE_SUCCESS)
                self.analysis_result.emit(0, 0, 0)
                self.finished.emit(True, self._create_empty_report())
                return

            if self._is_cancelled:
                self.log_message.emit("⚠️ 작업이 취소되었습니다.", STYLE_WARNING)
                self.finished.emit(False, None)
                return

            # Phase 3: Auto-Recommend
            self.phase_changed.emit(MigrationPhase.RECOMMENDATION, "권장 옵션 선택")
            self.log_message.emit("🎯 자동 권장 옵션 선택 중...", STYLE_INFO)

            from src.core.migration_fix_wizard import SmartFixGenerator, FixWizardStep

            generator = SmartFixGenerator(self.connector, self.schema)
            steps = []

            for i, issue in enumerate(self._pre_issues):
                options = generator.get_fix_options(issue)
                step = FixWizardStep(
                    issue_index=i,
                    issue_type=issue.issue_type,
                    location=issue.location,
                    description=issue.description,
                    options=options
                )
                steps.append(step)

            engine = AutoRecommendationEngine(self.connector, self.schema)
            steps = engine.recommend_all(self._pre_issues, steps)
            summary = engine.get_summary(steps, self._pre_issues)

            self.analysis_result.emit(
                summary.total_issues,
                summary.auto_fixable,
                summary.manual_review
            )

            self.log_message.emit(
                f"  - 자동 수정 가능: {summary.auto_fixable}개",
                STYLE_SUCCESS if summary.auto_fixable > 0 else STYLE_MUTED
            )
            self.log_message.emit(
                f"  - 수동 검토 필요: {summary.manual_review}개",
                STYLE_WARNING if summary.manual_review > 0 else STYLE_MUTED
            )
            self.log_message.emit(
                f"  - 건너뛰기 권장: {summary.skip_recommended}개",
                STYLE_MUTED
            )

            self.progress.emit(50, "권장 옵션 선택 완료")

            # Phase 3→4 사이: 실행 계획 확인 대기
            # 사용자가 "실행 시작" 버튼을 누를 때까지 일시 정지
            self._execution_gate.clear()
            self.execution_plan_ready.emit(steps, summary)
            _mig_logger.info("[Phase 3→4] 실행 계획 확인 대기 중...")

            # 취소 체크하며 대기 (100ms 간격)
            while not self._execution_gate.wait(timeout=0.1):
                if self._is_cancelled:
                    break

            if self._is_cancelled:
                self.log_message.emit("⚠️ 작업이 취소되었습니다.", STYLE_WARNING)
                self.finished.emit(False, None)
                return

            _mig_logger.info("[Phase 3→4] 실행 계획 확인 완료 - 실행 시작")

            # Phase 4: Execution
            self.phase_changed.emit(MigrationPhase.EXECUTION, "실행")

            if self.dry_run:
                self.log_message.emit("🧪 [DRY-RUN] 실제 실행하지 않음", STYLE_WARNING)
            else:
                self.log_message.emit("🔧 수정 작업 실행 중...", STYLE_INFO)

            execution_log = []
            executed_count = 0
            total_executable = summary.auto_fixable

            from src.core.migration_fix_wizard import BatchFixExecutor

            executor = BatchFixExecutor(self.connector, self.schema)
            executor.set_progress_callback(
                lambda msg: self.log_message.emit(msg, STYLE_INFO)
            )

            batch_result = executor.execute_batch(steps, dry_run=self.dry_run)

            # BatchExecutionResult → execution_log 변환
            # result.location을 직접 사용 (FK 정렬 후 step↔result 매핑 오류 방지)
            _mig_logger.info(f"[Phase 4] 실행 완료 - 성공:{batch_result.success_count} 실패:{batch_result.fail_count} 스킵:{batch_result.skip_count}")
            executed_count = batch_result.success_count
            for result in batch_result.results:
                loc = result.location or "<unknown>"
                if result.success:
                    if result.message not in ("건너뛰기", "수동 처리 필요"):
                        sql_preview = result.sql_executed[:50]
                        suffix = "..." if len(result.sql_executed) > 50 else ""
                        execution_log.append(f"[OK] {loc}: {sql_preview}{suffix}")
                        _mig_logger.info(
                            f"[OK] location={loc} affected_rows={result.affected_rows}\n"
                            f"     SQL: {result.sql_executed}"
                        )
                    else:
                        reason = f"{result.message}: {result.description}" if result.description else result.message
                        execution_log.append(f"[SKIP] {loc}: {reason}")
                        _mig_logger.info(f"[SKIP] location={loc} reason={reason}")
                else:
                    execution_log.append(f"[FAIL] {loc}: {result.error or result.message}")
                    _mig_logger.error(
                        f"[FAIL] location={loc} error={result.error or result.message}\n"
                        f"       SQL: {result.sql_executed}"
                    )

            # Rollback SQL이 있으면 로그에 기록
            if batch_result.rollback_sql:
                execution_log.append(f"\n-- Rollback SQL --\n{batch_result.rollback_sql}")

            self.progress.emit(90, "실행 완료")
            self.log_message.emit(
                f"✅ 실행 완료: {executed_count}/{total_executable}개 "
                f"(실패: {batch_result.fail_count}, 스킵: {batch_result.skip_count})",
                STYLE_SUCCESS
            )

            if self._is_cancelled:
                self.log_message.emit("⚠️ 작업이 취소되었습니다.", STYLE_WARNING)
                self.finished.emit(False, None)
                return

            # Phase 5: Validation
            self.phase_changed.emit(MigrationPhase.VALIDATION, "검증")
            self.log_message.emit("🔍 마이그레이션 결과 검증 중...", STYLE_INFO)
            _mig_logger.info("[Phase 5] 검증 시작")

            validator = PostMigrationValidator(self.connector)
            validation = validator.validate(self.schema, self._pre_issues)

            report = validator.generate_report(
                self.schema,
                self._pre_issues,
                validation,
                self._started_at,
                execution_log
            )
            report.execution_log_path = _log_path

            self.progress.emit(100, "검증 완료")

            if validation.all_fixed:
                self.log_message.emit("✅ 모든 이슈가 해결되었습니다!", STYLE_SUCCESS)
                _mig_logger.info("[Phase 5] 모든 이슈 해결됨")
            else:
                self.log_message.emit(
                    f"⚠️ 남은 이슈: {len(validation.remaining_issues)}개",
                    STYLE_WARNING
                )
                if validation.new_issues:
                    self.log_message.emit(
                        f"⚠️ 새 이슈: {len(validation.new_issues)}개",
                        STYLE_WARNING
                    )
                _mig_logger.warning(
                    f"[Phase 5] 남은 이슈: {len(validation.remaining_issues)}개, "
                    f"새 이슈: {len(validation.new_issues)}개"
                )

            _mig_logger.info(f"=== 마이그레이션 완료: success={report.success} ===")
            self.finished.emit(report.success, report)

        except Exception as e:
            if _mig_logger:
                _mig_logger.exception(f"마이그레이션 오류: {e}")
            self.log_message.emit(f"❌ 오류 발생: {str(e)}", STYLE_ERROR)
            self.finished.emit(False, None)
        finally:
            if _mig_logger:
                close_oneclick_logger(_mig_logger)

    def _ensure_rust_core_connector(self):
        """Fail closed unless One-Click is backed by tunnelforge-core."""
        connection = getattr(self.connector, "connection", None)
        facade = getattr(connection, "facade", None)
        connection_id = getattr(connection, "connection_id", None)
        endpoint = getattr(connection, "endpoint", None)
        if not all([connection, facade, connection_id, endpoint]):
            raise RuntimeError(
                "One-Click migration requires a Rust DB Core connector. "
                "Legacy Python DB connections are not supported."
            )

    def _create_empty_report(self) -> MigrationReport:
        """이슈가 없을 때 빈 리포트 생성"""
        return MigrationReport(
            schema=self.schema,
            started_at=self._started_at.isoformat() if self._started_at else "",
            completed_at=datetime.now().isoformat(),
            pre_issue_count=0,
            post_issue_count=0,
            success=True,
            duration_seconds=0.0
        )


class PreflightWidget(QWidget):
    """Pre-flight 검사 결과 위젯"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 제목
        title = QLabel("🔍 사전 검사 (Pre-flight Check)")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        layout.addWidget(title)

        # 설명
        desc = QLabel("마이그레이션 전 필수 요건을 검사합니다.")
        desc.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(desc)

        # 검사 결과 그룹
        self.checks_group = QGroupBox("검사 항목")
        checks_layout = QVBoxLayout(self.checks_group)

        self.check_labels = {}
        check_items = [
            ("permissions", "권한 검사"),
            ("disk_space", "디스크 공간"),
            ("connections", "활성 연결"),
            ("backup", "백업 상태"),
            ("version", "MySQL 버전"),
        ]

        for key, label_text in check_items:
            row = QHBoxLayout()
            status = QLabel("⏳")
            status.setFixedWidth(30)
            label = QLabel(label_text)
            detail = QLabel("")
            detail.setStyleSheet("color: #95a5a6;")

            row.addWidget(status)
            row.addWidget(label)
            row.addWidget(detail, 1)

            self.check_labels[key] = (status, label, detail)
            checks_layout.addLayout(row)

        layout.addWidget(self.checks_group)

        # 결과 요약
        self.result_label = QLabel("")
        self.result_label.setFont(QFont("", 11, QFont.Weight.Bold))
        layout.addWidget(self.result_label)

        layout.addStretch()

    def update_result(self, result: PreflightResult):
        """검사 결과 업데이트"""
        # 각 검사 항목 업데이트
        check_mapping = {
            "권한 검사": "permissions",
            "디스크 공간 검사": "disk_space",
            "활성 연결 검사": "connections",
            "백업 상태 확인": "backup",
            "MySQL 버전 확인": "version",
        }

        for check in result.checks:
            key = check_mapping.get(check.name)
            if key and key in self.check_labels:
                status, label, detail = self.check_labels[key]

                if check.passed:
                    status.setText("✅")
                elif check.severity == CheckSeverity.ERROR:
                    status.setText("❌")
                else:
                    status.setText("⚠️")

                detail.setText(check.message[:50] + "..." if len(check.message) > 50 else check.message)

        # 결과 요약
        if result.passed:
            self.result_label.setText("✅ Pre-flight 검사 통과")
            self.result_label.setStyleSheet(STYLE_SUCCESS)
        else:
            self.result_label.setText(f"❌ Pre-flight 검사 실패 ({result.error_count}개 오류)")
            self.result_label.setStyleSheet(STYLE_ERROR)


class AnalysisWidget(QWidget):
    """분석 결과 위젯"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 제목
        title = QLabel("📊 분석 결과")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        layout.addWidget(title)

        # 요약 카드
        cards_layout = QHBoxLayout()

        self.total_card = self._create_card("발견된 이슈", "0", "#3498db")
        self.auto_card = self._create_card("자동 수정 가능", "0", "#27ae60")
        self.manual_card = self._create_card("수동 검토", "0", "#f39c12")

        cards_layout.addWidget(self.total_card)
        cards_layout.addWidget(self.auto_card)
        cards_layout.addWidget(self.manual_card)

        layout.addLayout(cards_layout)
        layout.addStretch()

    def _create_card(self, title: str, value: str, color: str) -> QFrame:
        """요약 카드 생성"""
        card = QFrame()
        card.setStyleSheet(f"""
            QFrame {{
                background-color: #f8f9fa;
                border-radius: 8px;
                padding: 10px;
            }}
        """)

        layout = QVBoxLayout(card)
        layout.setSpacing(5)

        value_label = QLabel(value)
        value_label.setFont(QFont("", 24, QFont.Weight.Bold))
        value_label.setStyleSheet(f"color: {color};")
        value_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        value_label.setObjectName("value")

        title_label = QLabel(title)
        title_label.setStyleSheet("color: #7f8c8d;")
        title_label.setAlignment(Qt.AlignmentFlag.AlignCenter)

        layout.addWidget(value_label)
        layout.addWidget(title_label)

        return card

    def update_result(self, total: int, auto_fixable: int, manual: int):
        """분석 결과 업데이트"""
        self.total_card.findChild(QLabel, "value").setText(str(total))
        self.auto_card.findChild(QLabel, "value").setText(str(auto_fixable))
        self.manual_card.findChild(QLabel, "value").setText(str(manual))


class ExecutionWidget(QWidget):
    """실행 진행 위젯"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 제목
        title = QLabel("🔧 실행 중")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        layout.addWidget(title)

        # 프로그레스 바
        self.progress_bar = QProgressBar()
        self.progress_bar.setMinimum(0)
        self.progress_bar.setMaximum(100)
        self.progress_bar.setValue(0)
        self.progress_bar.setStyleSheet("""
            QProgressBar {
                border: 1px solid #ddd;
                border-radius: 4px;
                text-align: center;
                height: 25px;
            }
            QProgressBar::chunk {
                background-color: #3498db;
                border-radius: 3px;
            }
        """)
        layout.addWidget(self.progress_bar)

        # 현재 작업 메시지
        self.status_label = QLabel("대기 중...")
        self.status_label.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(self.status_label)

        # 로그 영역
        log_group = QGroupBox("실행 로그")
        log_layout = QVBoxLayout(log_group)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.log_text.setMinimumHeight(200)
        self.log_text.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
                background-color: #2c3e50;
                color: #ecf0f1;
                border-radius: 4px;
            }
        """)
        log_layout.addWidget(self.log_text)

        layout.addWidget(log_group)

    def update_progress(self, percent: int, message: str):
        """진행률 업데이트"""
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)

    def append_log(self, message: str, style: str = ""):
        """로그 추가"""
        # 스타일에 따른 색상 적용
        color = "#ecf0f1"  # 기본 흰색
        if style == STYLE_SUCCESS:
            color = "#2ecc71"
        elif style == STYLE_ERROR:
            color = "#e74c3c"
        elif style == STYLE_WARNING:
            color = "#f39c12"
        elif style == STYLE_INFO:
            color = "#3498db"
        elif style == STYLE_MUTED:
            color = "#95a5a6"

        self.log_text.append(f'<span style="color: {color};">{message}</span>')
        # 자동 스크롤
        scrollbar = self.log_text.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())


class ResultWidget(QWidget):
    """결과 위젯"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._report: Optional[MigrationReport] = None
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        # 제목
        self.title_label = QLabel("📋 마이그레이션 결과")
        self.title_label.setFont(QFont("", 14, QFont.Weight.Bold))
        layout.addWidget(self.title_label)

        # 결과 요약
        self.summary_group = QGroupBox("요약")
        summary_layout = QVBoxLayout(self.summary_group)

        self.result_status = QLabel("")
        self.result_status.setFont(QFont("", 12, QFont.Weight.Bold))
        summary_layout.addWidget(self.result_status)

        self.stats_label = QLabel("")
        summary_layout.addWidget(self.stats_label)

        self.log_path_label = QLabel("")
        self.log_path_label.setStyleSheet("color: #7f8c8d; font-size: 10px;")
        self.log_path_label.setWordWrap(True)
        summary_layout.addWidget(self.log_path_label)

        layout.addWidget(self.summary_group)

        # 리포트 다운로드 버튼
        btn_layout = QHBoxLayout()

        self.btn_download_html = QPushButton("📄 HTML 리포트 다운로드")
        self.btn_download_html.clicked.connect(self._download_html)
        btn_layout.addWidget(self.btn_download_html)

        self.btn_download_json = QPushButton("📊 JSON 리포트 다운로드")
        self.btn_download_json.clicked.connect(self._download_json)
        btn_layout.addWidget(self.btn_download_json)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

    def update_result(self, report: MigrationReport):
        """결과 업데이트"""
        self._report = report

        if report.success:
            self.result_status.setText("✅ 마이그레이션 성공!")
            self.result_status.setStyleSheet(STYLE_SUCCESS)
        else:
            self.result_status.setText("⚠️ 마이그레이션 완료 (일부 이슈 남음)")
            self.result_status.setStyleSheet(STYLE_WARNING)

        stats = (
            f"• 수정 전 이슈: {report.pre_issue_count}개\n"
            f"• 해결된 이슈: {len(report.fixed_issues)}개\n"
            f"• 남은 이슈: {len(report.remaining_issues)}개\n"
            f"• 새 이슈: {len(report.new_issues)}개\n"
            f"• 소요 시간: {report.duration_seconds:.1f}초"
        )
        self.stats_label.setText(stats)

        if report.execution_log_path:
            self.log_path_label.setText(f"📋 실행 로그: {report.execution_log_path}")
        else:
            self.log_path_label.setText("")

    def _download_html(self):
        """HTML 리포트 다운로드"""
        if not self._report:
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "HTML 리포트 저장",
            f"migration_report_{self._report.schema}.html",
            "HTML Files (*.html)"
        )

        if path:
            validator = PostMigrationValidator(None)  # connector 불필요
            validator.export_report_html(self._report, path)
            QMessageBox.information(self, "저장 완료", f"리포트가 저장되었습니다:\n{path}")

    def _download_json(self):
        """JSON 리포트 다운로드"""
        if not self._report:
            return

        path, _ = QFileDialog.getSaveFileName(
            self,
            "JSON 리포트 저장",
            f"migration_report_{self._report.schema}.json",
            "JSON Files (*.json)"
        )

        if path:
            validator = PostMigrationValidator(None)
            validator.export_report_json(self._report, path)
            QMessageBox.information(self, "저장 완료", f"리포트가 저장되었습니다:\n{path}")


class ExecutionPlanWidget(QWidget):
    """실행 계획 확인 위젯 (Phase 3 완료 후 사용자 확인)"""

    start_requested = pyqtSignal()  # "실행 시작" 버튼 클릭

    def __init__(self, parent=None):
        super().__init__(parent)
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)

        title = QLabel("📋 실행 계획 확인")
        title.setFont(QFont("", 14, QFont.Weight.Bold))
        layout.addWidget(title)

        desc = QLabel("아래 내용을 확인 후 실행을 시작하세요.")
        desc.setStyleSheet("color: #7f8c8d;")
        layout.addWidget(desc)

        # 자동 실행 대상
        self.auto_group = QGroupBox("🔧 자동 실행 대상 (0개)")
        auto_layout = QVBoxLayout(self.auto_group)
        self.auto_text = QTextEdit()
        self.auto_text.setReadOnly(True)
        self.auto_text.setMaximumHeight(120)
        self.auto_text.setStyleSheet("font-size: 11px;")
        auto_layout.addWidget(self.auto_text)
        layout.addWidget(self.auto_group)

        # 조치 불필요 (SKIP)
        self.skip_group = QGroupBox("ℹ️ 조치 불필요 (MySQL 8.4에서 자동 처리) (0개)")
        skip_layout = QVBoxLayout(self.skip_group)
        self.skip_label = QLabel("")
        self.skip_label.setWordWrap(True)
        self.skip_label.setStyleSheet("color: #7f8c8d; font-size: 11px;")
        skip_layout.addWidget(self.skip_label)
        layout.addWidget(self.skip_group)

        # 마이그레이션 후 처리 필요
        self.manual_group = QGroupBox("📋 마이그레이션 후 수동 처리 필요 (0개)")
        manual_layout = QVBoxLayout(self.manual_group)
        manual_note = QLabel("⚠️ 아래 항목은 DB가 아닌 애플리케이션 또는 설정 변경이 필요합니다.")
        manual_note.setStyleSheet("color: #e67e22; font-size: 11px;")
        manual_note.setWordWrap(True)
        manual_layout.addWidget(manual_note)
        self.manual_text = QTextEdit()
        self.manual_text.setReadOnly(True)
        self.manual_text.setMaximumHeight(100)
        self.manual_text.setStyleSheet("font-size: 11px;")
        manual_layout.addWidget(self.manual_text)
        layout.addWidget(self.manual_group)

        # 실행 버튼
        btn_layout = QHBoxLayout()
        self.btn_execute = QPushButton("▶ 실행 시작")
        self.btn_execute.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 8px 24px;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover { background-color: #219a52; }
        """)
        self.btn_execute.clicked.connect(self.start_requested)
        btn_layout.addWidget(self.btn_execute)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)

        layout.addStretch()

    def update_plan(self, steps, summary):
        """실행 계획 데이터로 UI 업데이트"""
        from src.core.migration_fix_wizard import FixStrategy

        auto_items = []
        skip_items = []
        manual_items = []

        for step in steps:
            if not step.selected_option:
                manual_items.append(f"• {step.location}: {step.description}")
                continue

            strategy = step.selected_option.strategy
            sql = step.selected_option.sql_template or ""

            if strategy == FixStrategy.SKIP:
                skip_items.append(f"• {step.location}: {step.selected_option.description}")
            elif strategy == FixStrategy.COLLATION_FK_SAFE:
                # FK 안전 변경: sql_template이 주석("--")으로 시작하므로 자동 실행으로 분류
                auto_items.append(f"• {step.location}: {step.selected_option.label}")
            elif not sql or sql.startswith("--"):
                # SQL이 없거나 주석인 경우 → 마이그레이션 후 수동 처리
                manual_items.append(
                    f"• {step.location}: {step.selected_option.description or step.description}"
                )
            else:
                auto_items.append(f"• {step.location}: {step.selected_option.label}")

        self.auto_group.setTitle(f"🔧 자동 실행 대상 ({len(auto_items)}개)")
        self.auto_text.setPlainText("\n".join(auto_items) if auto_items else "(없음)")

        self.skip_group.setTitle(f"ℹ️ 조치 불필요 (MySQL 8.4에서 자동 처리) ({len(skip_items)}개)")
        self.skip_label.setText("\n".join(skip_items) if skip_items else "(없음)")
        self.skip_group.setVisible(bool(skip_items))

        self.manual_group.setTitle(f"📋 마이그레이션 후 수동 처리 필요 ({len(manual_items)}개)")
        self.manual_text.setPlainText("\n".join(manual_items) if manual_items else "(없음)")
        self.manual_group.setVisible(bool(manual_items))

        # 실행 버튼 활성화 여부
        self.btn_execute.setEnabled(bool(auto_items))
        if not auto_items:
            self.btn_execute.setText("실행할 항목 없음")


class OneClickMigrationDialog(QDialog):
    """One-Click 마이그레이션 다이얼로그"""

    def __init__(self, parent, connector: MySQLConnector, schema: str):
        super().__init__(parent)
        self.connector = connector
        self.schema = schema
        self.worker: Optional[OneClickMigrationWorker] = None
        self._setup_ui()

    def _setup_ui(self):
        self.setWindowTitle(f"🚀 One-Click 마이그레이션 - {self.schema}")
        self.setMinimumSize(750, 600)

        layout = QVBoxLayout(self)

        # Phase 인디케이터
        self.phase_indicator = self._create_phase_indicator()
        layout.addWidget(self.phase_indicator)

        # 스택 위젯 (4개 화면)
        self.stack = QStackedWidget()

        self.preflight_widget = PreflightWidget()
        self.analysis_widget = AnalysisWidget()
        self.execution_plan_widget = ExecutionPlanWidget()
        self.execution_widget = ExecutionWidget()
        self.result_widget = ResultWidget()

        self.stack.addWidget(self.preflight_widget)
        self.stack.addWidget(self.analysis_widget)
        self.stack.addWidget(self.execution_plan_widget)
        self.stack.addWidget(self.execution_widget)
        self.stack.addWidget(self.result_widget)

        layout.addWidget(self.stack, 1)

        # 옵션
        options_layout = QHBoxLayout()

        self.chk_dry_run = QCheckBox("Dry-run (실제 실행하지 않음)")
        self.chk_dry_run.setToolTip("체크하면 실제 SQL을 실행하지 않고 시뮬레이션만 합니다.")
        self.chk_dry_run.setChecked(True)
        options_layout.addWidget(self.chk_dry_run)

        self.chk_backup = QCheckBox("백업 완료 확인")
        self.chk_backup.setToolTip("체크하면 백업 완료로 간주합니다.")
        options_layout.addWidget(self.chk_backup)

        options_layout.addStretch()
        layout.addLayout(options_layout)

        # 버튼
        btn_layout = QHBoxLayout()

        self.btn_start = QPushButton("🚀 시작")
        self.btn_start.setStyleSheet("""
            QPushButton {
                background-color: #27ae60;
                color: white;
                font-weight: bold;
                padding: 10px 30px;
                border-radius: 4px;
                border: none;
            }
            QPushButton:hover {
                background-color: #219a52;
            }
            QPushButton:disabled {
                background-color: #95a5a6;
            }
        """)
        self.btn_start.clicked.connect(self.start_migration)
        btn_layout.addWidget(self.btn_start)

        self.btn_cancel = QPushButton("취소")
        self.btn_cancel.clicked.connect(self.cancel_migration)
        self.btn_cancel.setEnabled(False)
        btn_layout.addWidget(self.btn_cancel)

        btn_layout.addStretch()

        self.btn_close = QPushButton("닫기")
        self.btn_close.clicked.connect(self.close)
        btn_layout.addWidget(self.btn_close)

        layout.addLayout(btn_layout)

    def _create_phase_indicator(self) -> QWidget:
        """단계 표시 위젯 생성"""
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setSpacing(10)

        phases = [
            ("preflight", "1. 사전검사"),
            ("analysis", "2. 분석"),
            ("recommendation", "3. 권장"),
            ("execution", "4. 실행"),
            ("validation", "5. 검증"),
        ]

        self.phase_labels = {}

        for key, text in phases:
            label = QLabel(text)
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            label.setStyleSheet("""
                QLabel {
                    padding: 8px 15px;
                    background-color: #ecf0f1;
                    border-radius: 4px;
                    color: #7f8c8d;
                }
            """)
            self.phase_labels[key] = label
            layout.addWidget(label)

        return widget

    def _update_phase_indicator(self, current_phase: str):
        """단계 표시 업데이트"""
        phase_order = ["preflight", "analysis", "recommendation", "execution", "validation"]

        try:
            current_idx = phase_order.index(current_phase)
        except ValueError:
            current_idx = -1

        for i, phase in enumerate(phase_order):
            label = self.phase_labels[phase]

            if i < current_idx:
                # 완료된 단계
                label.setStyleSheet("""
                    QLabel {
                        padding: 8px 15px;
                        background-color: #27ae60;
                        border-radius: 4px;
                        color: white;
                    }
                """)
            elif i == current_idx:
                # 현재 단계
                label.setStyleSheet("""
                    QLabel {
                        padding: 8px 15px;
                        background-color: #3498db;
                        border-radius: 4px;
                        color: white;
                        font-weight: bold;
                    }
                """)
            else:
                # 대기 중인 단계
                label.setStyleSheet("""
                    QLabel {
                        padding: 8px 15px;
                        background-color: #ecf0f1;
                        border-radius: 4px;
                        color: #7f8c8d;
                    }
                """)

    def start_migration(self):
        """마이그레이션 시작"""
        self.btn_start.setEnabled(False)
        self.btn_cancel.setEnabled(True)
        self.chk_dry_run.setEnabled(False)
        self.chk_backup.setEnabled(False)

        # UI 초기화 (재실행 시 이전 로그 제거)
        self.execution_widget.log_text.clear()
        self.execution_widget.update_progress(0, "시작 중...")

        # 실행 위젯으로 전환
        self.stack.setCurrentWidget(self.execution_widget)

        # Worker 시작
        self.worker = OneClickMigrationWorker(
            self.connector,
            self.schema,
            dry_run=self.chk_dry_run.isChecked(),
            backup_confirmed=self.chk_backup.isChecked()
        )

        self.worker.phase_changed.connect(self._on_phase_changed)
        self.worker.progress.connect(self._on_progress)
        self.worker.log_message.connect(self._on_log)
        self.worker.preflight_result.connect(self._on_preflight_result)
        self.worker.analysis_result.connect(self._on_analysis_result)
        self.worker.execution_plan_ready.connect(self._on_execution_plan_ready)
        self.worker.finished.connect(self._on_finished)

        self.worker.start()

    def cancel_migration(self):
        """마이그레이션 취소"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self,
                "취소 확인",
                "마이그레이션을 취소하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )

            if reply == QMessageBox.StandardButton.Yes:
                self.worker.cancel()
                self.btn_cancel.setEnabled(False)

    def closeEvent(self, event):
        """다이얼로그 닫기 이벤트 처리"""
        if self.worker and self.worker.isRunning():
            reply = QMessageBox.question(
                self, "작업 중",
                "마이그레이션이 실행 중입니다. 종료하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            self.worker.quit()
            if not self.worker.wait(3000):
                self.worker.terminate()
                self.worker.wait(1000)
        event.accept()

    def _on_phase_changed(self, phase: str, phase_name: str):
        """단계 변경 핸들러"""
        self._update_phase_indicator(phase)
        # One-Click 모드: execution_widget에서 전체 로그를 계속 표시
        # phase_indicator가 현재 단계를 이미 표시하므로 화면 전환 불필요

    def _on_progress(self, percent: int, message: str):
        """진행률 핸들러"""
        self.execution_widget.update_progress(percent, message)

    def _on_log(self, message: str, style: str):
        """로그 핸들러"""
        self.execution_widget.append_log(message, style)

    def _on_preflight_result(self, result: PreflightResult):
        """Pre-flight 결과 핸들러"""
        self.preflight_widget.update_result(result)

    def _on_analysis_result(self, total: int, auto_fixable: int, manual: int):
        """분석 결과 핸들러"""
        self.analysis_widget.update_result(total, auto_fixable, manual)

    def _on_execution_plan_ready(self, steps, summary):
        """실행 계획 준비 완료 핸들러 (Phase 3→4 사이 일시 정지)"""
        self.execution_plan_widget.update_plan(steps, summary)
        self.stack.setCurrentWidget(self.execution_plan_widget)

        # "실행 시작" 버튼 연결 (한 번만 연결하기 위해 disconnect 후 connect)
        try:
            self.execution_plan_widget.start_requested.disconnect()
        except (TypeError, RuntimeError):
            pass
        self.execution_plan_widget.start_requested.connect(self._on_start_execution_confirmed)

    def _on_start_execution_confirmed(self):
        """사용자가 실행 계획 확인 후 "실행 시작" 클릭"""
        # 실행 로그 위젯으로 전환
        self.stack.setCurrentWidget(self.execution_widget)
        # Worker 재개
        if self.worker:
            self.worker.resume_execution()

    def _on_finished(self, success: bool, report):
        """완료 핸들러"""
        self.btn_start.setEnabled(True)
        self.btn_cancel.setEnabled(False)
        self.chk_dry_run.setEnabled(True)
        self.chk_backup.setEnabled(True)

        if report:
            self.result_widget.update_result(report)
            self.stack.setCurrentWidget(self.result_widget)

            # 모든 단계 완료 표시
            for phase in self.phase_labels.values():
                phase.setStyleSheet("""
                    QLabel {
                        padding: 8px 15px;
                        background-color: #27ae60;
                        border-radius: 4px;
                        color: white;
                    }
                """)
