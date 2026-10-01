"""SQL editor: production read-only session policy and change review (TF-STATUS-128).

- A profile whose environment is `production` always opens interactive sessions read-only
  (server enforced by the Rust core). Writing needs a per-window unlock confirmed by typing the
  schema name; closing the window or changing the target locks it again.
- A banner always shows target, environment, read-only/unlocked state and uncommitted changes.
- Before a manual-transaction commit the executed write statements are listed with their
  affected row counts.
"""
import logging
from typing import Any, Dict, List, Sequence, Tuple

from PyQt6.QtWidgets import QDialog, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton

from src.core.production_guard import Environment, ProductionGuard, SchemaConfirmDialog

logger = logging.getLogger(__name__)

PREVIEW_LEN = 90
MAX_LISTED = 15

_STYLE_READ_ONLY = ("#7f1d1d", "#fee2e2")   # production, locked
_STYLE_UNLOCKED = ("#ffffff", "#b91c1c")    # production, writes enabled (loud)
_STYLE_NEUTRAL = ("#1f2937", "#e5e7eb")     # other environments


def build_commit_summary(pending_queries: Sequence[Dict[str, Any]]) -> Tuple[str, int]:
    """Plain-text list of the write statements of this transaction and the total affected rows."""
    total = 0
    lines: List[str] = []
    for index, pending in enumerate(pending_queries):
        affected = int(pending.get("affected") or 0)
        total += affected
        if index < MAX_LISTED:
            preview = " ".join(str(pending.get("query", "")).split())
            if len(preview) > PREVIEW_LEN:
                preview = preview[:PREVIEW_LEN] + "..."
            lines.append(f"[{pending.get('type', '?')}] {affected}행 - {preview}")
    if len(pending_queries) > MAX_LISTED:
        lines.append(f"... 외 {len(pending_queries) - MAX_LISTED}건")
    return "\n".join(lines), total


def banner_content(env_label: str, target: str, is_production: bool, unlocked: bool,
                   pending_count: int, cell_edit_count: int) -> Tuple[str, Tuple[str, str]]:
    """Banner text and (foreground, background) colours."""
    if is_production and not unlocked:
        mode, style = "🔒 읽기 전용 (서버 강제)", _STYLE_READ_ONLY
    elif is_production:
        mode, style = "⚠️ 쓰기 해제됨 - 이 창에서만", _STYLE_UNLOCKED
    else:
        mode, style = "쓰기 가능", _STYLE_NEUTRAL
    if pending_count or cell_edit_count:
        parts = []
        if pending_count:
            parts.append(f"쿼리 {pending_count}건")
        if cell_edit_count:
            parts.append(f"셀 편집 {cell_edit_count}건")
        tx = "미커밋: " + ", ".join(parts)
    else:
        tx = "미커밋 변경 없음"
    return f"{env_label}   {target}   |   {mode}   |   {tx}", style


class ProductionSessionMixin:
    """Methods mixed into SQLEditorDialog."""

    _write_unlocked = False
    _unlock_target = None

    # ------------------------------------------------------------------ policy

    def _is_production_profile(self) -> bool:
        return ProductionGuard.is_production(self.config)

    def _session_read_only(self) -> bool:
        """True when interactive sessions of this window must be opened read-only."""
        return self._is_production_profile() and not self._write_unlocked

    # ------------------------------------------------------------------ banner

    def _build_session_banner(self) -> QFrame:
        self.session_banner = QFrame()
        layout = QHBoxLayout(self.session_banner)
        layout.setContentsMargins(10, 4, 10, 4)
        self.session_banner_label = QLabel()
        self.session_banner_label.setStyleSheet("background: transparent; border: none; font-weight: bold;")
        layout.addWidget(self.session_banner_label, 1)
        self.btn_write_lock = QPushButton()
        self.btn_write_lock.clicked.connect(self._on_toggle_write_lock)
        layout.addWidget(self.btn_write_lock)
        self._update_session_banner()
        return self.session_banner

    def _banner_target_text(self) -> str:
        if self.config.get("connection_mode") == "direct":
            host_info = f"{self.config.get('remote_host')}:{self.config.get('remote_port')}"
        else:
            host_info = f"localhost:{self.config.get('local_port', '?')} (SSH 터널)"
        combo = getattr(self, "db_combo", None)
        selected = combo.currentText().strip() if combo is not None else ""
        database = selected or self.config.get("default_database") or self.config.get("default_schema") or "(선택 없음)"
        return f"{host_info} / {database}"

    def _update_session_banner(self) -> None:
        banner = getattr(self, "session_banner", None)
        if banner is None:
            return
        environment = ProductionGuard.get_environment(self.config)
        env_label = SchemaConfirmDialog.ENV_LABELS.get(environment, "⚪ 환경 미분류")
        pending = len(getattr(self, "pending_queries", []))
        try:
            cell_edits = sum(len(ctx["pending_edits"]) for _, ctx in self._collect_all_pending_edits())
        except Exception:
            cell_edits = 0
        text, (fg, bg) = banner_content(
            env_label, self._banner_target_text(), self._is_production_profile(),
            self._write_unlocked, pending, cell_edits,
        )
        banner.setStyleSheet(f"QFrame {{ background-color: {bg}; border-radius: 4px; }} QLabel {{ color: {fg}; }}")
        self.session_banner_label.setText(text)
        if self._is_production_profile():
            self.btn_write_lock.setVisible(True)
            self.btn_write_lock.setText("🔒 읽기 전용으로 복귀" if self._write_unlocked else "🔓 쓰기 잠금 해제...")
        else:
            self.btn_write_lock.setVisible(False)

    # ------------------------------------------------------------------ unlock / lock

    def _on_toggle_write_lock(self) -> None:
        if getattr(self, "_query_executing", False):
            QMessageBox.warning(self, "경고", "쿼리 실행 중에는 변경할 수 없습니다.")
            return
        if self._write_unlocked:
            self._lock_writes()
        else:
            self._unlock_writes()

    def _confirmation_name(self) -> str:
        combo = getattr(self, "db_combo", None)
        selected = combo.currentText().strip() if combo is not None else ""
        return selected or self.config.get("default_database") or self.config.get("default_schema") \
            or str(self.config.get("name", "production"))

    def _unlock_writes(self) -> None:
        name = self._confirmation_name()
        dialog = SchemaConfirmDialog(
            self, "읽기 전용 해제 (이 창의 세션을 쓰기 가능으로 다시 연결)", name, Environment.PRODUCTION,
            "이 창에서만 해제됩니다. 창을 닫거나 대상 DB/스키마를 바꾸면 다시 읽기 전용입니다.",
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        self._write_unlocked = True
        self._unlock_target = self._database_and_schema_for_selection(self.db_combo.currentText().strip())
        self._close_db_connection()  # next run reconnects without the read-only flag
        self.message_text.append("⚠️ 운영 읽기 전용 해제: 이 창의 세션이 쓰기 가능으로 다시 연결됩니다.")
        self._update_session_banner()

    def _lock_writes(self) -> None:
        if self.pending_queries or self._collect_all_pending_edits():
            QMessageBox.warning(self, "경고", "미커밋 변경을 커밋하거나 롤백한 뒤 읽기 전용으로 돌아갈 수 있습니다.")
            return
        self._write_unlocked = False
        self._unlock_target = None
        self._close_db_connection()
        self.message_text.append("🔒 읽기 전용으로 복귀했습니다.")
        self._update_session_banner()

    def _relock_if_target_changed(self, target) -> None:
        """Changing DB/schema ends the unlock (called after the persistent session was handled)."""
        if self._write_unlocked and self._unlock_target is not None and target != self._unlock_target:
            self._write_unlocked = False
            self._unlock_target = None
            self.message_text.append("🔒 대상이 바뀌어 읽기 전용으로 복귀했습니다.")
        self._update_session_banner()

    # ------------------------------------------------------------------ change review

    def _confirm_commit_summary(self) -> bool:
        """List this transaction's write statements and affected rows before committing."""
        if not self.pending_queries:
            return True
        text, total = build_commit_summary(self.pending_queries)
        reply = QMessageBox.question(
            self, "커밋 확인",
            f"이번 트랜잭션에서 실행한 쓰기 {len(self.pending_queries)}건 (영향 행 합계 {total}행)을 커밋합니다.\n\n{text}",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return reply == QMessageBox.StandardButton.Yes
