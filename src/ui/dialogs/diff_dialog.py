"""
스키마 비교 다이얼로그
- 소스/타겟 연결 선택
- 스키마 비교 결과 표시
- 동기화 스크립트 생성
"""
import math
import random
from typing import List, Dict, Optional

from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QFormLayout,
    QLabel, QComboBox, QPushButton, QGroupBox,
    QTreeWidget, QTreeWidgetItem, QTextEdit, QSplitter,
    QWidget, QProgressBar, QMessageBox, QFileDialog,
    QHeaderView, QApplication
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QPen

from src.core.schema_diff import (
    SchemaExtractor, SchemaComparator, SyncScriptGenerator,
    TableDiff, DiffType, DiffSeverity, CompareLevel,
    SeverityClassifier, VersionContext, SeveritySummary
)
from src.core.db_connector import MySQLConnector
from src.core.logger import get_logger

logger = get_logger(__name__)


class SchemaCompareThread(QThread):
    """스키마 비교 백그라운드 스레드"""

    progress = pyqtSignal(str)
    finished = pyqtSignal(list, object, object)  # diffs, SeveritySummary, VersionContext
    error = pyqtSignal(str)

    def __init__(self, source_connector, target_connector,
                 source_schema: str, target_schema: str,
                 compare_level: CompareLevel = CompareLevel.STANDARD):
        super().__init__()
        self.source_connector = source_connector
        self.target_connector = target_connector
        self.source_schema = source_schema
        self.target_schema = target_schema
        self.compare_level = compare_level

    def run(self):
        try:
            # MySQL 버전 감지
            self.progress.emit("MySQL 버전 확인 중...")
            version_ctx = VersionContext(
                source_version=self.source_connector.get_db_version(),
                target_version=self.target_connector.get_db_version(),
                source_version_str=self.source_connector.get_db_version_string(),
                target_version_str=self.target_connector.get_db_version_string(),
            )

            self.progress.emit("소스 스키마 추출 중...")
            source_extractor = SchemaExtractor(self.source_connector)
            source_tables = source_extractor.extract_all_tables(self.source_schema)

            self.progress.emit("타겟 스키마 추출 중...")
            target_extractor = SchemaExtractor(self.target_connector)
            target_tables = target_extractor.extract_all_tables(self.target_schema)

            self.progress.emit("스키마 비교 중...")
            comparator = SchemaComparator()
            diffs = comparator.compare_schemas(
                source_tables, target_tables, self.compare_level
            )

            # 심각도 분류
            self.progress.emit("심각도 분류 중...")
            classifier = SeverityClassifier(version_ctx)
            diffs, summary = classifier.classify(diffs)

            self.finished.emit(diffs, summary, version_ctx)

        except Exception as e:
            self.error.emit(str(e))


class PixelLoadingWidget(QWidget):
    """스키마 비교 진행 중 Pixel 아트 애니메이션 위젯

    두 DB 아이콘 사이를 데이터 파티클이 흐르고,
    활성 단계의 DB가 펄스 효과로 강조된다.
    """

    TIPS = [
        "컬럼, 인덱스, FK 정보를 수집하고 있어요",
        "비교 완료 후 🟡 항목을 클릭하면 상세 내용을 볼 수 있어요",
        "동기화 스크립트로 타겟 DB 구조를 소스와 맞출 수 있어요",
        "인덱스와 Foreign Key 변경사항도 자동 감지합니다",
        "행 수(row count) 차이도 함께 비교합니다",
    ]

    PX = 4  # 1 pixel art pixel = 4×4 real pixels

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(90)
        self.setVisible(False)

        self._phase = "idle"   # idle / source / target / compare
        self._frame = 0
        self._tip_idx = 0
        self._status_text = ""
        self._particles: list[dict] = []

        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._tick)
        self._tip_timer = QTimer(self)
        self._tip_timer.timeout.connect(self._next_tip)

    # ------ public API ------

    def start(self, status: str):
        """애니메이션 시작"""
        self._status_text = status
        self._phase = self._detect_phase(status)
        self._frame = 0
        self._tip_idx = 0
        self._init_particles()
        self.setVisible(True)
        self._anim_timer.start(80)   # ~12 FPS
        self._tip_timer.start(3500)
        self.update()

    def update_status(self, status: str):
        """진행 상태(phase) 업데이트"""
        self._status_text = status
        new_phase = self._detect_phase(status)
        if new_phase != self._phase:
            self._phase = new_phase
            self._init_particles()
        self.update()

    def stop(self):
        """애니메이션 정지 및 숨김"""
        self._anim_timer.stop()
        self._tip_timer.stop()
        self.setVisible(False)
        self._phase = "idle"

    # ------ internals ------

    @staticmethod
    def _detect_phase(s: str) -> str:
        if "소스" in s:
            return "source"
        if "타겟" in s:
            return "target"
        if "비교" in s:
            return "compare"
        return "source"

    def _tick(self):
        self._frame += 1
        self._update_particles()
        self.update()

    def _next_tip(self):
        self._tip_idx = (self._tip_idx + 1) % len(self.TIPS)

    def _init_particles(self):
        self._particles = []
        n = 8 if self._phase == "compare" else 5
        for _ in range(n):
            self._particles.append({
                "x": random.uniform(0, 1),
                "y": random.uniform(0.2, 0.8),
                "speed": random.uniform(0.012, 0.028),
                "size": random.choice([1, 2]),
                "alt": random.random() > 0.5,
            })

    def _update_particles(self):
        for i, p in enumerate(self._particles):
            p["x"] += p["speed"]
            if p["x"] > 1.0:
                self._particles[i] = {
                    "x": 0.0,
                    "y": random.uniform(0.2, 0.8),
                    "speed": random.uniform(0.012, 0.028),
                    "size": random.choice([1, 2]),
                    "alt": random.random() > 0.5,
                }

    # ------ painting ------

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)

        w = self.width()
        px = self.PX
        cx = w // 2

        # DB icon positions
        src_x = cx - 80
        tgt_x = cx + 52
        db_y = 2

        # Source DB
        src_active = self._phase in ("source", "compare")
        src_c = QColor("#3498db") if src_active else QColor("#bdc3c7")
        self._draw_db(painter, src_x, db_y, px, src_c,
                       pulse=(self._phase == "source"))

        # Target DB
        tgt_active = self._phase in ("target", "compare")
        tgt_c = QColor("#2ecc71") if tgt_active else QColor("#bdc3c7")
        self._draw_db(painter, tgt_x, db_y, px, tgt_c,
                       pulse=(self._phase == "target"))

        # Labels
        painter.setPen(QPen(QColor("#7f8c8d")))
        painter.setFont(QFont("Consolas", 7))
        label_y = db_y + 9 * px + 2
        painter.drawText(src_x, label_y, 7 * px, 12,
                         Qt.AlignmentFlag.AlignCenter, "SRC")
        painter.drawText(tgt_x, label_y, 7 * px, 12,
                         Qt.AlignmentFlag.AlignCenter, "TGT")

        # Dotted connection line
        line_left = src_x + 8 * px
        line_right = tgt_x - px
        line_y = db_y + 4 * px
        painter.setPen(QPen(QColor("#dcdde1"), 1, Qt.PenStyle.DotLine))
        painter.drawLine(line_left, line_y, line_right, line_y)

        # Flowing data particles
        gap = line_right - line_left
        if gap > 0:
            for p in self._particles:
                px_x = int(line_left + p["x"] * gap)
                px_y = int(line_y - 6 + p["y"] * 12)
                c = QColor("#f39c12") if not p["alt"] else QColor("#e74c3c")
                s = p["size"] * px
                painter.fillRect(px_x, px_y, s, s, c)

        # Compare phase: pulsing center indicator
        if self._phase == "compare":
            pulse = abs(math.sin(self._frame * 0.15))
            size = int(4 + pulse * 6)
            c = QColor("#9b59b6")
            c.setAlpha(int(120 + pulse * 135))
            painter.fillRect(cx - size // 2, line_y - size // 2, size, size, c)

        # Status text with animated dots
        dots = "." * ((self._frame // 4) % 4)
        text = self._status_text.rstrip(".") + dots
        painter.setPen(QPen(QColor("#2c3e50")))
        painter.setFont(QFont("Consolas", 10, QFont.Weight.Bold))
        painter.drawText(0, 52, w, 18, Qt.AlignmentFlag.AlignCenter, text)

        # Rotating tip
        painter.setPen(QPen(QColor("#95a5a6")))
        painter.setFont(QFont("", 9))
        painter.drawText(0, 72, w, 16, Qt.AlignmentFlag.AlignCenter,
                         self.TIPS[self._tip_idx])

        painter.end()

    def _draw_db(self, painter: QPainter, x: int, y: int,
                 px: int, color: QColor, pulse: bool = False):
        """Pixel art database cylinder (7×9 art pixels)"""
        light = QColor(color).lighter(130)
        dark = QColor(color).darker(130)
        fill = QColor("#f8f9fa")

        # Pulse glow
        if pulse and (self._frame // 6) % 2 == 0:
            g = QColor(color)
            g.setAlpha(35)
            painter.fillRect(x - px, y - px, 9 * px, 11 * px, g)

        # Top cap
        painter.fillRect(x + 2 * px, y, 3 * px, px, light)
        painter.fillRect(x + px, y + px, 5 * px, px, color)

        # Side walls
        for r in range(2, 8):
            painter.fillRect(x, y + r * px, px, px, color)
            painter.fillRect(x + 6 * px, y + r * px, px, px, color)

        # Interior fill (two sections)
        painter.fillRect(x + px, y + 2 * px, 5 * px, 2 * px, fill)
        painter.fillRect(x + px, y + 5 * px, 5 * px, 2 * px, fill)

        # Middle divider
        painter.fillRect(x, y + 4 * px, 7 * px, px, light)

        # Bottom
        painter.fillRect(x + px, y + 7 * px, 5 * px, px, color)
        painter.fillRect(x + 2 * px, y + 8 * px, 3 * px, px, dark)


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

        result = self._resolve_connection_params(tunnel_id)
        if not result[0]:
            schema_combo.addItem(f"({result[1]})")
            return

        _, host, port, db_user, db_password = result

        # DB 연결
        connector = None
        try:
            connector = MySQLConnector(
                host=host, port=port,
                user=db_user, password=db_password
            )

            success, msg = connector.connect()
            if not success:
                schema_combo.addItem("(연결 실패)")
                return

            # 스키마 목록 조회
            schemas = connector.get_schemas(use_cache=False)
            for schema_name in schemas:
                schema_combo.addItem(schema_name)

        except Exception as e:
            logger.error(f"스키마 로드 실패: {e}")
            schema_combo.addItem("(오류)")
        finally:
            if connector:
                try:
                    connector.disconnect()
                except Exception:
                    pass

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
                if self._source_connector:
                    self._source_connector.disconnect()
                    self._source_connector = None
                raise Exception("타겟 연결 실패")

        except Exception as e:
            # 연결 정리
            if self._source_connector:
                try:
                    self._source_connector.disconnect()
                except Exception:
                    pass
                self._source_connector = None
            if self._target_connector:
                try:
                    self._target_connector.disconnect()
                except Exception:
                    pass
                self._target_connector = None
            QMessageBox.critical(self, "연결 오류", f"DB 연결 실패: {e}")
            return

        # UI 업데이트
        self.compare_btn.setEnabled(False)
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
        self._compare_thread.finished.connect(self._on_compare_finished)
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

    def _on_compare_error(self, error: str):
        """비교 오류"""
        self.compare_btn.setEnabled(True)
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

            # 컬럼 차이
            if diff.column_diffs:
                for col_diff in diff.column_diffs:
                    if col_diff.diff_type != DiffType.UNCHANGED:
                        col_icon = self._get_diff_icon(col_diff.diff_type)
                        sev_icon = self._get_severity_icon(col_diff.severity)
                        sev_suffix = f" {sev_icon}" if sev_icon else ""
                        col_item = QTreeWidgetItem([
                            f"  {col_icon} {col_diff.column_name}{sev_suffix}",
                            col_diff.diff_type.value,
                            ""
                        ])
                        col_item.setData(0, Qt.ItemDataRole.UserRole, col_diff)
                        self._apply_severity_background(col_item, col_diff.severity)
                        item.addChild(col_item)

            # 인덱스 차이
            if diff.index_diffs:
                for idx_diff in diff.index_diffs:
                    if idx_diff.diff_type != DiffType.UNCHANGED:
                        idx_icon = self._get_diff_icon(idx_diff.diff_type)
                        sev_icon = self._get_severity_icon(idx_diff.severity)
                        sev_suffix = f" {sev_icon}" if sev_icon else ""
                        # RENAMED: old_name → new_name 표시
                        if idx_diff.diff_type == DiffType.RENAMED and idx_diff.old_name:
                            label = (f"  {idx_icon} [IDX] {idx_diff.old_name} "
                                     f"→ {idx_diff.index_name}{sev_suffix}")
                        else:
                            label = f"  {idx_icon} [IDX] {idx_diff.index_name}{sev_suffix}"
                        idx_item = QTreeWidgetItem([
                            label,
                            idx_diff.diff_type.value,
                            ""
                        ])
                        idx_item.setData(0, Qt.ItemDataRole.UserRole, idx_diff)
                        self._apply_severity_background(idx_item, idx_diff.severity)
                        item.addChild(idx_item)

            # FK 차이
            if diff.fk_diffs:
                for fk_diff in diff.fk_diffs:
                    if fk_diff.diff_type != DiffType.UNCHANGED:
                        fk_icon = self._get_diff_icon(fk_diff.diff_type)
                        sev_icon = self._get_severity_icon(fk_diff.severity)
                        sev_suffix = f" {sev_icon}" if sev_icon else ""
                        # RENAMED: old_name → new_name 표시
                        if fk_diff.diff_type == DiffType.RENAMED and fk_diff.old_name:
                            label = (f"  {fk_icon} [FK] {fk_diff.old_name} "
                                     f"→ {fk_diff.fk_name}{sev_suffix}")
                        else:
                            label = f"  {fk_icon} [FK] {fk_diff.fk_name}{sev_suffix}"
                        fk_item = QTreeWidgetItem([
                            label,
                            fk_diff.diff_type.value,
                            ""
                        ])
                        fk_item.setData(0, Qt.ItemDataRole.UserRole, fk_diff)
                        self._apply_severity_background(fk_item, fk_diff.severity)
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

        if hasattr(diff, 'column_name'):
            lines.append(f"컬럼: {diff.column_name}")
        elif hasattr(diff, 'index_name'):
            lines.append(f"인덱스: {diff.index_name}")
        elif hasattr(diff, 'fk_name'):
            lines.append(f"FK: {diff.fk_name}")

        lines.append(f"상태: {diff.diff_type.value}")

        # RENAMED인 경우 이전 이름 표시
        if hasattr(diff, 'old_name') and diff.old_name:
            lines.append(f"이전 이름: {diff.old_name}")

        if hasattr(diff, 'severity') and diff.severity:
            sev_icon = self._get_severity_icon(diff.severity)
            lines.append(f"심각도: {sev_icon} {diff.severity.value}")

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
            except Exception:
                pass

        if self._target_connector:
            try:
                self._target_connector.disconnect()
            except Exception:
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

        # 데이터 미복사 경고
        data_warning = QLabel(
            "📋 이 스크립트는 테이블 구조(DDL)만 동기화합니다.\n"
            "데이터는 복사되지 않습니다. 데이터 이전은 Export/Import 기능을 사용하세요."
        )
        data_warning.setStyleSheet(
            "background-color: #d1ecf1; color: #0c5460; "
            "padding: 10px; border-radius: 4px; font-weight: bold;"
        )
        data_warning.setWordWrap(True)
        layout.addWidget(data_warning)

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
