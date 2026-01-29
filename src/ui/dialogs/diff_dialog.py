"""
스키마 비교 다이얼로그
- 소스/타겟 연결 선택
- 스키마 비교 결과 표시
- 동기화 스크립트 생성
"""
from typing import List, Dict, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QComboBox, QPushButton, QGroupBox,
    QTreeWidget, QTreeWidgetItem, QTextEdit, QSplitter,
    QWidget, QProgressBar, QMessageBox, QFileDialog,
    QHeaderView, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QColor

from src.core.schema_diff import (
    SchemaExtractor, SchemaComparator, SyncScriptGenerator,
    TableDiff, DiffType
)
from src.core.db_connector import MySQLConnector
from src.core.logger import get_logger

logger = get_logger(__name__)


class SchemaCompareThread(QThread):
    """스키마 비교 백그라운드 스레드"""

    progress = pyqtSignal(str)
    finished = pyqtSignal(list)  # List[TableDiff]
    error = pyqtSignal(str)

    def __init__(self, source_connector, target_connector,
                 source_schema: str, target_schema: str):
        super().__init__()
        self.source_connector = source_connector
        self.target_connector = target_connector
        self.source_schema = source_schema
        self.target_schema = target_schema

    def run(self):
        try:
            self.progress.emit("소스 스키마 추출 중...")
            source_extractor = SchemaExtractor(self.source_connector)
            source_tables = source_extractor.extract_all_tables(self.source_schema)

            self.progress.emit("타겟 스키마 추출 중...")
            target_extractor = SchemaExtractor(self.target_connector)
            target_tables = target_extractor.extract_all_tables(self.target_schema)

            self.progress.emit("스키마 비교 중...")
            comparator = SchemaComparator()
            diffs = comparator.compare_schemas(source_tables, target_tables)

            self.finished.emit(diffs)

        except Exception as e:
            self.error.emit(str(e))


class SchemaDiffDialog(QDialog):
    """스키마 비교 다이얼로그"""

    def __init__(self, parent=None, tunnels: List[dict] = None,
                 tunnel_engine=None, config_manager=None):
        """
        Args:
            parent: 부모 위젯
            tunnels: 터널 설정 목록
            tunnel_engine: TunnelEngine 인스턴스
            config_manager: ConfigManager 인스턴스
        """
        super().__init__(parent)
        self.tunnels = tunnels or []
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager

        self._source_connector = None
        self._target_connector = None
        self._diffs = []
        self._compare_thread = None

        self._setup_ui()
        self._connect_signals()

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("스키마 비교")
        self.setMinimumSize(900, 650)

        layout = QVBoxLayout(self)

        # 연결 선택
        conn_group = QGroupBox("연결 선택")
        conn_layout = QHBoxLayout(conn_group)

        # 소스 연결
        source_layout = QFormLayout()
        self.source_tunnel_combo = QComboBox()
        self.source_tunnel_combo.setMinimumWidth(200)
        for tunnel in self.tunnels:
            port = tunnel.get('local_port', '')
            name = f"{tunnel.get('name', '')} ({port})"
            self.source_tunnel_combo.addItem(name, tunnel.get('id'))
        source_layout.addRow("소스 터널:", self.source_tunnel_combo)

        self.source_schema_combo = QComboBox()
        self.source_schema_combo.setMinimumWidth(150)
        source_layout.addRow("스키마:", self.source_schema_combo)

        conn_layout.addLayout(source_layout)

        # 화살표
        arrow_label = QLabel("  →  ")
        arrow_label.setFont(QFont("", 16, QFont.Weight.Bold))
        conn_layout.addWidget(arrow_label)

        # 타겟 연결
        target_layout = QFormLayout()
        self.target_tunnel_combo = QComboBox()
        self.target_tunnel_combo.setMinimumWidth(200)
        for tunnel in self.tunnels:
            port = tunnel.get('local_port', '')
            name = f"{tunnel.get('name', '')} ({port})"
            self.target_tunnel_combo.addItem(name, tunnel.get('id'))
        target_layout.addRow("타겟 터널:", self.target_tunnel_combo)

        self.target_schema_combo = QComboBox()
        self.target_schema_combo.setMinimumWidth(150)
        target_layout.addRow("스키마:", self.target_schema_combo)

        conn_layout.addLayout(target_layout)
        conn_layout.addStretch()

        # 비교 버튼
        self.compare_btn = QPushButton("비교 시작")
        self.compare_btn.setStyleSheet("""
            QPushButton {
                background-color: #3498db; color: white;
                padding: 8px 20px; border-radius: 4px; border: none;
                font-size: 13px; font-weight: bold;
            }
            QPushButton:hover { background-color: #2980b9; }
            QPushButton:disabled { background-color: #bdc3c7; }
        """)
        conn_layout.addWidget(self.compare_btn)

        layout.addWidget(conn_group)

        # 진행 상태
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: #3498db; font-size: 12px;")
        layout.addWidget(self.progress_label)

        # 결과 영역 (스플리터)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # 테이블 목록
        table_group = QGroupBox("테이블 목록")
        table_layout = QVBoxLayout(table_group)

        self.diff_tree = QTreeWidget()
        self.diff_tree.setHeaderLabels(["테이블/항목", "상태", "행 수"])
        self.diff_tree.setColumnWidth(0, 200)
        self.diff_tree.setColumnWidth(1, 80)
        self.diff_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        table_layout.addWidget(self.diff_tree)

        # 요약
        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("font-size: 11px; color: gray;")
        table_layout.addWidget(self.summary_label)

        splitter.addWidget(table_group)

        # 상세 비교
        detail_group = QGroupBox("상세 비교")
        detail_layout = QVBoxLayout(detail_group)

        self.detail_text = QTextEdit()
        self.detail_text.setReadOnly(True)
        self.detail_text.setFont(QFont("Consolas", 10))
        detail_layout.addWidget(self.detail_text)

        splitter.addWidget(detail_group)
        splitter.setSizes([350, 500])

        layout.addWidget(splitter)

        # 버튼
        btn_layout = QHBoxLayout()

        self.script_btn = QPushButton("동기화 스크립트 생성")
        self.script_btn.setEnabled(False)
        self.script_btn.clicked.connect(self._generate_script)
        btn_layout.addWidget(self.script_btn)

        btn_layout.addStretch()

        self.close_btn = QPushButton("닫기")
        self.close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(self.close_btn)

        layout.addLayout(btn_layout)

    def _connect_signals(self):
        """시그널 연결"""
        self.source_tunnel_combo.currentIndexChanged.connect(
            lambda: self._load_schemas('source')
        )
        self.target_tunnel_combo.currentIndexChanged.connect(
            lambda: self._load_schemas('target')
        )
        self.compare_btn.clicked.connect(self._start_compare)
        self.diff_tree.currentItemChanged.connect(self._on_item_selected)

        # 초기 스키마 로드
        if self.tunnels:
            self._load_schemas('source')
            self._load_schemas('target')

    def _load_schemas(self, side: str):
        """스키마 목록 로드"""
        if side == 'source':
            combo = self.source_tunnel_combo
            schema_combo = self.source_schema_combo
        else:
            combo = self.target_tunnel_combo
            schema_combo = self.target_schema_combo

        tunnel_id = combo.currentData()
        if not tunnel_id:
            return

        schema_combo.clear()

        # 터널 연결 확인
        if not self.tunnel_engine.is_running(tunnel_id):
            schema_combo.addItem("(터널 연결 필요)")
            return

        # 연결 정보 가져오기
        conn_info = self.tunnel_engine.get_connection_info(tunnel_id)
        if not conn_info:
            schema_combo.addItem("(연결 정보 없음)")
            return

        # DB 연결
        try:
            connector = MySQLConnector(
                host=conn_info.get('host', '127.0.0.1'),
                port=conn_info.get('local_port', 3306),
                user=conn_info.get('db_user', 'root'),
                password=conn_info.get('db_password', '')
            )

            success, msg = connector.connect()
            if not success:
                schema_combo.addItem("(연결 실패)")
                return

            # 스키마 목록 조회
            query = """
                SELECT SCHEMA_NAME
                FROM INFORMATION_SCHEMA.SCHEMATA
                WHERE SCHEMA_NAME NOT IN ('information_schema', 'mysql', 'performance_schema', 'sys')
                ORDER BY SCHEMA_NAME
            """
            success, result = connector.execute_query(query)

            if success:
                for row in result:
                    schema_name = row[0] if isinstance(row, tuple) else row['SCHEMA_NAME']
                    schema_combo.addItem(schema_name)

            connector.disconnect()

        except Exception as e:
            logger.error(f"스키마 로드 실패: {e}")
            schema_combo.addItem("(오류)")

    def _start_compare(self):
        """비교 시작"""
        source_tunnel_id = self.source_tunnel_combo.currentData()
        target_tunnel_id = self.target_tunnel_combo.currentData()
        source_schema = self.source_schema_combo.currentText()
        target_schema = self.target_schema_combo.currentText()

        if not all([source_tunnel_id, target_tunnel_id, source_schema, target_schema]):
            QMessageBox.warning(self, "입력 오류", "모든 연결 정보를 선택하세요.")
            return

        if source_schema.startswith("(") or target_schema.startswith("("):
            QMessageBox.warning(self, "입력 오류", "유효한 스키마를 선택하세요.")
            return

        # 연결 생성
        try:
            source_conn = self.tunnel_engine.get_connection_info(source_tunnel_id)
            target_conn = self.tunnel_engine.get_connection_info(target_tunnel_id)

            self._source_connector = MySQLConnector(
                host=source_conn.get('host', '127.0.0.1'),
                port=source_conn.get('local_port', 3306),
                user=source_conn.get('db_user', 'root'),
                password=source_conn.get('db_password', '')
            )
            success, _ = self._source_connector.connect()
            if not success:
                raise Exception("소스 연결 실패")

            self._target_connector = MySQLConnector(
                host=target_conn.get('host', '127.0.0.1'),
                port=target_conn.get('local_port', 3306),
                user=target_conn.get('db_user', 'root'),
                password=target_conn.get('db_password', '')
            )
            success, _ = self._target_connector.connect()
            if not success:
                # 소스 연결 정리 후 예외 발생
                if self._source_connector:
                    self._source_connector.disconnect()
                    self._source_connector = None
                raise Exception("타겟 연결 실패")

        except Exception as e:
            # 연결 정리
            if self._source_connector:
                try:
                    self._source_connector.disconnect()
                except:
                    pass
                self._source_connector = None
            if self._target_connector:
                try:
                    self._target_connector.disconnect()
                except:
                    pass
                self._target_connector = None
            QMessageBox.critical(self, "연결 오류", f"DB 연결 실패: {e}")
            return

        # UI 업데이트
        self.compare_btn.setEnabled(False)
        self.script_btn.setEnabled(False)
        self.diff_tree.clear()
        self.detail_text.clear()
        self.progress_label.setText("비교 시작...")

        # 백그라운드 스레드에서 비교
        self._compare_thread = SchemaCompareThread(
            self._source_connector, self._target_connector,
            source_schema, target_schema
        )
        self._compare_thread.progress.connect(self._on_progress)
        self._compare_thread.finished.connect(self._on_compare_finished)
        self._compare_thread.error.connect(self._on_compare_error)
        self._compare_thread.start()

    def _on_progress(self, message: str):
        """진행 상태 업데이트"""
        self.progress_label.setText(message)

    def _on_compare_finished(self, diffs: List[TableDiff]):
        """비교 완료"""
        self._diffs = diffs
        self.compare_btn.setEnabled(True)
        self.script_btn.setEnabled(True)
        self.progress_label.setText("비교 완료")

        self._display_results(diffs)

    def _on_compare_error(self, error: str):
        """비교 오류"""
        self.compare_btn.setEnabled(True)
        self.progress_label.setText("")
        QMessageBox.critical(self, "비교 오류", f"스키마 비교 실패: {error}")

    def _display_results(self, diffs: List[TableDiff]):
        """비교 결과 표시"""
        self.diff_tree.clear()

        added = 0
        removed = 0
        modified = 0
        unchanged = 0

        for diff in diffs:
            # 상태 아이콘
            if diff.diff_type == DiffType.ADDED:
                icon = "🟢"
                status = "추가"
                added += 1
            elif diff.diff_type == DiffType.REMOVED:
                icon = "🔴"
                status = "삭제"
                removed += 1
            elif diff.diff_type == DiffType.MODIFIED:
                icon = "🟡"
                status = "수정"
                modified += 1
            else:
                icon = "⚪"
                status = "동일"
                unchanged += 1

            # 테이블 항목
            item = QTreeWidgetItem([
                f"{icon} {diff.table_name}",
                status,
                f"{diff.row_count_source} / {diff.row_count_target}"
            ])
            item.setData(0, Qt.ItemDataRole.UserRole, diff)

            # 컬럼 차이
            if diff.column_diffs:
                for col_diff in diff.column_diffs:
                    if col_diff.diff_type != DiffType.UNCHANGED:
                        col_icon = self._get_diff_icon(col_diff.diff_type)
                        col_item = QTreeWidgetItem([
                            f"  {col_icon} {col_diff.column_name}",
                            col_diff.diff_type.value,
                            ""
                        ])
                        col_item.setData(0, Qt.ItemDataRole.UserRole, col_diff)
                        item.addChild(col_item)

            # 인덱스 차이
            if diff.index_diffs:
                for idx_diff in diff.index_diffs:
                    if idx_diff.diff_type != DiffType.UNCHANGED:
                        idx_icon = self._get_diff_icon(idx_diff.diff_type)
                        idx_item = QTreeWidgetItem([
                            f"  {idx_icon} [IDX] {idx_diff.index_name}",
                            idx_diff.diff_type.value,
                            ""
                        ])
                        idx_item.setData(0, Qt.ItemDataRole.UserRole, idx_diff)
                        item.addChild(idx_item)

            # FK 차이
            if diff.fk_diffs:
                for fk_diff in diff.fk_diffs:
                    if fk_diff.diff_type != DiffType.UNCHANGED:
                        fk_icon = self._get_diff_icon(fk_diff.diff_type)
                        fk_item = QTreeWidgetItem([
                            f"  {fk_icon} [FK] {fk_diff.fk_name}",
                            fk_diff.diff_type.value,
                            ""
                        ])
                        fk_item.setData(0, Qt.ItemDataRole.UserRole, fk_diff)
                        item.addChild(fk_item)

            self.diff_tree.addTopLevelItem(item)

            # 변경된 테이블 펼치기
            if diff.diff_type == DiffType.MODIFIED:
                item.setExpanded(True)

        # 요약
        self.summary_label.setText(
            f"총 {len(diffs)}개 테이블: "
            f"🟢 추가 {added}, 🟡 수정 {modified}, 🔴 삭제 {removed}, ⚪ 동일 {unchanged}"
        )

    def _get_diff_icon(self, diff_type: DiffType) -> str:
        """차이 유형에 따른 아이콘"""
        icons = {
            DiffType.ADDED: "🟢",
            DiffType.REMOVED: "🔴",
            DiffType.MODIFIED: "🟡",
            DiffType.UNCHANGED: "⚪"
        }
        return icons.get(diff_type, "")

    def _on_item_selected(self, current, previous):
        """항목 선택 시 상세 표시"""
        if not current:
            return

        data = current.data(0, Qt.ItemDataRole.UserRole)
        if not data:
            return

        self.detail_text.clear()

        if isinstance(data, TableDiff):
            self._show_table_detail(data)
        else:
            # 컬럼/인덱스/FK 차이
            self._show_diff_detail(data)

    def _show_table_detail(self, diff: TableDiff):
        """테이블 상세 정보 표시"""
        lines = [
            f"테이블: {diff.table_name}",
            f"상태: {diff.diff_type.value}",
            f"행 수: 소스 {diff.row_count_source} / 타겟 {diff.row_count_target}",
            "",
            "=" * 50
        ]

        if diff.source_schema:
            lines.append("\n[소스 컬럼]")
            for col in diff.source_schema.columns:
                lines.append(f"  {col.name}: {col.data_type}")

        if diff.target_schema:
            lines.append("\n[타겟 컬럼]")
            for col in diff.target_schema.columns:
                lines.append(f"  {col.name}: {col.data_type}")

        if diff.column_diffs:
            changed = [d for d in diff.column_diffs if d.diff_type != DiffType.UNCHANGED]
            if changed:
                lines.append("\n[컬럼 변경]")
                for col_diff in changed:
                    lines.append(f"  {col_diff.diff_type.value}: {col_diff.column_name}")
                    for d in col_diff.differences:
                        lines.append(f"    - {d}")

        self.detail_text.setPlainText("\n".join(lines))

    def _show_diff_detail(self, diff):
        """차이 상세 정보 표시"""
        lines = []

        if hasattr(diff, 'column_name'):
            lines.append(f"컬럼: {diff.column_name}")
        elif hasattr(diff, 'index_name'):
            lines.append(f"인덱스: {diff.index_name}")
        elif hasattr(diff, 'fk_name'):
            lines.append(f"FK: {diff.fk_name}")

        lines.append(f"상태: {diff.diff_type.value}")

        if diff.differences:
            lines.append("\n[변경 내용]")
            for d in diff.differences:
                lines.append(f"  - {d}")

        if hasattr(diff, 'source_info') and diff.source_info:
            lines.append(f"\n[소스]\n  {diff.source_info}")

        if hasattr(diff, 'target_info') and diff.target_info:
            lines.append(f"\n[타겟]\n  {diff.target_info}")

        self.detail_text.setPlainText("\n".join(lines))

    def _generate_script(self):
        """동기화 스크립트 생성"""
        if not self._diffs:
            return

        target_schema = self.target_schema_combo.currentText()
        generator = SyncScriptGenerator()
        script = generator.generate_sync_script(self._diffs, target_schema)

        # 스크립트 다이얼로그 열기
        dialog = SyncScriptDialog(self, script)
        dialog.exec()

    def closeEvent(self, event):
        """다이얼로그 닫힐 때"""
        # 연결 정리
        if self._source_connector:
            try:
                self._source_connector.disconnect()
            except:
                pass

        if self._target_connector:
            try:
                self._target_connector.disconnect()
            except:
                pass

        super().closeEvent(event)


class SyncScriptDialog(QDialog):
    """동기화 스크립트 다이얼로그"""

    def __init__(self, parent=None, script: str = ""):
        super().__init__(parent)
        self.script = script
        self._setup_ui()

    def _setup_ui(self):
        """UI 구성"""
        self.setWindowTitle("동기화 스크립트")
        self.setMinimumSize(700, 500)

        layout = QVBoxLayout(self)

        # 경고
        warning = QLabel(
            "⚠️ 주의: 이 스크립트를 실행하기 전에 반드시 타겟 데이터베이스를 백업하세요!"
        )
        warning.setStyleSheet(
            "background-color: #fff3cd; color: #856404; "
            "padding: 10px; border-radius: 4px; font-weight: bold;"
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)

        # 스크립트
        self.script_text = QTextEdit()
        self.script_text.setPlainText(self.script)
        self.script_text.setFont(QFont("Consolas", 10))
        self.script_text.setReadOnly(True)
        layout.addWidget(self.script_text)

        # 버튼
        btn_layout = QHBoxLayout()

        copy_btn = QPushButton("클립보드에 복사")
        copy_btn.clicked.connect(self._copy_to_clipboard)
        btn_layout.addWidget(copy_btn)

        save_btn = QPushButton("파일로 저장")
        save_btn.clicked.connect(self._save_to_file)
        btn_layout.addWidget(save_btn)

        btn_layout.addStretch()

        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        layout.addLayout(btn_layout)

    def _copy_to_clipboard(self):
        """클립보드에 복사"""
        clipboard = QApplication.clipboard()
        clipboard.setText(self.script)
        QMessageBox.information(self, "복사 완료", "스크립트가 클립보드에 복사되었습니다.")

    def _save_to_file(self):
        """파일로 저장"""
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "스크립트 저장",
            "sync_script.sql",
            "SQL Files (*.sql);;All Files (*)"
        )

        if file_path:
            try:
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(self.script)
                QMessageBox.information(
                    self, "저장 완료",
                    f"스크립트가 저장되었습니다:\n{file_path}"
                )
            except Exception as e:
                QMessageBox.critical(self, "저장 실패", f"파일 저장 실패: {e}")
