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
    QHeaderView
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QPen

from src.core.schema_diff import (
    SchemaExtractor, SchemaComparator, SyncScriptGenerator,
    TableDiff, ColumnDiff, IndexDiff, ForeignKeyDiff,
    DiffType, DiffSeverity, CompareLevel,
    SeverityClassifier, VersionContext, SeveritySummary
)
from src.core.db_connector import MySQLConnector
from src.ui.dialogs.diff_workers import SchemaCompareThread, SchemaLoadThread
from src.ui.dialogs.diff_pixel_loading_widget import PixelLoadingWidget
from src.ui.dialogs.diff_sync_script_dialog import SyncScriptDialog








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
        self._severity_summary = None
        self._version_ctx = None
        # 비교 시작 시점에 캡처한 스키마 이름 (비교 도중 콤보 변경과 무관하게 고정)
        self._compared_source_schema = None
        self._compared_target_schema = None
        # side('source'/'target') -> 현재 진행 중인 SchemaLoadThread (stale 결과 판별용)
        self._schema_load_threads: Dict[str, "SchemaLoadThread"] = {}
        # 종료 전까지 참조를 유지해 GC로 인한 스레드 파괴를 방지
        self._pending_schema_threads: List["SchemaLoadThread"] = []

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

        # 비교 수준 선택
        level_layout = QFormLayout()
        self.level_combo = QComboBox()
        self.level_combo.addItem("Quick (빠른 비교)", CompareLevel.QUICK)
        self.level_combo.addItem("Standard (표준)", CompareLevel.STANDARD)
        self.level_combo.addItem("Strict (엄격)", CompareLevel.STRICT)
        self.level_combo.setCurrentIndex(1)  # Standard 기본
        self.level_combo.setMinimumWidth(140)
        level_layout.addRow("비교 수준:", self.level_combo)
        conn_layout.addLayout(level_layout)

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

        # Pixel 아트 로딩 애니메이션
        self.loading_widget = PixelLoadingWidget()
        layout.addWidget(self.loading_widget)

        # 완료/오류 상태 라벨
        self.progress_label = QLabel("")
        self.progress_label.setStyleSheet("color: #3498db; font-size: 12px;")
        layout.addWidget(self.progress_label)

        # 심각도 요약 바
        self.severity_bar = QLabel("")
        self.severity_bar.setStyleSheet(
            "background-color: #f8f9fa; padding: 6px 12px; "
            "border-radius: 4px; font-size: 12px;"
        )
        self.severity_bar.setVisible(False)
        layout.addWidget(self.severity_bar)

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

    def _resolve_connection_params(self, tunnel_id: str):
        """터널 ID로 DB 연결 파라미터를 조회한다.

        Returns:
            (True, host, port, user, password) 성공 시
            (False, error_message, None, None, None) 실패 시
        """
        if not self.tunnel_engine.is_running(tunnel_id):
            return (False, "터널 연결 필요", None, None, None)

        host, port = self.tunnel_engine.get_connection_info(tunnel_id)
        if not host:
            return (False, "연결 정보 없음", None, None, None)

        db_user, db_password = self.config_manager.get_tunnel_credentials(tunnel_id)
        if not db_user:
            return (False, "자격 증명 없음", None, None, None)

        return (True, host, port, db_user, db_password)

    def _load_schemas(self, side: str):
        """스키마 목록 로드 (백그라운드 스레드에서 조회, UI 스레드 블로킹 방지)"""
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

        result = self._resolve_connection_params(tunnel_id)
        if not result[0]:
            schema_combo.addItem(f"({result[1]})")
            return

        _, host, port, db_user, db_password = result

        thread = SchemaLoadThread(side, host, port, db_user, db_password)
        thread.loaded.connect(self._on_schema_loaded)
        thread.load_failed.connect(self._on_schema_load_failed)
        thread.finished.connect(lambda t=thread: self._on_schema_thread_finished(t))

        # 같은 side의 이전 결과는 stale로 취급 (아래 콜백에서 sender 비교)
        self._schema_load_threads[side] = thread
        self._pending_schema_threads.append(thread)
        thread.start()

    def _on_schema_loaded(self, side: str, schemas: list):
        """스키마 목록 로드 완료 콜백 (stale 결과는 무시)"""
        if self._schema_load_threads.get(side) is not self.sender():
            return
        schema_combo = self.source_schema_combo if side == 'source' else self.target_schema_combo
        schema_combo.clear()
        for schema_name in schemas:
            schema_combo.addItem(schema_name)

    def _on_schema_load_failed(self, side: str, message: str):
        """스키마 목록 로드 실패 콜백 (stale 결과는 무시)"""
        if self._schema_load_threads.get(side) is not self.sender():
            return
        schema_combo = self.source_schema_combo if side == 'source' else self.target_schema_combo
        schema_combo.clear()
        schema_combo.addItem(message)

    def _on_schema_thread_finished(self, thread: "SchemaLoadThread"):
        """스레드 종료 후 보관 참조 정리 (실행 중 GC로 파괴되는 것 방지용 목록)"""
        if thread in self._pending_schema_threads:
            self._pending_schema_threads.remove(thread)

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

        # 연결 파라미터 검증
        source_params = self._resolve_connection_params(source_tunnel_id)
        if not source_params[0]:
            QMessageBox.warning(self, "소스 오류", f"소스: {source_params[1]}")
            return

        target_params = self._resolve_connection_params(target_tunnel_id)
        if not target_params[0]:
            QMessageBox.warning(self, "타겟 오류", f"타겟: {target_params[1]}")
            return

        _, source_host, source_port, source_user, source_pw = source_params
        _, target_host, target_port, target_user, target_pw = target_params

        # 이전 비교에서 남아있는 커넥터 정리 (반복 비교 시 세션 누수 방지)
        self._disconnect_connectors()

        # 비교 시작 시점의 스키마 이름을 캡처 (비교 중 콤보가 바뀌어도 결과와 일치 보장)
        self._compared_source_schema = source_schema
        self._compared_target_schema = target_schema

        # 연결 생성
        try:
            self._source_connector = MySQLConnector(
                host=source_host, port=source_port,
                user=source_user, password=source_pw
            )
            success, _ = self._source_connector.connect()
            if not success:
                raise Exception("소스 연결 실패")

            self._target_connector = MySQLConnector(
                host=target_host, port=target_port,
                user=target_user, password=target_pw
            )
            success, _ = self._target_connector.connect()
            if not success:
                # 소스 연결 정리 후 예외 발생
                self._disconnect_connectors()
                raise Exception("타겟 연결 실패")

        except Exception as e:
            # 연결 정리
            self._disconnect_connectors()
            QMessageBox.critical(self, "연결 오류", f"DB 연결 실패: {e}")
            return

        # UI 업데이트 - 비교 중 입력 비활성화
        self.compare_btn.setEnabled(False)
        self.source_tunnel_combo.setEnabled(False)
        self.source_schema_combo.setEnabled(False)
        self.target_tunnel_combo.setEnabled(False)
        self.target_schema_combo.setEnabled(False)
        self.script_btn.setEnabled(False)
        self.diff_tree.clear()
        self.detail_text.clear()
        self.severity_bar.setVisible(False)
        self.progress_label.setText("")
        self.loading_widget.start("비교 시작...")

        # 비교 수준
        compare_level = self.level_combo.currentData()

        # 백그라운드 스레드에서 비교
        self._compare_thread = SchemaCompareThread(
            self._source_connector, self._target_connector,
            source_schema, target_schema, compare_level
        )
        self._compare_thread.progress.connect(self._on_progress)
        self._compare_thread.compare_finished.connect(self._on_compare_finished)
        self._compare_thread.error.connect(self._on_compare_error)
        self._compare_thread.start()

    def _on_progress(self, message: str):
        """진행 상태 업데이트"""
        self.loading_widget.update_status(message)

    def _on_compare_finished(self, diffs, summary, version_ctx):
        """비교 완료"""
        self._diffs = diffs
        self._severity_summary = summary
        self._version_ctx = version_ctx
        self.compare_btn.setEnabled(True)
        self.source_tunnel_combo.setEnabled(True)
        self.source_schema_combo.setEnabled(True)
        self.target_tunnel_combo.setEnabled(True)
        self.target_schema_combo.setEnabled(True)
        self.script_btn.setEnabled(True)
        self.loading_widget.stop()
        self.progress_label.setText("✅ 비교 완료")

        self._update_severity_bar(summary, version_ctx)
        self._display_results(diffs)

    def _update_severity_bar(self, summary: SeveritySummary, version_ctx: VersionContext):
        """심각도 요약 바 업데이트"""
        parts = []
        if summary.critical > 0:
            parts.append(f"🔴 Critical: {summary.critical}")
        if summary.warning > 0:
            parts.append(f"🟡 Warning: {summary.warning}")
        if summary.info > 0:
            parts.append(f"ℹ️ Info: {summary.info}")

        version_info = ""
        if version_ctx.source_version_str or version_ctx.target_version_str:
            version_info = (
                f"  |  소스: MySQL {version_ctx.source_version_str}"
                f"  →  타겟: MySQL {version_ctx.target_version_str}"
            )

        if parts:
            bar_text = " | ".join(parts) + version_info

            # Critical이 있으면 배경색 변경
            if summary.critical > 0:
                self.severity_bar.setStyleSheet(
                    "background-color: #ffeaea; padding: 6px 12px; "
                    "border-radius: 4px; font-size: 12px; border: 1px solid #f5c6cb;"
                )
            else:
                self.severity_bar.setStyleSheet(
                    "background-color: #f8f9fa; padding: 6px 12px; "
                    "border-radius: 4px; font-size: 12px;"
                )

            self.severity_bar.setText(bar_text)
            self.severity_bar.setVisible(True)
        else:
            self.severity_bar.setVisible(False)

    def _get_severity_icon(self, severity: Optional[DiffSeverity]) -> str:
        """심각도에 따른 아이콘"""
        if severity is None:
            return ""
        icons = {
            DiffSeverity.CRITICAL: "🔴",
            DiffSeverity.WARNING: "🟡",
            DiffSeverity.INFO: "ℹ️",
        }
        return icons.get(severity, "")

    def _disconnect_connectors(self):
        """Disconnect and clear dialog-owned DB connectors."""
        for attr_name in ("_source_connector", "_target_connector"):
            connector = getattr(self, attr_name)
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass
            setattr(self, attr_name, None)

    def _on_compare_error(self, error: str):
        """비교 오류"""
        self.compare_btn.setEnabled(True)
        self.source_tunnel_combo.setEnabled(True)
        self.source_schema_combo.setEnabled(True)
        self.target_tunnel_combo.setEnabled(True)
        self.target_schema_combo.setEnabled(True)
        self.loading_widget.stop()
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

            for col_diff in diff.column_diffs or []:
                self._add_diff_child_item(item, "", col_diff, "column_name")
            for idx_diff in diff.index_diffs or []:
                self._add_diff_child_item(item, "[IDX] ", idx_diff, "index_name", show_renamed_old_name=True)
            for fk_diff in diff.fk_diffs or []:
                self._add_diff_child_item(item, "[FK] ", fk_diff, "fk_name", show_renamed_old_name=True)

            self.diff_tree.addTopLevelItem(item)

            # 변경된 테이블 펼치기
            if diff.diff_type == DiffType.MODIFIED:
                item.setExpanded(True)

        # 요약
        self.summary_label.setText(
            f"총 {len(diffs)}개 테이블: "
            f"🟢 추가 {added}, 🟡 수정 {modified}, 🔴 삭제 {removed}, ⚪ 동일 {unchanged}"
        )

    def _add_diff_child_item(
        self,
        parent_item: QTreeWidgetItem,
        kind_prefix: str,
        diff,
        name_attr: str,
        show_renamed_old_name: bool = False,
    ):
        if diff.diff_type == DiffType.UNCHANGED:
            return
        diff_icon = self._get_diff_icon(diff.diff_type)
        sev_icon = self._get_severity_icon(diff.severity)
        sev_suffix = f" {sev_icon}" if sev_icon else ""
        name = getattr(diff, name_attr)
        if show_renamed_old_name and diff.diff_type == DiffType.RENAMED and diff.old_name:
            label = f"  {diff_icon} {kind_prefix}{diff.old_name} → {name}{sev_suffix}"
        else:
            label = f"  {diff_icon} {kind_prefix}{name}{sev_suffix}"
        child_item = QTreeWidgetItem([
            label,
            diff.diff_type.value,
            ""
        ])
        child_item.setData(0, Qt.ItemDataRole.UserRole, diff)
        self._apply_severity_background(child_item, diff.severity)
        parent_item.addChild(child_item)

    def _apply_severity_background(
        self, item: QTreeWidgetItem, severity: Optional[DiffSeverity]
    ):
        """심각도에 따라 트리 항목 배경색 설정"""
        if severity == DiffSeverity.CRITICAL:
            for col in range(3):
                item.setBackground(col, QColor("#ffeaea"))
        elif severity == DiffSeverity.WARNING:
            for col in range(3):
                item.setBackground(col, QColor("#fff8e1"))

    def _get_diff_icon(self, diff_type: DiffType) -> str:
        """차이 유형에 따른 아이콘"""
        icons = {
            DiffType.ADDED: "🟢",
            DiffType.REMOVED: "🔴",
            DiffType.MODIFIED: "🟡",
            DiffType.RENAMED: "🔄",
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

        if isinstance(diff, ColumnDiff):
            lines.append(f"컬럼: {diff.column_name}")
        elif isinstance(diff, IndexDiff):
            lines.append(f"인덱스: {diff.index_name}")
        elif isinstance(diff, ForeignKeyDiff):
            lines.append(f"FK: {diff.fk_name}")

        lines.append(f"상태: {diff.diff_type.value}")

        # RENAMED인 경우 이전 이름 표시
        if isinstance(diff, (IndexDiff, ForeignKeyDiff)) and diff.old_name:
            lines.append(f"이전 이름: {diff.old_name}")

        if diff.severity:
            sev_icon = self._get_severity_icon(diff.severity)
            lines.append(f"심각도: {sev_icon} {diff.severity.value}")

        if diff.differences:
            lines.append("\n[변경 내용]")
            for d in diff.differences:
                lines.append(f"  - {d}")

        if isinstance(diff, (IndexDiff, ForeignKeyDiff)) and diff.source_info:
            lines.append(f"\n[소스]\n  {diff.source_info}")

        if isinstance(diff, (IndexDiff, ForeignKeyDiff)) and diff.target_info:
            lines.append(f"\n[타겟]\n  {diff.target_info}")

        self.detail_text.setPlainText("\n".join(lines))

    def _generate_script(self):
        """동기화 스크립트 생성"""
        if not self._diffs:
            return

        # Critical 이슈가 있으면 경고
        if self._severity_summary and self._severity_summary.has_critical:
            reply = QMessageBox.warning(
                self,
                "Critical 이슈 감지",
                f"🔴 Critical 이슈 {self._severity_summary.critical}건이 발견되었습니다.\n"
                "Import 실패 위험이 있는 변경 사항이 포함되어 있습니다.\n\n"
                "그래도 동기화 스크립트를 생성하시겠습니까?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        # 비교 시작 시점에 캡처한 스키마를 사용한다 (완료 후 콤보를 바꿔도 결과와 일치)
        target_schema = self._compared_target_schema or self.target_schema_combo.currentText()
        generator = SyncScriptGenerator()
        script = generator.generate_sync_script(self._diffs, target_schema)

        # 스크립트 다이얼로그 열기
        dialog = SyncScriptDialog(self, script)
        dialog.exec()

    def closeEvent(self, event):
        """다이얼로그 닫힐 때"""
        # 진행 중인 스레드를 먼저 정리(시그널 해제 + 대기)한 뒤 커넥터를 정리해야
        # 스레드가 사용 중인 커넥터를 도중에 끊어버리는 경합을 피할 수 있다.
        self._cancel_compare_thread()
        self._cancel_schema_load_threads()

        # 연결 정리
        self._disconnect_connectors()

        super().closeEvent(event)

    def _cancel_compare_thread(self):
        """진행 중인 비교 스레드를 정리한다.

        다이얼로그가 파괴된 뒤 완료 콜백이 죽은 위젯을 건드리지 않도록
        시그널을 먼저 해제하고, 스레드가 끝날 때까지 기다린 뒤 반환한다.
        """
        thread = self._compare_thread
        if thread is None:
            return

        for signal, slot in (
            (thread.progress, self._on_progress),
            (thread.compare_finished, self._on_compare_finished),
            (thread.error, self._on_compare_error),
        ):
            try:
                signal.disconnect(slot)
            except (TypeError, RuntimeError):
                pass

        if thread.isRunning():
            thread.wait(5000)

    def _cancel_schema_load_threads(self):
        """진행 중인 스키마 로드 스레드를 정리한다."""
        for thread in list(self._pending_schema_threads):
            for signal, slot in (
                (thread.loaded, self._on_schema_loaded),
                (thread.load_failed, self._on_schema_load_failed),
            ):
                try:
                    signal.disconnect(slot)
                except (TypeError, RuntimeError):
                    pass
            if thread.isRunning():
                thread.wait(3000)


