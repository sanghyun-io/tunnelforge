"""
Cross-Engine 마이그레이션 소스/타겟 엔드포인트 입력 폼
"""
from typing import Dict, Optional
from PyQt6.QtWidgets import QComboBox, QFormLayout, QGroupBox, QLineEdit, QSpinBox

from src.core.cross_engine_migration import (
    DEFAULT_MYSQL_PORT,
    DEFAULT_POSTGRESQL_DATABASE,
    DEFAULT_POSTGRESQL_PORT,
    DEFAULT_POSTGRESQL_SCHEMA,
    ConnectionEndpointInput,
    DatabaseEngine,
    make_connection_payload,
)
from src.core.i18n import translate_text
from src.ui.dialogs.ssh_host_key_dialog import ensure_ssh_host_trusted


class EndpointForm(QGroupBox):
    def __init__(
        self,
        title: str,
        default_engine: DatabaseEngine,
        tunnel_engine=None,
        config_manager=None,
        require_tunnel: bool = False,
    ):
        super().__init__(title)
        self.tunnel_engine = tunnel_engine
        self.config_manager = config_manager
        self.require_tunnel = require_tunnel
        self.engine_filter = None
        self._setup_ui(default_engine)

    def _setup_ui(self, default_engine: DatabaseEngine):
        layout = QFormLayout(self)

        self.combo_tunnel = QComboBox()
        self._load_tunnels()

        self.combo_engine = QComboBox()
        self.combo_engine.addItem("MySQL", DatabaseEngine.MYSQL.value)
        self.combo_engine.addItem("PostgreSQL", DatabaseEngine.POSTGRESQL.value)
        index = self.combo_engine.findData(default_engine.value)
        self.combo_engine.setCurrentIndex(index if index >= 0 else 0)
        self.combo_engine.setEnabled(False)
        self.combo_engine.setToolTip("터널 연결 정보에서 자동 인식됩니다.")

        self.input_host = QLineEdit("127.0.0.1")
        self.input_port = QSpinBox()
        self.input_port.setRange(1, 65535)
        self.input_port.setValue(
            DEFAULT_MYSQL_PORT if default_engine == DatabaseEngine.MYSQL else DEFAULT_POSTGRESQL_PORT
        )
        self.input_user = QLineEdit()
        self.input_password = QLineEdit()
        self.input_password.setEchoMode(QLineEdit.EchoMode.Password)
        self.input_database = QLineEdit()
        self.input_schema = QLineEdit()
        self.input_schema.setPlaceholderText("MySQL은 database, PostgreSQL은 schema")

        self.combo_engine.currentIndexChanged.connect(self._on_engine_changed)
        self.combo_tunnel.currentIndexChanged.connect(self._on_tunnel_selected)

        layout.addRow("기존 연결:", self.combo_tunnel)
        layout.addRow("Engine:", self.combo_engine)
        layout.addRow("Host:", self.input_host)
        layout.addRow("Port:", self.input_port)
        layout.addRow("User:", self.input_user)
        layout.addRow("Password:", self.input_password)
        layout.addRow("Database:", self.input_database)
        layout.addRow("Schema scope:", self.input_schema)
        self._apply_tunnel_only_state()

    def _load_tunnels(self):
        selected_data = self.combo_tunnel.currentData() if hasattr(self, "combo_tunnel") else None
        selected_id = selected_data.get("tunnel_id") if isinstance(selected_data, dict) else None
        self.combo_tunnel.clear()
        self.combo_tunnel.addItem("터널 목록에서 선택", None)

        seen = set()
        for config in self._configured_tunnels():
            tid = config.get("id")
            if not tid:
                continue
            if not self._passes_engine_filter(config):
                continue
            self.combo_tunnel.addItem(self._tunnel_display(config), self._tunnel_data(config))
            seen.add(tid)

        if self.tunnel_engine:
            for tunnel in self.tunnel_engine.get_active_tunnels():
                tid = tunnel.get("tunnel_id") or tunnel.get("id")
                if not tid or tid in seen:
                    continue
                config = getattr(self.tunnel_engine, "tunnel_configs", {}).get(tid, {})
                if not self._passes_engine_filter(config, tunnel):
                    continue
                self.combo_tunnel.addItem(self._tunnel_display(config, tunnel), self._tunnel_data(config, tunnel))

        if self.combo_tunnel.count() == 1:
            self.combo_tunnel.setItemText(0, "사용 가능한 터널 항목 없음")
        elif selected_id:
            for index in range(1, self.combo_tunnel.count()):
                data = self.combo_tunnel.itemData(index)
                if isinstance(data, dict) and data.get("tunnel_id") == selected_id:
                    self.combo_tunnel.setCurrentIndex(index)
                    break

    def set_engine_filter(self, allowed_engines):
        self.engine_filter = set(allowed_engines) if allowed_engines else None
        self._load_tunnels()

    def _passes_engine_filter(self, config: Dict, active_info: Optional[Dict] = None) -> bool:
        if not self.engine_filter:
            return True
        engine = self._known_engine(config, active_info)
        return engine in self.engine_filter

    def _configured_tunnels(self):
        if not self.config_manager:
            return []
        try:
            config = self.config_manager.load_config()
        except Exception:
            return []
        tunnels = config.get("tunnels", [])
        return tunnels if isinstance(tunnels, list) else []

    def _tunnel_display(self, config: Dict, active_info: Optional[Dict] = None) -> str:
        name = config.get("name") or (active_info or {}).get("name") or config.get("id", "Unknown")
        engine = self._known_engine(config, active_info)
        engine_key = engine or ""
        engine_label = {
            "mysql": "MySQL",
            "postgresql": "PostgreSQL",
        }.get(engine_key, "엔진 미확인")
        if active_info:
            host = active_info.get("host", "")
            port = active_info.get("port", "")
            return f"{name} ({engine_label}, {host}:{port}, 연결됨)"
        host = config.get("remote_host", "")
        port = config.get("remote_port", "")
        mode = "직접" if config.get("connection_mode") == "direct" else "터널"
        return f"{name} ({engine_label}, {host}:{port}, {mode})"

    def _tunnel_data(self, config: Dict, active_info: Optional[Dict] = None) -> Dict:
        tid = config.get("id") or (active_info or {}).get("tunnel_id") or (active_info or {}).get("id")
        host, port = self._connection_host_port(config, active_info)
        return {
            "tunnel_id": tid,
            "host": host,
            "port": port,
            "config": config,
        }

    def _known_engine(self, config: Dict, active_info: Optional[Dict] = None):
        engine = config.get("db_engine")
        if engine in ("mysql", "postgresql"):
            return engine
        return None

    def _connection_host_port(self, config: Dict, active_info: Optional[Dict] = None):
        if active_info and active_info.get("host") and active_info.get("port"):
            return active_info["host"], int(active_info["port"])

        tid = config.get("id")
        if self.tunnel_engine and tid and self.tunnel_engine.is_running(tid):
            host, port = self.tunnel_engine.get_connection_info(tid)
            if host and port:
                return host, int(port)

        if config.get("connection_mode") == "direct":
            return config.get("remote_host") or "127.0.0.1", int(config.get("remote_port", 0) or 0)
        return "127.0.0.1", int(config.get("local_port", 0) or config.get("remote_port", 0) or 0)

    def _on_engine_changed(self):
        if self.engine() == DatabaseEngine.MYSQL and self.input_port.value() == DEFAULT_POSTGRESQL_PORT:
            self.input_port.setValue(DEFAULT_MYSQL_PORT)
        elif self.engine() == DatabaseEngine.POSTGRESQL and self.input_port.value() == DEFAULT_MYSQL_PORT:
            self.input_port.setValue(DEFAULT_POSTGRESQL_PORT)
        if self.engine() == DatabaseEngine.MYSQL and not self.input_schema.text().strip():
            self.input_schema.setText(self.input_database.text().strip())
        elif self.engine() == DatabaseEngine.POSTGRESQL and not self.input_schema.text().strip():
            self.input_schema.setText(DEFAULT_POSTGRESQL_SCHEMA)

    def _on_tunnel_selected(self):
        data = self.combo_tunnel.currentData()
        if not data:
            return
        if not self._ensure_selected_tunnel_trusted(data):
            self.input_user.clear()
            self.input_password.clear()
            return
        self._apply_tunnel_data(data)

    def _ensure_selected_tunnel_trusted(self, data: Dict) -> bool:
        config = data.get("config") or {}
        if not config:
            return False
        return ensure_ssh_host_trusted(self, self.tunnel_engine, config)

    def _apply_tunnel_data(self, data: Dict, *, load_credentials: bool = True):
        host = data.get("host")
        port = data.get("port")
        config = data.get("config") or {}
        if host:
            self.input_host.setText(str(host))
        if port:
            self.input_port.setValue(int(port))

        engine = self._detect_engine(config)
        engine_index = self.combo_engine.findData(engine.value)
        if engine_index >= 0:
            self.combo_engine.setCurrentIndex(engine_index)

        database, schema = self._resolve_default_database_and_schema(
            self.engine(),
            config.get("default_database"),
            config.get("default_schema"),
        )
        if database:
            self.input_database.setText(database)
        if schema and (config.get("default_schema") or not self.input_schema.text().strip()):
            self.input_schema.setText(schema)

        if load_credentials and self.config_manager:
            db_user, db_password = self.config_manager.get_tunnel_credentials(data["tunnel_id"])
            if db_user:
                self.input_user.setText(db_user)
            if db_password:
                self.input_password.setText(db_password)

    @staticmethod
    def _resolve_default_database_and_schema(
        engine: DatabaseEngine,
        default_database,
        default_schema,
    ) -> tuple[str, str]:
        if engine == DatabaseEngine.POSTGRESQL:
            return (
                str(default_database) if default_database else DEFAULT_POSTGRESQL_DATABASE,
                str(default_schema) if default_schema else DEFAULT_POSTGRESQL_SCHEMA,
            )
        database = str(default_schema) if default_schema else ""
        schema = str(default_schema) if default_schema else ""
        return database, schema

    def _detect_engine(self, config: Dict) -> DatabaseEngine:
        configured = config.get("db_engine")
        if configured in ("mysql", "postgresql"):
            return DatabaseEngine(configured)
        return self.engine()

    def _apply_tunnel_only_state(self):
        if not self.require_tunnel:
            return
        self.input_host.setReadOnly(True)
        self.input_port.setEnabled(False)
        self.input_user.setReadOnly(True)
        self.input_password.setReadOnly(True)
        self.input_database.setReadOnly(True)

    def set_inputs_enabled(self, enabled: bool):
        """Lock/unlock the mutable inputs while a worker is running.

        `combo_engine` is intentionally excluded: it stays permanently disabled
        (auto-detected from the tunnel). For tunnel-only forms, host/port/user/
        password/database are already read-only/disabled via
        `_apply_tunnel_only_state` and must stay that way even after
        re-enabling; only `combo_tunnel` and `input_schema` are interactive.
        """
        self.combo_tunnel.setEnabled(enabled)
        if self.require_tunnel:
            self.input_schema.setEnabled(enabled)
        else:
            self.input_host.setEnabled(enabled)
            self.input_port.setEnabled(enabled)
            self.input_user.setEnabled(enabled)
            self.input_password.setEnabled(enabled)
            self.input_database.setEnabled(enabled)
            self.input_schema.setEnabled(enabled)

    def _prepare_selected_tunnel(self):
        data = self.combo_tunnel.currentData()
        if not data:
            raise ValueError(f"{self.title()}는 터널 목록에서 항목을 선택해야 합니다.")

        config = data.get("config") or {}
        tid = data.get("tunnel_id")
        if not self._ensure_selected_tunnel_trusted(data):
            raise ValueError(
                translate_text("SSH 호스트 키 승인이 완료되지 않았습니다.")
            )
        is_direct = config.get("connection_mode") == "direct"
        if (
            self.tunnel_engine
            and config
            and tid
            and not is_direct
            and not self.tunnel_engine.is_running(tid)
        ):
            success, message = self.tunnel_engine.start_tunnel(config)
            if not success:
                raise ValueError(f"{self.title()} 터널 시작 실패: {message}")
            host, port = self.tunnel_engine.get_connection_info(tid)
            if host and port:
                data["host"] = host
                data["port"] = int(port)

        self._apply_tunnel_data(data, load_credentials=False)

    def engine(self) -> DatabaseEngine:
        return DatabaseEngine(self.combo_engine.currentData())

    def payload(self, prepare_tunnel: bool = False) -> Dict:
        if self.require_tunnel:
            if prepare_tunnel:
                self._prepare_selected_tunnel()
            elif not self.combo_tunnel.currentData():
                raise ValueError(f"{self.title()}는 터널 목록에서 항목을 선택해야 합니다.")
        schema = self.input_schema.text().strip()
        database = self.input_database.text().strip()
        if self.engine() == DatabaseEngine.MYSQL:
            database = schema or database
            schema = database
        elif not schema:
            schema = DEFAULT_POSTGRESQL_SCHEMA
            if not database:
                database = DEFAULT_POSTGRESQL_DATABASE
        return ConnectionEndpointInput(
            engine=self.engine(),
            host=self.input_host.text().strip(),
            port=self.input_port.value(),
            user=self.input_user.text().strip(),
            password=self.input_password.text(),
            database=database,
            schema=schema,
        ).to_payload()
