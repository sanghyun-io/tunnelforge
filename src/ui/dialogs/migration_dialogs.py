"""
마이그레이션 분석 다이얼로그
- 스키마 분석 (고아 레코드, 호환성 이슈)
- FK 관계 시각화
- dry-run 및 실제 정리 작업
"""
import os
import json
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QLineEdit, QSpinBox, QPushButton, QComboBox,
    QCheckBox, QListWidget, QListWidgetItem, QGroupBox,
    QMessageBox, QProgressBar, QApplication,
    QRadioButton, QButtonGroup, QWidget, QTabWidget,
    QTextEdit, QTreeWidget, QTreeWidgetItem, QSplitter,
    QTableWidget, QTableWidgetItem, QHeaderView, QFrame,
    QFileDialog, QMenu
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QColor
from typing import List, Optional, Dict
from datetime import datetime

from src.core.db_connector import MySQLConnector
from src.core.migration_analyzer import (
    MigrationAnalyzer, AnalysisResult, OrphanRecord,
    CompatibilityIssue, CleanupAction, ActionType, IssueType
)
from src.ui.workers.migration_worker import MigrationAnalyzerWorker, CleanupWorker
from src.core.logger import get_logger

logger = get_logger('migration_dialogs')


class MigrationAnalyzerDialog(QDialog):
    """마이그레이션 분석 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None):
        super().__init__(parent)
        self.setWindowTitle("🔄 마이그레이션 분석기")
        self.resize(1000, 700)

        self.connector = connector
        self.config_manager = config_manager
        self.analysis_result: Optional[AnalysisResult] = None
        self.worker: Optional[MigrationAnalyzerWorker] = None
        self.cleanup_worker: Optional[CleanupWorker] = None

        self.init_ui()
        self.load_schemas()

    def init_ui(self):
        layout = QVBoxLayout(self)

        # --- 상단: 스키마 선택 및 분석 옵션 ---
        top_group = QGroupBox("분석 설정")
        top_layout = QVBoxLayout(top_group)

        # 스키마 선택
        schema_layout = QHBoxLayout()
        schema_layout.addWidget(QLabel("스키마:"))
        self.combo_schema = QComboBox()
        self.combo_schema.setMinimumWidth(200)
        schema_layout.addWidget(self.combo_schema)
        schema_layout.addStretch()
        top_layout.addLayout(schema_layout)

        # 분석 옵션 체크박스들 (행 1: 기존 검사)
        options_layout = QHBoxLayout()

        self.chk_orphans = QCheckBox("고아 레코드 검사")
        self.chk_orphans.setChecked(True)
        self.chk_orphans.setToolTip("FK 관계에서 부모가 없는 자식 레코드 탐지")

        self.chk_charset = QCheckBox("문자셋 이슈")
        self.chk_charset.setChecked(True)
        self.chk_charset.setToolTip("utf8mb3 사용 테이블/컬럼 확인")

        self.chk_keywords = QCheckBox("예약어 충돌")
        self.chk_keywords.setChecked(True)
        self.chk_keywords.setToolTip("MySQL 8.4 새 예약어와 충돌하는 이름 확인")

        self.chk_routines = QCheckBox("저장 프로시저/함수")
        self.chk_routines.setChecked(True)
        self.chk_routines.setToolTip("deprecated 함수 사용 여부 확인")

        self.chk_sql_mode = QCheckBox("SQL 모드")
        self.chk_sql_mode.setChecked(True)
        self.chk_sql_mode.setToolTip("deprecated SQL 모드 사용 여부 확인")

        options_layout.addWidget(self.chk_orphans)
        options_layout.addWidget(self.chk_charset)
        options_layout.addWidget(self.chk_keywords)
        options_layout.addWidget(self.chk_routines)
        options_layout.addWidget(self.chk_sql_mode)
        options_layout.addStretch()

        top_layout.addLayout(options_layout)

        # 분석 옵션 체크박스들 (행 2: MySQL 8.4 Upgrade Checker)
        options_layout2 = QHBoxLayout()

        self.chk_auth_plugins = QCheckBox("인증 플러그인")
        self.chk_auth_plugins.setChecked(True)
        self.chk_auth_plugins.setToolTip("mysql_native_password, sha256_password 사용자 확인")

        self.chk_zerofill = QCheckBox("ZEROFILL")
        self.chk_zerofill.setChecked(True)
        self.chk_zerofill.setToolTip("ZEROFILL 속성 사용 컬럼 확인")

        self.chk_float_precision = QCheckBox("FLOAT(M,D)")
        self.chk_float_precision.setChecked(True)
        self.chk_float_precision.setToolTip("FLOAT(M,D), DOUBLE(M,D) deprecated 구문 확인")

        self.chk_fk_name_length = QCheckBox("FK 이름 길이")
        self.chk_fk_name_length.setChecked(True)
        self.chk_fk_name_length.setToolTip("FK 이름 64자 초과 확인")

        options_layout2.addWidget(QLabel("🔧 8.4 검사:"))
        options_layout2.addWidget(self.chk_auth_plugins)
        options_layout2.addWidget(self.chk_zerofill)
        options_layout2.addWidget(self.chk_float_precision)
        options_layout2.addWidget(self.chk_fk_name_length)
        options_layout2.addStretch()

        top_layout.addLayout(options_layout2)

        # 분석 버튼
        btn_layout = QHBoxLayout()
        self.btn_analyze = QPushButton("🔍 분석 시작")
        self.btn_analyze.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white; font-weight: bold;
                padding: 8px 20px; border-radius: 4px; border: none;
                font-size: 14px;
            }
            QPushButton:hover { background-color: #2980b9; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_analyze.clicked.connect(self.start_analysis)
        btn_layout.addWidget(self.btn_analyze)

        # 저장/불러오기 버튼
        self.btn_save = QPushButton("💾 결과 저장")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self.save_analysis_result)
        btn_layout.addWidget(self.btn_save)

        self.btn_load = QPushButton("📂 결과 불러오기")
        self.btn_load.clicked.connect(self.load_analysis_result)
        btn_layout.addWidget(self.btn_load)

        btn_layout.addStretch()
        top_layout.addLayout(btn_layout)

        layout.addWidget(top_group)

        # --- 진행 상황 ---
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        layout.addWidget(self.progress_bar)

        # --- 탭 위젯: 결과 표시 ---
        self.tab_widget = QTabWidget()

        # 탭 1: 개요
        self.tab_overview = QWidget()
        self.init_overview_tab()
        self.tab_widget.addTab(self.tab_overview, "📊 개요")

        # 탭 2: 고아 레코드
        self.tab_orphans = QWidget()
        self.init_orphans_tab()
        self.tab_widget.addTab(self.tab_orphans, "🔗 고아 레코드")

        # 탭 3: 호환성 이슈
        self.tab_compatibility = QWidget()
        self.init_compatibility_tab()
        self.tab_widget.addTab(self.tab_compatibility, "⚠️ 호환성")

        # 탭 4: FK 트리
        self.tab_fk_tree = QWidget()
        self.init_fk_tree_tab()
        self.tab_widget.addTab(self.tab_fk_tree, "🌳 FK 관계")

        # 탭 5: 로그
        self.tab_log = QWidget()
        self.init_log_tab()
        self.tab_widget.addTab(self.tab_log, "📝 로그")

        layout.addWidget(self.tab_widget)

        # --- 하단 버튼 ---
        bottom_layout = QHBoxLayout()

        self.btn_close = QPushButton("닫기")
        self.btn_close.setStyleSheet("""
            QPushButton {
                background-color: #ecf0f1; color: #2c3e50;
                padding: 8px 20px; border-radius: 4px; border: 1px solid #bdc3c7;
            }
            QPushButton:hover { background-color: #d5dbdb; }
        """)
        self.btn_close.clicked.connect(self.close)

        bottom_layout.addStretch()
        bottom_layout.addWidget(self.btn_close)

        layout.addLayout(bottom_layout)

    def init_overview_tab(self):
        """개요 탭 초기화"""
        layout = QVBoxLayout(self.tab_overview)

        # 요약 정보
        self.lbl_summary = QLabel("분석을 시작하세요.")
        self.lbl_summary.setWordWrap(True)
        self.lbl_summary.setStyleSheet("""
            QLabel {
                background-color: #f8f9fa;
                padding: 15px;
                border-radius: 8px;
                font-size: 13px;
            }
        """)
        layout.addWidget(self.lbl_summary)

        # 통계 테이블
        self.table_stats = QTableWidget()
        self.table_stats.setColumnCount(2)
        self.table_stats.setHorizontalHeaderLabels(["항목", "값"])
        self.table_stats.horizontalHeader().setStretchLastSection(True)
        self.table_stats.verticalHeader().setVisible(False)
        layout.addWidget(self.table_stats)

    def init_orphans_tab(self):
        """고아 레코드 탭 초기화"""
        layout = QVBoxLayout(self.tab_orphans)

        # 고아 레코드 테이블
        self.table_orphans = QTableWidget()
        self.table_orphans.setColumnCount(6)
        self.table_orphans.setHorizontalHeaderLabels([
            "자식 테이블", "자식 컬럼", "부모 테이블", "부모 컬럼", "고아 수", "샘플 값"
        ])
        self.table_orphans.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_orphans.horizontalHeader().setStretchLastSection(True)
        self.table_orphans.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table_orphans.itemSelectionChanged.connect(self.on_orphan_selected)
        layout.addWidget(self.table_orphans)

        # 정리 옵션
        cleanup_group = QGroupBox("정리 작업")
        cleanup_layout = QVBoxLayout(cleanup_group)

        # 조치 선택
        action_layout = QHBoxLayout()
        action_layout.addWidget(QLabel("조치:"))

        self.btn_group_action = QButtonGroup(self)
        self.radio_delete = QRadioButton("삭제 (DELETE)")
        self.radio_delete.setChecked(True)
        self.radio_set_null = QRadioButton("NULL로 설정 (SET NULL)")

        self.btn_group_action.addButton(self.radio_delete)
        self.btn_group_action.addButton(self.radio_set_null)

        action_layout.addWidget(self.radio_delete)
        action_layout.addWidget(self.radio_set_null)
        action_layout.addStretch()
        cleanup_layout.addLayout(action_layout)

        # SQL 미리보기
        self.txt_cleanup_sql = QTextEdit()
        self.txt_cleanup_sql.setReadOnly(True)
        self.txt_cleanup_sql.setMaximumHeight(100)
        self.txt_cleanup_sql.setPlaceholderText("정리할 레코드를 선택하면 SQL이 표시됩니다...")
        self.txt_cleanup_sql.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                background-color: #2d2d2d;
                color: #f8f8f2;
                border-radius: 4px;
            }
        """)
        cleanup_layout.addWidget(self.txt_cleanup_sql)

        # 버튼들
        btn_layout = QHBoxLayout()

        self.btn_dry_run = QPushButton("🔍 Dry-Run (미리보기)")
        self.btn_dry_run.setStyleSheet("""
            QPushButton {
                background-color: #f39c12; color: white; font-weight: bold;
                padding: 8px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #e67e22; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_dry_run.clicked.connect(lambda: self.execute_cleanup(dry_run=True))
        self.btn_dry_run.setEnabled(False)

        self.btn_execute = QPushButton("⚡ 실행")
        self.btn_execute.setStyleSheet("""
            QPushButton {
                background-color: #e74c3c; color: white; font-weight: bold;
                padding: 8px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #c0392b; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_execute.clicked.connect(lambda: self.execute_cleanup(dry_run=False))
        self.btn_execute.setEnabled(False)

        self.btn_select_all = QPushButton("전체 선택")
        self.btn_select_all.clicked.connect(self.select_all_orphans)

        btn_layout.addWidget(self.btn_select_all)
        btn_layout.addStretch()
        btn_layout.addWidget(self.btn_dry_run)
        btn_layout.addWidget(self.btn_execute)

        cleanup_layout.addLayout(btn_layout)
        layout.addWidget(cleanup_group)

    def init_compatibility_tab(self):
        """호환성 이슈 탭 초기화"""
        layout = QVBoxLayout(self.tab_compatibility)

        # 필터
        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("필터:"))

        self.chk_filter_error = QCheckBox("Error")
        self.chk_filter_error.setChecked(True)
        self.chk_filter_error.stateChanged.connect(self.filter_compatibility_issues)

        self.chk_filter_warning = QCheckBox("Warning")
        self.chk_filter_warning.setChecked(True)
        self.chk_filter_warning.stateChanged.connect(self.filter_compatibility_issues)

        self.chk_filter_info = QCheckBox("Info")
        self.chk_filter_info.setChecked(True)
        self.chk_filter_info.stateChanged.connect(self.filter_compatibility_issues)

        filter_layout.addWidget(self.chk_filter_error)
        filter_layout.addWidget(self.chk_filter_warning)
        filter_layout.addWidget(self.chk_filter_info)
        filter_layout.addStretch()

        # 자동 수정 위저드 버튼
        self.btn_auto_fix = QPushButton("🔧 자동 수정 위저드")
        self.btn_auto_fix.setStyleSheet("""
            QPushButton {
                background-color: #9b59b6; color: white; font-weight: bold;
                padding: 6px 16px; border-radius: 4px; border: none;
            }
            QPushButton:hover { background-color: #8e44ad; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        self.btn_auto_fix.setToolTip("호환성 이슈를 대화형 위저드로 자동 수정합니다.")
        self.btn_auto_fix.setEnabled(False)  # 분석 완료 후 활성화
        self.btn_auto_fix.clicked.connect(self.open_fix_wizard)
        filter_layout.addWidget(self.btn_auto_fix)

        layout.addLayout(filter_layout)

        # 이슈 테이블
        self.table_issues = QTableWidget()
        self.table_issues.setColumnCount(5)
        self.table_issues.setHorizontalHeaderLabels([
            "심각도", "유형", "위치", "설명", "권장 조치"
        ])
        self.table_issues.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table_issues.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table_issues)

    def init_fk_tree_tab(self):
        """FK 관계 트리 탭 초기화"""
        layout = QVBoxLayout(self.tab_fk_tree)

        # 트리 위젯
        self.tree_fk = QTreeWidget()
        self.tree_fk.setHeaderLabels(["테이블 (부모 → 자식)"])
        self.tree_fk.setAlternatingRowColors(True)
        layout.addWidget(self.tree_fk)

        # 텍스트 뷰 (ASCII 트리)
        self.txt_fk_tree = QTextEdit()
        self.txt_fk_tree.setReadOnly(True)
        self.txt_fk_tree.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 12px;
            }
        """)
        layout.addWidget(self.txt_fk_tree)

    def init_log_tab(self):
        """로그 탭 초기화"""
        layout = QVBoxLayout(self.tab_log)

        self.txt_log = QTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setStyleSheet("""
            QTextEdit {
                font-family: 'Consolas', 'Monaco', monospace;
                font-size: 11px;
            }
        """)
        layout.addWidget(self.txt_log)

        # 로그 저장 버튼
        btn_layout = QHBoxLayout()
        btn_save_log = QPushButton("로그 저장")
        btn_save_log.clicked.connect(self.save_log)
        btn_layout.addStretch()
        btn_layout.addWidget(btn_save_log)
        layout.addLayout(btn_layout)

    def load_schemas(self):
        """스키마 목록 로드"""
        if not self.connector:
            return

        schemas = self.connector.get_schemas()
        self.combo_schema.clear()
        for schema in schemas:
            self.combo_schema.addItem(schema)

    def add_log(self, message: str):
        """로그 추가"""
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.txt_log.append(f"[{timestamp}] {message}")

    def save_log(self):
        """로그 저장"""
        from PyQt6.QtWidgets import QFileDialog

        filename, _ = QFileDialog.getSaveFileName(
            self, "로그 저장", f"migration_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt",
            "Text Files (*.txt)"
        )
        if filename:
            with open(filename, 'w', encoding='utf-8') as f:
                f.write(self.txt_log.toPlainText())
            QMessageBox.information(self, "저장 완료", f"로그가 저장되었습니다:\n{filename}")

    def set_ui_enabled(self, enabled: bool):
        """UI 활성화/비활성화"""
        self.btn_analyze.setEnabled(enabled)
        self.combo_schema.setEnabled(enabled)
        self.chk_orphans.setEnabled(enabled)
        self.chk_charset.setEnabled(enabled)
        self.chk_keywords.setEnabled(enabled)
        self.chk_routines.setEnabled(enabled)
        self.chk_sql_mode.setEnabled(enabled)
        # MySQL 8.4 Upgrade Checker 옵션
        self.chk_auth_plugins.setEnabled(enabled)
        self.chk_zerofill.setEnabled(enabled)
        self.chk_float_precision.setEnabled(enabled)
        self.chk_fk_name_length.setEnabled(enabled)

    def start_analysis(self):
        """분석 시작"""
        schema = self.combo_schema.currentText()
        if not schema:
            QMessageBox.warning(self, "오류", "스키마를 선택하세요.")
            return

        self.set_ui_enabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # 무한 프로그레스

        self.add_log(f"📊 스키마 '{schema}' 분석 시작...")

        # 워커 생성 및 시작
        self.worker = MigrationAnalyzerWorker(
            connector=self.connector,
            schema=schema,
            check_orphans=self.chk_orphans.isChecked(),
            check_charset=self.chk_charset.isChecked(),
            check_keywords=self.chk_keywords.isChecked(),
            check_routines=self.chk_routines.isChecked(),
            check_sql_mode=self.chk_sql_mode.isChecked(),
            # MySQL 8.4 Upgrade Checker 옵션
            check_auth_plugins=self.chk_auth_plugins.isChecked(),
            check_zerofill=self.chk_zerofill.isChecked(),
            check_float_precision=self.chk_float_precision.isChecked(),
            check_fk_name_length=self.chk_fk_name_length.isChecked(),
            # 추가 검사 (기본 활성화)
            check_invalid_dates=True,
            check_year2=True,
            check_deprecated_engines=True,
            check_enum_empty=True,
            check_timestamp_range=True
        )

        self.worker.progress.connect(self.add_log)
        self.worker.analysis_complete.connect(self.on_analysis_complete)
        self.worker.finished.connect(self.on_analysis_finished)
        self.worker.start()

    def on_analysis_complete(self, result: AnalysisResult):
        """분석 완료 시"""
        try:
            self.analysis_result = result
            self.update_overview(result)
            self.update_orphans_table(result.orphan_records)
            self.update_compatibility_table(result.compatibility_issues)
            self.update_fk_tree(result.fk_tree, result.schema)
            # 저장 버튼 활성화
            self.btn_save.setEnabled(True)
            # 자동 수정 버튼 활성화 (호환성 이슈가 있을 때만)
            self.btn_auto_fix.setEnabled(len(result.compatibility_issues) > 0)
        except Exception as e:
            logger.error(f"분석 결과 UI 업데이트 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"분석 결과 표시 중 오류 발생:\n{e}")

    def on_analysis_finished(self, success: bool, message: str):
        """분석 종료 시"""
        self.set_ui_enabled(True)
        self.progress_bar.setVisible(False)

        if success:
            self.add_log(f"✅ {message}")
        else:
            self.add_log(f"❌ {message}")
            QMessageBox.critical(self, "분석 오류", message)

    def update_overview(self, result: AnalysisResult):
        """개요 탭 업데이트"""
        # 요약 텍스트
        orphan_count = sum(o.orphan_count for o in result.orphan_records)
        error_count = sum(1 for i in result.compatibility_issues if i.severity == "error")
        warning_count = sum(1 for i in result.compatibility_issues if i.severity == "warning")

        summary = f"""
<h3>📊 분석 결과: {result.schema}</h3>
<p><b>분석 시각:</b> {result.analyzed_at}</p>
<p><b>테이블 수:</b> {result.total_tables}개</p>
<p><b>FK 관계:</b> {result.total_fk_relations}개</p>
<hr>
<p><b>🔗 고아 레코드:</b> {len(result.orphan_records)}개 FK 관계에서 총 {orphan_count:,}개 발견</p>
<p><b>❌ 오류:</b> {error_count}개</p>
<p><b>⚠️ 경고:</b> {warning_count}개</p>
"""
        self.lbl_summary.setText(summary)

        # 통계 테이블
        stats = [
            ("스키마", result.schema),
            ("분석 시각", result.analyzed_at),
            ("테이블 수", str(result.total_tables)),
            ("FK 관계 수", str(result.total_fk_relations)),
            ("고아 레코드 FK 관계", str(len(result.orphan_records))),
            ("총 고아 레코드 수", f"{orphan_count:,}"),
            ("호환성 오류", str(error_count)),
            ("호환성 경고", str(warning_count)),
        ]

        self.table_stats.setRowCount(len(stats))
        for i, (key, value) in enumerate(stats):
            self.table_stats.setItem(i, 0, QTableWidgetItem(key))
            self.table_stats.setItem(i, 1, QTableWidgetItem(value))

    def update_orphans_table(self, orphans: List[OrphanRecord]):
        """고아 레코드 테이블 업데이트"""
        self.table_orphans.setRowCount(len(orphans))

        for i, orphan in enumerate(orphans):
            self.table_orphans.setItem(i, 0, QTableWidgetItem(orphan.child_table))
            self.table_orphans.setItem(i, 1, QTableWidgetItem(orphan.child_column))
            self.table_orphans.setItem(i, 2, QTableWidgetItem(orphan.parent_table))
            self.table_orphans.setItem(i, 3, QTableWidgetItem(orphan.parent_column))

            count_item = QTableWidgetItem(f"{orphan.orphan_count:,}")
            count_item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if orphan.orphan_count > 1000:
                count_item.setForeground(QColor("#e74c3c"))
            elif orphan.orphan_count > 100:
                count_item.setForeground(QColor("#f39c12"))
            self.table_orphans.setItem(i, 4, count_item)

            samples = ", ".join(str(v) for v in orphan.sample_values[:3])
            if len(orphan.sample_values) > 3:
                samples += "..."
            self.table_orphans.setItem(i, 5, QTableWidgetItem(samples))

        self.btn_dry_run.setEnabled(len(orphans) > 0)
        self.btn_execute.setEnabled(len(orphans) > 0)

    def update_compatibility_table(self, issues: List[CompatibilityIssue]):
        """호환성 이슈 테이블 업데이트"""
        self._all_issues = issues  # 필터링용 저장
        self.filter_compatibility_issues()

    def filter_compatibility_issues(self):
        """호환성 이슈 필터링"""
        if not hasattr(self, '_all_issues'):
            return

        show_error = self.chk_filter_error.isChecked()
        show_warning = self.chk_filter_warning.isChecked()
        show_info = self.chk_filter_info.isChecked()

        filtered = []
        for issue in self._all_issues:
            if issue.severity == "error" and show_error:
                filtered.append(issue)
            elif issue.severity == "warning" and show_warning:
                filtered.append(issue)
            elif issue.severity == "info" and show_info:
                filtered.append(issue)

        # UI 업데이트 최적화 - 일괄 업데이트
        self.table_issues.setUpdatesEnabled(False)
        self.table_issues.setRowCount(len(filtered))

        severity_icons = {
            "error": "❌",
            "warning": "⚠️",
            "info": "ℹ️"
        }

        type_names = {
            # 기존 이슈 타입
            IssueType.ORPHAN_ROW: "고아 레코드",
            IssueType.DEPRECATED_FUNCTION: "deprecated 함수",
            IssueType.CHARSET_ISSUE: "문자셋",
            IssueType.RESERVED_KEYWORD: "예약어",
            IssueType.SQL_MODE_ISSUE: "SQL 모드",
            # MySQL 8.4 Upgrade Checker 이슈 타입 (신규)
            IssueType.REMOVED_SYS_VAR: "제거된 시스템 변수",
            IssueType.AUTH_PLUGIN_ISSUE: "인증 플러그인",
            IssueType.INVALID_DATE: "잘못된 날짜",
            IssueType.ZEROFILL_USAGE: "ZEROFILL 속성",
            IssueType.FLOAT_PRECISION: "FLOAT 정밀도",
            IssueType.INT_DISPLAY_WIDTH: "INT 표시 너비",
            IssueType.FK_NAME_LENGTH: "FK 이름 길이",
            IssueType.FTS_TABLE_PREFIX: "FTS_ 테이블명",
            IssueType.SUPER_PRIVILEGE: "SUPER 권한",
            IssueType.DEFAULT_VALUE_CHANGE: "기본값 변경",
        }

        for i, issue in enumerate(filtered):
            severity_item = QTableWidgetItem(f"{severity_icons.get(issue.severity, '')} {issue.severity.upper()}")
            if issue.severity == "error":
                severity_item.setForeground(QColor("#e74c3c"))
            elif issue.severity == "warning":
                severity_item.setForeground(QColor("#f39c12"))

            self.table_issues.setItem(i, 0, severity_item)
            self.table_issues.setItem(i, 1, QTableWidgetItem(type_names.get(issue.issue_type, str(issue.issue_type))))
            self.table_issues.setItem(i, 2, QTableWidgetItem(issue.location))
            self.table_issues.setItem(i, 3, QTableWidgetItem(issue.description))
            self.table_issues.setItem(i, 4, QTableWidgetItem(issue.suggestion))

        # UI 업데이트 재활성화
        self.table_issues.setUpdatesEnabled(True)

    def update_fk_tree(self, fk_tree: Dict[str, List[str]], schema: str):
        """FK 트리 업데이트"""
        self.tree_fk.clear()

        if not fk_tree:
            self.txt_fk_tree.setText("FK 관계가 없습니다.")
            return

        # 루트 테이블 찾기
        all_children = set()
        for children in fk_tree.values():
            all_children.update(children)

        root_tables = set(fk_tree.keys()) - all_children

        def add_tree_items(parent_item, table: str, visited: set):
            if table in fk_tree:
                for child in fk_tree[table]:
                    # 순환 참조 방지
                    if child in visited:
                        child_item = QTreeWidgetItem(parent_item, [f"🔄 {child} (순환 참조)"])
                        continue
                    child_item = QTreeWidgetItem(parent_item, [f"└── {child}"])
                    add_tree_items(child_item, child, visited | {child})

        for root in sorted(root_tables):
            root_item = QTreeWidgetItem(self.tree_fk, [f"📁 {root}"])
            add_tree_items(root_item, root, {root})

        self.tree_fk.expandAll()

        # ASCII 트리 텍스트
        analyzer = MigrationAnalyzer(self.connector)
        tree_text = analyzer.get_fk_visualization(schema)
        self.txt_fk_tree.setText(tree_text)

    def on_orphan_selected(self):
        """고아 레코드 선택 시"""
        selected_rows = self.table_orphans.selectionModel().selectedRows()

        if not selected_rows or not self.analysis_result:
            self.txt_cleanup_sql.clear()
            return

        # 선택된 고아 레코드들에 대한 SQL 생성
        sql_parts = []
        schema = self.analysis_result.schema
        action = ActionType.DELETE if self.radio_delete.isChecked() else ActionType.SET_NULL

        analyzer = MigrationAnalyzer(self.connector)

        for row_index in selected_rows:
            row = row_index.row()
            if row < len(self.analysis_result.orphan_records):
                orphan = self.analysis_result.orphan_records[row]
                cleanup = analyzer.generate_cleanup_sql(orphan, action, schema, dry_run=True)
                sql_parts.append(f"-- {cleanup.description}\n{cleanup.sql};")

        self.txt_cleanup_sql.setText("\n\n".join(sql_parts))

    def select_all_orphans(self):
        """모든 고아 레코드 선택"""
        self.table_orphans.selectAll()

    def execute_cleanup(self, dry_run: bool = True):
        """정리 작업 실행"""
        if not self.analysis_result:
            return

        selected_rows = self.table_orphans.selectionModel().selectedRows()
        if not selected_rows:
            QMessageBox.warning(self, "선택 필요", "정리할 고아 레코드를 선택하세요.")
            return

        # 실제 실행 시 확인
        if not dry_run:
            reply = QMessageBox.warning(
                self,
                "실행 확인",
                f"선택된 {len(selected_rows)}개 항목에 대해 정리 작업을 실행합니다.\n\n"
                "이 작업은 되돌릴 수 없습니다. 계속하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 정리 작업 목록 생성
        schema = self.analysis_result.schema
        action = ActionType.DELETE if self.radio_delete.isChecked() else ActionType.SET_NULL
        analyzer = MigrationAnalyzer(self.connector)

        actions = []
        for row_index in selected_rows:
            row = row_index.row()
            if row < len(self.analysis_result.orphan_records):
                orphan = self.analysis_result.orphan_records[row]
                cleanup = analyzer.generate_cleanup_sql(orphan, action, schema, dry_run=dry_run)
                actions.append(cleanup)

        # UI 비활성화
        self.btn_dry_run.setEnabled(False)
        self.btn_execute.setEnabled(False)
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)

        mode = "DRY-RUN" if dry_run else "실행"
        self.add_log(f"🔧 [{mode}] 정리 작업 시작 ({len(actions)}개)")

        # 워커 실행
        self.cleanup_worker = CleanupWorker(
            connector=self.connector,
            schema=schema,
            actions=actions,
            dry_run=dry_run
        )

        self.cleanup_worker.progress.connect(self.add_log)
        self.cleanup_worker.action_complete.connect(self.on_action_complete)
        self.cleanup_worker.finished.connect(self.on_cleanup_finished)
        self.cleanup_worker.start()

    def on_action_complete(self, table: str, success: bool, message: str, affected: int):
        """개별 정리 작업 완료 시"""
        status = "✅" if success else "❌"
        self.add_log(f"  {status} {table}: {message}")

    def on_cleanup_finished(self, success: bool, message: str, results: dict):
        """정리 작업 완료 시"""
        self.btn_dry_run.setEnabled(True)
        self.btn_execute.setEnabled(True)
        self.progress_bar.setVisible(False)

        self.add_log(message)

        # 결과 요약
        total_affected = sum(r.get('affected_rows', 0) for r in results.values())
        success_count = sum(1 for r in results.values() if r.get('success'))
        fail_count = len(results) - success_count

        QMessageBox.information(
            self,
            "작업 완료",
            f"정리 작업이 완료되었습니다.\n\n"
            f"성공: {success_count}개\n"
            f"실패: {fail_count}개\n"
            f"영향받은 행: {total_affected:,}개"
        )

    # =========================================================================
    # 자동 수정 위저드
    # =========================================================================

    def open_fix_wizard(self):
        """자동 수정 위저드 열기"""
        if not self.analysis_result:
            QMessageBox.warning(self, "분석 필요", "먼저 스키마 분석을 실행하세요.")
            return

        if not self.analysis_result.compatibility_issues:
            QMessageBox.information(self, "이슈 없음", "수정할 호환성 이슈가 없습니다.")
            return

        try:
            from src.ui.dialogs.fix_wizard_dialog import FixWizardDialog

            wizard = FixWizardDialog(
                parent=self,
                connector=self.connector,
                issues=self.analysis_result.compatibility_issues,
                schema=self.analysis_result.schema
            )
            result = wizard.exec()

            if result:
                # 위저드 완료 후 재분석 권장
                reply = QMessageBox.question(
                    self,
                    "재분석",
                    "수정이 완료되었습니다. 변경사항을 확인하기 위해 재분석하시겠습니까?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.Yes
                )
                if reply == QMessageBox.StandardButton.Yes:
                    self.start_analysis()

        except ImportError as e:
            logger.error(f"자동 수정 위저드 모듈 로드 실패: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"자동 수정 위저드를 불러올 수 없습니다:\n{e}")
        except Exception as e:
            logger.error(f"자동 수정 위저드 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "오류", f"자동 수정 위저드 실행 중 오류:\n{e}")

    # =========================================================================
    # 분석 결과 저장/로드
    # =========================================================================

    def _get_analysis_dir(self) -> str:
        """분석 결과 저장 디렉토리"""
        if os.name == 'nt':
            base_dir = os.path.join(os.environ.get('LOCALAPPDATA', ''), 'TunnelForge', 'analysis')
        else:
            base_dir = os.path.join(os.path.expanduser('~'), '.config', 'tunnelforge', 'analysis')
        os.makedirs(base_dir, exist_ok=True)
        return base_dir

    def save_analysis_result(self):
        """분석 결과 저장"""
        if not self.analysis_result:
            QMessageBox.warning(self, "저장 오류", "저장할 분석 결과가 없습니다.")
            return

        # 기본 파일명 생성
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        default_name = f"{self.analysis_result.schema}_{timestamp}.json"
        default_path = os.path.join(self._get_analysis_dir(), default_name)

        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "분석 결과 저장",
            default_path,
            "JSON 파일 (*.json);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(self.analysis_result.to_dict(), f, ensure_ascii=False, indent=2, default=str)

            self.add_log(f"💾 분석 결과 저장 완료: {file_path}")
            QMessageBox.information(self, "저장 완료", f"분석 결과가 저장되었습니다.\n\n{file_path}")

        except Exception as e:
            logger.error(f"분석 결과 저장 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "저장 오류", f"파일 저장 실패:\n{e}")

    def load_analysis_result(self):
        """분석 결과 불러오기"""
        default_dir = self._get_analysis_dir()

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "분석 결과 불러오기",
            default_dir,
            "JSON 파일 (*.json);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)

            result = AnalysisResult.from_dict(data)

            # UI 업데이트
            self.analysis_result = result
            self.combo_schema.setCurrentText(result.schema)
            self.update_overview(result)
            self.update_orphans_table(result.orphan_records)
            self.update_compatibility_table(result.compatibility_issues)
            self.update_fk_tree(result.fk_tree, result.schema)
            self.btn_save.setEnabled(True)
            # 자동 수정 버튼 활성화
            self.btn_auto_fix.setEnabled(len(result.compatibility_issues) > 0)

            self.add_log(f"📂 분석 결과 불러오기 완료: {file_path}")
            self.add_log(f"   스키마: {result.schema}, 분석일시: {result.analyzed_at}")
            QMessageBox.information(
                self,
                "불러오기 완료",
                f"분석 결과를 불러왔습니다.\n\n"
                f"스키마: {result.schema}\n"
                f"분석일시: {result.analyzed_at}\n"
                f"테이블: {result.total_tables}개\n"
                f"FK 관계: {result.total_fk_relations}개"
            )

        except Exception as e:
            logger.error(f"분석 결과 불러오기 오류: {e}", exc_info=True)
            QMessageBox.critical(self, "불러오기 오류", f"파일 불러오기 실패:\n{e}")


class MigrationWizard:
    """마이그레이션 분석 위저드"""

    @staticmethod
    def start(parent=None, tunnel_engine=None, config_manager=None) -> bool:
        """
        마이그레이션 분석 시작

        Args:
            parent: 부모 위젯
            tunnel_engine: TunnelEngine 인스턴스
            config_manager: ConfigManager 인스턴스

        Returns:
            성공 여부
        """
        from src.ui.dialogs.db_dialogs import DBConnectionDialog

        # 1단계: DB 연결
        conn_dialog = DBConnectionDialog(parent, tunnel_engine, config_manager)
        if conn_dialog.exec() != QDialog.DialogCode.Accepted:
            return False

        connector = conn_dialog.connector
        if not connector:
            return False

        # 2단계: 마이그레이션 분석 다이얼로그
        try:
            analyzer_dialog = MigrationAnalyzerDialog(parent, connector, config_manager)
            analyzer_dialog.exec()
            return True
        finally:
            # 연결 종료
            if connector:
                connector.disconnect()
