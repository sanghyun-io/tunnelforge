"""
Rust DB Core 기반 Export/Import 마법사
"""
from PyQt6.QtWidgets import QDialog, QMessageBox

from src.core.db_connector import MySQLConnector
from src.core.postgres_connector import PostgresConnector
from src.ui.dialogs.db_connection_dialog import DBConnectionDialog
from src.ui.dialogs.db_export_dialog import (
    RustDumpExportDialog,
)
from src.ui.dialogs.db_import_dialog import (
    RustDumpImportDialog,
    # 테스트 하위호환 재노출
    format_import_row_labels,
    import_overall_percent,
    displayed_import_percent,
    format_import_visible_telemetry,
    _sanitized_rust_event,
    _sanitize_plain_rust_line,
)
from src.ui.dialogs.db_orphan_dialog import (
    OrphanRecordDialog,
    # 테스트 하위호환 재노출
    OrphanAnalysisWorker,
    OrphanReportWorker,
)


class RustDumpWizard:
    """Rust DB Core Export/Import 마법사"""

    def __init__(self, parent=None, tunnel_engine=None, config_manager=None, preselected_tunnel=None):
        self.parent = parent
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.preselected_tunnel = preselected_tunnel

    def _connect_preselected_tunnel(self) -> tuple:
        """미리 선택된 터널로 연결 - (connector, connection_info) 반환"""
        if not self.preselected_tunnel:
            return None, None

        tunnel = self.preselected_tunnel
        tid = tunnel.get('id')
        is_direct = tunnel.get('connection_mode') == 'direct'

        # 자격 증명 가져오기
        db_user, db_password = self.config_manager.get_tunnel_credentials(tid)
        if not db_user:
            QMessageBox.warning(
                self.parent, "경고",
                "DB 자격 증명이 저장되어 있지 않습니다."
            )
            return None, None

        # 연결 정보 결정
        if is_direct:
            host = tunnel['remote_host']
            port = int(tunnel['remote_port'])
        elif self.tunnel_engine.is_running(tid):
            host, port = self.tunnel_engine.get_connection_info(tid)
        else:
            QMessageBox.warning(
                self.parent, "경고",
                "터널이 활성화되어 있지 않습니다."
            )
            return None, None

        db_engine = tunnel.get('db_engine')
        database = tunnel.get('default_database') or (
            tunnel.get('default_schema') if db_engine == 'mysql' else None
        )

        if db_engine == "postgresql":
            connector = PostgresConnector(host, port, db_user, db_password, database)
        else:
            connector = MySQLConnector(host, port, db_user, db_password, database)
        success, msg = connector.connect()

        if not success:
            QMessageBox.critical(
                self.parent, "연결 오류",
                f"DB 연결에 실패했습니다:\n{msg}"
            )
            return None, None

        # 연결 식별자 (Export 폴더명 등에 사용)
        connection_info = f"{tunnel.get('name', 'Unknown')}_{db_user}"

        return connector, connection_info

    def _resolve_connector(self, need_connection_info: bool = False) -> tuple:
        """Export/Import/고아 레코드 검사에서 사용할 커넥터를 준비한다."""
        if self.preselected_tunnel:
            return self._connect_preselected_tunnel()

        conn_dialog = DBConnectionDialog(
            self.parent,
            tunnel_engine=self.tunnel_engine,
            config_manager=self.config_manager
        )

        if conn_dialog.exec() != QDialog.DialogCode.Accepted:
            return None, None

        connector = conn_dialog.get_connector()
        if not connector:
            return None, None

        connection_info = (
            conn_dialog.get_connection_identifier()
            if need_connection_info else None
        )
        return connector, connection_info

    def start_export(self) -> bool:
        """Export 마법사 시작"""
        connector, connection_info = self._resolve_connector(need_connection_info=True)
        if not connector:
            return False

        # 2단계: Export
        export_dialog = RustDumpExportDialog(
            self.parent,
            connector=connector,
            config_manager=self.config_manager,
            connection_info=connection_info
        )
        export_dialog.exec()

        return True

    def start_import(self) -> bool:
        """Import 마법사 시작"""
        connector, _ = self._resolve_connector()
        if not connector:
            return False

        # 2단계: Import
        import_dialog = RustDumpImportDialog(
            self.parent,
            connector=connector,
            config_manager=self.config_manager,
            tunnel_config=self.preselected_tunnel  # Production 환경 보호용
        )
        import_dialog.exec()

        return True

    def start_orphan_check(self) -> bool:
        """고아 레코드 검사 마법사 시작"""
        connector, _ = self._resolve_connector()
        if not connector:
            return False

        # 2단계: 고아 레코드 검사
        orphan_dialog = OrphanRecordDialog(
            self.parent,
            connector=connector,
            config_manager=self.config_manager
        )
        try:
            orphan_dialog.exec()
        finally:
            if connector:
                connector.disconnect()

        return True








