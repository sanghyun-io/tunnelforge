"""
스키마 비교 진행 중 표시하는 픽셀 아트 로딩 위젯
"""
import math
import random
from PyQt6.QtWidgets import QWidget
from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont, QColor, QPainter, QPen


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

        # DB icon positions (중앙 대칭 배치, DB 아이콘 너비 = 7*px = 28px)
        db_icon_w = 7 * px      # 28px
        half_gap = 52           # 중앙에서 각 아이콘까지의 거리
        src_x = cx - half_gap - db_icon_w   # 소스: 중앙 왼쪽
        tgt_x = cx + half_gap               # 타겟: 중앙 오른쪽
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
