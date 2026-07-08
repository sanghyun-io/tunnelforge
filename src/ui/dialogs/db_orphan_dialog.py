"""
고아 레코드(FK 참조 무결성 깨짐) 분석 다이얼로그
"""
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QComboBox,
    QListWidget, QGroupBox,
    QFileDialog, QMessageBox, QProgressBar, QApplication,
    QWidget, QSplitter
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from typing import List, Optional
from datetime import datetime

from src.core.db_connector import MySQLConnector
from src.core.i18n import translate_text
from src.exporters.rust_dump_exporter import ForeignKeyResolver, OrphanRecordInfo


def _build_orphan_queries_sql(schema: str, orphan_results: List[OrphanRecordInfo]) -> str:
    """이미 수집된 고아 레코드 결과로부터 조회 쿼리 모음을 생성 (DB 재조회 없음)."""
    lines = [
        f"-- 고아 레코드 조회 쿼리 (스키마: {schema})",
        f"-- 생성일시: {datetime.now().isoformat()}",
        f"-- 발견된 고아 관계: {len(orphan_results)}건",
        "",
    ]
    for index, item in enumerate(orphan_results, 1):
        lines.append(
            f"-- [{index}] {item.table}.{item.column} -> "
            f"{item.referenced_table}.{item.referenced_column} "
            f"({item.orphan_count:,}건)"
        )
        lines.append(item.query.rstrip() + ";")
        lines.append("")
    return "\n".join(lines)


class OrphanAnalysisWorker(QThread):
    """스키마의 고아 레코드를 분석하는 백그라운드 워커 (GUI 스레드 블로킹 방지)."""
    progress = pyqtSignal(str)
    analysis_finished = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, connector, schema: str):
        super().__init__()
        self.connector = connector
        self.schema = schema

    def run(self):
        try:
            resolver = ForeignKeyResolver(self.connector)
            results = resolver.find_orphan_records(
                self.schema,
                progress_callback=lambda msg: self.progress.emit(msg),
            )
            self.analysis_finished.emit(results)
        except Exception as exc:
            self.failed.emit(str(exc))


class OrphanReportWorker(QThread):
    """이미 수집된 고아 레코드 결과로 보고서 파일을 작성하는 백그라운드 워커 (DB 재조회 없음)."""
    progress = pyqtSignal(str)
    report_finished = pyqtSignal(bool, str, int)

    def __init__(self, schema: str, output_path: str, orphan_results: List[OrphanRecordInfo]):
        super().__init__()
        self.schema = schema
        self.output_path = output_path
        self.orphan_results = orphan_results

    def run(self):
        try:
            orphans = self.orphan_results
            with open(self.output_path, "w", encoding="utf-8") as file:
                file.write("# 고아 레코드 분석 보고서\n")
                file.write(f"# 스키마: {self.schema}\n")
                file.write(f"# 생성일시: {datetime.now().isoformat()}\n")
                file.write(f"# 발견된 고아 관계: {len(orphans)}건\n")
                file.write("=" * 80 + "\n\n")
                if not orphans:
                    file.write("고아 레코드가 발견되지 않았습니다.\n")
                else:
                    total_orphans = sum(item.orphan_count for item in orphans)
                    file.write(f"총 {total_orphans:,}개의 고아 레코드 발견\n\n")
                    for index, item in enumerate(orphans, 1):
                        file.write(
                            f"## [{index}] {item.table}.{item.column} -> "
                            f"{item.referenced_table}.{item.referenced_column}\n"
                        )
                        file.write(f"   고아 레코드 수: {item.orphan_count:,}건\n")
                        file.write(f"   샘플 값: {', '.join(item.sample_values)}\n")
                        file.write("\n   조회 쿼리:\n")
                        file.write("   ```sql\n")
                        for line in item.query.split("\n"):
                            file.write(f"   {line}\n")
                        file.write("   ```\n\n")
                        file.write("-" * 80 + "\n\n")
            self.report_finished.emit(True, f"보고서 저장 완료: {self.output_path}", len(orphans))
        except Exception as exc:
            self.report_finished.emit(False, f"보고서 저장 실패: {exc}", 0)


class OrphanRecordDialog(QDialog):
    """고아 레코드 분석 다이얼로그"""

    def __init__(self, parent=None, connector: MySQLConnector = None, config_manager=None):
        super().__init__(parent)
        self.connector = connector
        self.config_manager = config_manager
        self.resolver: Optional[ForeignKeyResolver] = None
        self.worker: Optional[QThread] = None
        self.orphan_results: List[OrphanRecordInfo] = []

        self.setWindowTitle("🔍 고아 레코드 분석")
        self.setMinimumSize(900, 650)
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # === 상단: 스키마 선택 ===
        schema_group = QGroupBox("스키마 선택")
        schema_layout = QHBoxLayout(schema_group)

        self.schema_combo = QComboBox()
        self.schema_combo.setMinimumWidth(200)
        schema_layout.addWidget(QLabel("스키마:"))
        schema_layout.addWidget(self.schema_combo)

        self.analyze_btn = QPushButton("🔍 분석 시작")
        self.analyze_btn.clicked.connect(self.start_analysis)
        schema_layout.addWidget(self.analyze_btn)

        schema_layout.addStretch()
        layout.addWidget(schema_group)

        # === 중앙: 결과 영역 ===
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 왼쪽: 고아 관계 목록
        left_widget = QWidget()
        left_layout = QVBoxLayout(left_widget)
        left_layout.setContentsMargins(0, 0, 0, 0)

        left_layout.addWidget(QLabel("발견된 고아 관계:"))
        self.result_list = QListWidget()
        self.result_list.currentRowChanged.connect(self.on_result_selected)
        left_layout.addWidget(self.result_list)

        splitter.addWidget(left_widget)

        # 오른쪽: 상세 정보
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(0, 0, 0, 0)

        right_layout.addWidget(QLabel("상세 정보 / SQL 쿼리:"))

        from PyQt6.QtWidgets import QTextEdit
        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        right_layout.addWidget(self.detail_text)

        # 쿼리 복사 버튼
        copy_btn_layout = QHBoxLayout()
        self.copy_query_btn = QPushButton("📋 쿼리 복사")
        self.copy_query_btn.clicked.connect(self.copy_current_query)
        self.copy_query_btn.setEnabled(False)
        copy_btn_layout.addWidget(self.copy_query_btn)
        copy_btn_layout.addStretch()
        right_layout.addLayout(copy_btn_layout)

        splitter.addWidget(right_widget)
        splitter.setSizes([350, 550])

        layout.addWidget(splitter, stretch=1)

        # === 하단: 진행상황 및 버튼 ===
        progress_layout = QHBoxLayout()
        self.progress_label = QLabel("")
        progress_layout.addWidget(self.progress_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setVisible(False)
        progress_layout.addWidget(self.progress_bar)
        layout.addLayout(progress_layout)

        # 버튼 영역
        btn_layout = QHBoxLayout()

        self.export_all_queries_btn = QPushButton("📄 전체 쿼리 내보내기")
        self.export_all_queries_btn.clicked.connect(self.export_all_queries)
        self.export_all_queries_btn.setEnabled(False)
        btn_layout.addWidget(self.export_all_queries_btn)

        self.export_report_btn = QPushButton("📊 보고서 저장")
        self.export_report_btn.clicked.connect(self.export_report)
        self.export_report_btn.setEnabled(False)
        btn_layout.addWidget(self.export_report_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

        # 스키마 목록 로드
        self.load_schemas()

    def load_schemas(self):
        """스키마 목록 로드"""
        if not self.connector:
            return

        try:
            schemas = self.connector.get_schemas()
            self.schema_combo.clear()
            self.schema_combo.addItems(schemas)
        except Exception as e:
            QMessageBox.warning(self, "경고", f"스키마 목록 로드 실패:\n{str(e)}")

    def start_analysis(self):
        """고아 레코드 분석 시작 (백그라운드 워커, GUI 스레드 블로킹 없음)"""
        schema = self.schema_combo.currentText()
        if not schema:
            QMessageBox.warning(self, "경고", "스키마를 선택해주세요.")
            return

        if self.worker is not None and self.worker.isRunning():
            return

        self.result_list.clear()
        self.detail_text.clear()
        self.orphan_results.clear()
        self.copy_query_btn.setEnabled(False)
        self.export_all_queries_btn.setEnabled(False)
        self.export_report_btn.setEnabled(False)

        self.analyze_btn.setEnabled(False)
        self.export_all_queries_btn.setEnabled(False)
        self.export_report_btn.setEnabled(False)
        self.progress_label.setText("분석 중...")
        self.progress_bar.setVisible(True)
        self.progress_bar.setRange(0, 0)  # Indeterminate

        self.worker = OrphanAnalysisWorker(self.connector, schema)
        self.worker.progress.connect(self.progress_label.setText)
        self.worker.analysis_finished.connect(self._on_orphan_analysis_finished)
        self.worker.failed.connect(self._on_orphan_worker_failed)
        self.worker.finished.connect(self._clear_orphan_worker)
        self.worker.start()

    def _on_orphan_analysis_finished(self, results: list):
        """분석 워커 완료 처리"""
        self.orphan_results = results
        self.display_results()
        self.analyze_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")

    def _on_orphan_worker_failed(self, message: str):
        """분석 워커 실패 처리"""
        QMessageBox.critical(self, "오류", f"분석 중 오류 발생:\n{message}")
        self.analyze_btn.setEnabled(True)
        self.progress_bar.setVisible(False)
        self.progress_label.setText("")

    def _clear_orphan_worker(self):
        """워커 스레드 참조 정리 (QThread.finished 시그널에 연결)"""
        self.worker = None

    def display_results(self):
        """분석 결과 표시"""
        if not self.orphan_results:
            self.result_list.addItem("✅ 고아 레코드가 발견되지 않았습니다.")
            self.detail_text.setText("모든 FK 관계가 정상입니다.")
            return

        total_orphans = sum(o.orphan_count for o in self.orphan_results)
        self.progress_label.setText(f"⚠️ {len(self.orphan_results)}개 관계에서 총 {total_orphans:,}개 고아 레코드 발견")

        for o in self.orphan_results:
            item_text = f"⚠️ {o.table}.{o.column} → {o.referenced_table} ({o.orphan_count:,}건)"
            self.result_list.addItem(item_text)

        self.export_all_queries_btn.setEnabled(True)
        self.export_report_btn.setEnabled(True)

        # 첫 번째 항목 선택
        if self.result_list.count() > 0:
            self.result_list.setCurrentRow(0)

    def on_result_selected(self, row: int):
        """결과 목록 선택 시"""
        if row < 0 or row >= len(self.orphan_results):
            self.detail_text.clear()
            self.copy_query_btn.setEnabled(False)
            return

        o = self.orphan_results[row]

        detail = f"""═══════════════════════════════════════════════════════════════════
 고아 레코드 상세 정보
═══════════════════════════════════════════════════════════════════

📊 FK 관계:
   자식 테이블: {o.table}
   FK 컬럼: {o.column}
   부모 테이블: {o.referenced_table}
   참조 컬럼: {o.referenced_column}

⚠️ 고아 레코드 수: {o.orphan_count:,}건

📝 샘플 값 (최대 5개):
   {', '.join(o.sample_values) if o.sample_values else '(없음)'}

═══════════════════════════════════════════════════════════════════
 조회 쿼리 (아래 쿼리로 고아 레코드를 직접 조회할 수 있습니다)
═══════════════════════════════════════════════════════════════════

{o.query}
"""
        self.detail_text.setText(detail)
        self.copy_query_btn.setEnabled(True)

    def copy_current_query(self):
        """현재 선택된 쿼리 복사"""
        row = self.result_list.currentRow()
        if row < 0 or row >= len(self.orphan_results):
            return

        o = self.orphan_results[row]
        clipboard = QApplication.clipboard()
        clipboard.setText(o.query)

        self.progress_label.setText("✅ 쿼리가 클립보드에 복사되었습니다.")

    def export_all_queries(self):
        """전체 쿼리 내보내기 (현재 세션의 orphan_results 기반, DB 재조회 없음)"""
        if not self.orphan_results:
            QMessageBox.information(self, "내보내기", translate_text("내보낼 고아 레코드 쿼리가 없습니다."))
            return

        schema = self.schema_combo.currentText()
        if not schema:
            return

        # 파일 저장 다이얼로그
        default_name = f"orphan_queries_{schema}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.sql"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "쿼리 저장",
            default_name,
            "SQL 파일 (*.sql);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        try:
            all_queries = _build_orphan_queries_sql(schema, self.orphan_results)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(all_queries)

            QMessageBox.information(
                self, "저장 완료",
                f"✅ 쿼리가 저장되었습니다.\n\n{file_path}"
            )
        except Exception as e:
            QMessageBox.critical(
                self, "저장 실패",
                f"❌ 쿼리 저장 중 오류가 발생했습니다.\n\n{str(e)}"
            )

    def export_report(self):
        """보고서 저장 (백그라운드 워커, 현재 orphan_results 재사용, DB 재조회 없음)"""
        if not self.orphan_results:
            QMessageBox.information(self, "내보내기", translate_text("내보낼 고아 레코드 결과가 없습니다."))
            return

        schema = self.schema_combo.currentText()
        if not schema:
            return

        if self.worker is not None and self.worker.isRunning():
            return

        # 파일 저장 다이얼로그
        default_name = f"orphan_report_{schema}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.md"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "보고서 저장",
            default_name,
            "Markdown 파일 (*.md);;텍스트 파일 (*.txt);;모든 파일 (*.*)"
        )

        if not file_path:
            return

        self.analyze_btn.setEnabled(False)
        self.export_all_queries_btn.setEnabled(False)
        self.export_report_btn.setEnabled(False)

        self.worker = OrphanReportWorker(schema, file_path, self.orphan_results)
        self.worker.report_finished.connect(self._on_orphan_report_finished)
        self.worker.finished.connect(self._clear_orphan_worker)
        self.worker.start()

    def _on_orphan_report_finished(self, success: bool, message: str, count: int):
        """보고서 저장 워커 완료 처리"""
        self.analyze_btn.setEnabled(True)
        self.export_all_queries_btn.setEnabled(bool(self.orphan_results))
        self.export_report_btn.setEnabled(bool(self.orphan_results))
        if success:
            QMessageBox.information(
                self, "저장 완료",
                f"✅ {message}\n\n발견된 고아 관계: {count}건"
            )
        else:
            QMessageBox.critical(self, "저장 실패", f"❌ {message}")

    def closeEvent(self, event):
        """다이얼로그 닫기"""
        if self.worker is not None and self.worker.isRunning():
            QMessageBox.warning(
                self,
                translate_text("분석 실행 중"),
                translate_text(
                    "고아 레코드 분석 또는 보고서 저장이 실행 중입니다.\n"
                    "현재 단계는 안전하게 중단할 수 없습니다. 완료 후 닫아주세요."
                )
            )
            event.ignore()
            return
        # connector는 외부에서 관리하므로 여기서 닫지 않음 (RustDumpWizard.start_orphan_check가 처리)
        event.accept()
