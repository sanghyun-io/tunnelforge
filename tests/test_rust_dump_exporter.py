"""
RustDumpExporter 테스트
"""
import io
import json
import pytest
from unittest.mock import patch, MagicMock


class TestRustDumpChecker:
    """RustDumpChecker 클래스 테스트"""

    def test_check_installation_success(self):
        """Rust DB Core 확인 성공 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpChecker

        with patch('src.exporters.rust_dump_exporter.DbCoreFacade') as facade_class:
            facade_class.return_value.hello.return_value = {
                "service": "tunnelforge-core",
                "protocol_version": "1",
                "capabilities": ["dump.run", "dump.import"],
            }
            installed, msg, version = RustDumpChecker.check_installation()

        assert installed is True
        assert 'tunnelforge-core' in msg
        assert version is not None

    def test_check_installation_not_found(self):
        """Rust DB Core 미설치 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpChecker

        with patch('src.exporters.rust_dump_exporter.DbCoreFacade') as facade_class:
            facade_class.return_value.hello.side_effect = FileNotFoundError()
            installed, msg, version = RustDumpChecker.check_installation()

        assert installed is False
        assert '찾을 수 없습니다' in msg
        assert version is None

    def test_check_installation_timeout(self):
        """Rust DB Core 타임아웃 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpChecker

        with patch('src.exporters.rust_dump_exporter.DbCoreFacade') as facade_class:
            facade_class.return_value.hello.side_effect = TimeoutError()
            installed, msg, version = RustDumpChecker.check_installation()

        assert installed is False
        assert '시간 초과' in msg

    def test_check_installation_always_boundedly_shuts_down_dedicated_facade(self):
        from src.core.db_core_service import DEFAULT_SHUTDOWN_TIMEOUT_SECONDS
        from src.exporters.rust_dump_exporter import RustDumpChecker

        with patch('src.exporters.rust_dump_exporter.DbCoreFacade') as facade_class:
            facade = facade_class.return_value
            facade.hello.return_value = {
                "service": "tunnelforge-core",
                "protocol_version": "1",
                "capabilities": ["dump.run", "dump.import"],
            }

            installed, _msg, _version = RustDumpChecker.check_installation()

        assert installed is True
        facade.client.shutdown.assert_called_once_with(
            timeout_seconds=DEFAULT_SHUTDOWN_TIMEOUT_SECONDS,
        )

    def test_check_installation_leaves_no_live_real_owner(self, monkeypatch):
        from src.core.db_core_service import DbCoreFacade, DbCoreServiceClient
        from src.exporters import rust_dump_exporter
        from src.exporters.rust_dump_exporter import RustDumpChecker

        class _Process:
            def __init__(self):
                process = self

                class _Writer(io.StringIO):
                    def write(self, data):
                        request = json.loads(data)
                        process.stdout = io.StringIO(json.dumps({
                            "event": "result",
                            "command": "service.hello",
                            "request_id": request["request_id"],
                            "success": True,
                            "service": "tunnelforge-core",
                            "protocol_version": 1,
                            "process_version": 1,
                            "process_capabilities": [
                                "request.deadline",
                                "request.strict_id",
                                "process.generation",
                                "mutation.outcome_indeterminate",
                            ],
                            "capabilities": ["dump.run", "dump.import"],
                            "max_jsonl_frame_bytes": 1_048_576,
                            "max_assembled_event_bytes": 64 * 1024 * 1024,
                            "max_assembled_event_chunks": 4_096,
                            "max_assembled_event_nodes": 65_536,
                            "max_assembled_event_depth": 128,
                        }) + "\n")
                        return super().write(data)

                self.stdin = _Writer()
                self.stdout = io.StringIO()
                self.stderr = io.StringIO()
                self.terminated = False

            def poll(self):
                return 0 if self.terminated else None

            def terminate(self):
                self.terminated = True

        client = DbCoreServiceClient(
            executable="fake-core",
            process_factory=lambda *args, **kwargs: _Process(),
        )
        facade = DbCoreFacade(client)
        owner = client.owner_thread
        monkeypatch.setattr(rust_dump_exporter, "DbCoreFacade", lambda: facade)

        installed, _msg, _version = RustDumpChecker.check_installation()

        assert installed is True
        assert not owner.is_alive()

    def test_get_install_guide(self):
        """설치 가이드 반환 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpChecker

        guide = RustDumpChecker.get_install_guide()

        assert 'Windows' in guide
        assert 'macOS' in guide
        assert 'Linux' in guide
        assert 'tunnelforge-core' in guide


class TestRustDumpConfig:
    """RustDumpConfig 클래스 테스트"""

    def test_config_does_not_expose_plaintext_uri_accessor(self):
        """평문 비밀번호가 포함된 URI 접근자는 존재하지 않는다"""
        from src.exporters.rust_dump_exporter import RustDumpConfig

        config = RustDumpConfig("localhost", 3306, "root", "secret123")

        assert not hasattr(config, "get_uri")
        assert "secret123" not in config.get_masked_uri()

    def test_get_masked_uri(self):
        """마스킹된 URI 생성 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpConfig

        config = RustDumpConfig(
            host='db.example.com',
            port=3307,
            user='admin',
            password='my_password'
        )

        masked = config.get_masked_uri()

        assert 'admin' in masked
        assert 'my_password' not in masked
        assert '****' in masked
        assert 'db.example.com:3307' in masked

    def test_config_preserves_postgresql_engine(self):
        """PostgreSQL dump 설정은 Rust Core endpoint engine으로 보존된다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig

        config = RustDumpConfig(
            host='db.example.com',
            port=5432,
            user='postgres',
            password='secret',
            engine='postgresql',
        )

        assert config.engine == 'postgresql'

    def test_build_config_from_connector_preserves_values(self):
        from src.exporters.rust_dump_exporter import build_rust_dump_config

        class Connector:
            host = "db.example.com"
            port = 5432
            user = "postgres"
            password = "secret"
            engine = "postgresql"

        config = build_rust_dump_config(Connector())

        assert config.host == "db.example.com"
        assert config.port == 5432
        assert config.user == "postgres"
        assert config.password == "secret"
        assert config.engine == "postgresql"

    def test_build_config_from_connector_uses_legacy_defaults(self):
        from src.exporters.rust_dump_exporter import build_rust_dump_config

        config = build_rust_dump_config(object())

        assert config.host == "127.0.0.1"
        assert config.port == 3306
        assert config.user == "root"
        assert config.password == ""
        assert config.engine == "mysql"


class TestForeignKeyResolver:
    """ForeignKeyResolver 클래스 테스트"""

    @pytest.fixture
    def mock_connector(self):
        """MySQLConnector Mock"""
        connector = MagicMock()
        return connector

    def test_get_all_dependencies(self, mock_connector):
        """전체 FK 의존성 조회"""
        from src.exporters.rust_dump_exporter import ForeignKeyResolver

        mock_connector.execute.return_value = [
            {'TABLE_NAME': 'posts', 'REFERENCED_TABLE_NAME': 'users'},
            {'TABLE_NAME': 'comments', 'REFERENCED_TABLE_NAME': 'posts'},
            {'TABLE_NAME': 'comments', 'REFERENCED_TABLE_NAME': 'users'}
        ]

        resolver = ForeignKeyResolver(mock_connector)
        deps = resolver.get_all_dependencies('blog')

        assert 'posts' in deps
        assert 'users' in deps['posts']
        assert 'comments' in deps
        assert 'posts' in deps['comments']
        assert 'users' in deps['comments']


class TestRustDumpExporter:
    """RustDumpExporter 클래스 테스트"""

    def test_exporter_initialization(self):
        """Exporter 초기화 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config)

        assert exporter.config == config
        assert not hasattr(exporter, "_connector")

    def test_export_full_schema_uses_rust_dump_command(self, tmp_path):
        """전체 스키마 export가 Rust dump.run을 호출"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                if on_event:
                    on_event({"event": "table_progress", "table": "users", "status": "dumping", "current": 1, "total": 1})
                return {"success": True, "tables": 1, "rows_dumped": 2}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config, facade=facade)

        success, msg = exporter.export_full_schema('app', str(tmp_path / 'dump'))

        assert success is True
        assert facade.payload["source"]["engine"] == "mysql"
        assert facade.payload["source"]["database"] == "app"
        assert facade.payload["output_dir"].endswith("dump")
        assert facade.payload["threads"] == 8
        assert facade.payload["chunk_size"] == 50000
        assert facade.payload["data_format"] == "tsv"
        assert facade.payload["compression"] == "zstd"
        assert "2" in msg

    def test_export_full_schema_passes_zstd_compression_to_rust_dump(self, tmp_path):
        """압축 선택이 Rust dump.run payload로 전달됨"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_dumped": 1}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config, facade=facade)

        success, _ = exporter.export_full_schema(
            'app',
            str(tmp_path / 'dump'),
            compression='zstd',
        )

        assert success is True
        assert facade.payload["compression"] == "zstd"

    def test_export_tables_passes_selected_tables_to_rust_dump(self, tmp_path):
        """부분 export가 선택 테이블 목록을 Rust core에 전달"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 2, "rows_dumped": 0}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config, facade=facade)

        success, msg, tables = exporter.export_tables(
            'app',
            ['users', 'orders'],
            str(tmp_path / 'dump'),
            include_fk_parents=False,
        )

        assert success is True
        assert tables == ['users', 'orders']
        assert facade.payload["tables"] == ['users', 'orders']
        assert facade.payload["threads"] == 8
        assert facade.payload["chunk_size"] == 50000
        assert facade.payload["data_format"] == "tsv"
        assert facade.payload["compression"] == "zstd"

    def test_export_tables_resolves_fk_parents_through_rust_schema_inspect(
        self, tmp_path, monkeypatch
    ):
        """부분 export FK 부모 자동 포함은 Python MySQLConnector를 열지 않는다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FailingConnector:
            def __init__(self, *args, **kwargs):
                raise AssertionError("partial export must not instantiate MySQLConnector")

        class FakeFacade:
            def inspect_schema(self, endpoint):
                self.inspect_endpoint = endpoint
                return {
                    "tables": [
                        {"name": "users", "foreign_keys": []},
                        {
                            "name": "orders",
                            "foreign_keys": [{"name": "fk_orders_users", "referenced_table": "users"}],
                        },
                        {
                            "name": "order_items",
                            "foreign_keys": [{"name": "fk_items_orders", "referenced_table": "orders"}],
                        },
                    ]
                }

            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 3, "rows_dumped": 0}

        monkeypatch.setattr("src.exporters.rust_dump_exporter.MySQLConnector", FailingConnector)
        facade = FakeFacade()
        config = RustDumpConfig("localhost", 3306, "root", "password")
        exporter = RustDumpExporter(config, facade=facade)

        success, msg, tables = exporter.export_tables(
            "app",
            ["order_items"],
            str(tmp_path / "dump"),
            include_fk_parents=True,
        )

        assert success is True
        assert tables == ["order_items", "orders", "users"]
        assert facade.inspect_endpoint.database == "app"
        assert facade.payload["tables"] == ["order_items", "orders", "users"]
        assert "3개 테이블" in msg

    def test_resolve_required_tables_from_rust_schema_no_deps(self):
        """FK 의존성이 없으면 선택한 테이블만 정렬되어 반환된다"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def inspect_schema(self, endpoint):
                self.inspect_endpoint = endpoint
                return {
                    "tables": [
                        {"name": "users", "foreign_keys": []},
                        {"name": "products", "foreign_keys": []},
                    ]
                }

        facade = FakeFacade()
        config = RustDumpConfig("localhost", 3306, "root", "password")
        exporter = RustDumpExporter(config, facade=facade)

        required, added = exporter._resolve_required_tables_from_rust_schema(
            ["products", "users"], "app"
        )

        assert required == ["products", "users"]
        assert added == []
        assert facade.inspect_endpoint.database == "app"

    def test_resolve_required_tables_from_rust_schema_transitive_deps(self):
        """전이적 FK 의존성(order_items -> orders -> users)이 모두 required에 포함된다"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def inspect_schema(self, endpoint):
                self.inspect_endpoint = endpoint
                return {
                    "tables": [
                        {"name": "users", "foreign_keys": []},
                        {
                            "name": "orders",
                            "foreign_keys": [{"name": "fk_orders_users", "referenced_table": "users"}],
                        },
                        {
                            "name": "order_items",
                            "foreign_keys": [{"name": "fk_items_orders", "referenced_table": "orders"}],
                        },
                    ]
                }

        facade = FakeFacade()
        config = RustDumpConfig("localhost", 3306, "root", "password")
        exporter = RustDumpExporter(config, facade=facade)

        required, added = exporter._resolve_required_tables_from_rust_schema(
            ["order_items"], "app"
        )

        assert required == ["order_items", "orders", "users"]
        assert added == ["orders", "users"]
        assert facade.inspect_endpoint.database == "app"

    def test_export_full_schema_reports_view_count(self, tmp_path):
        """전체 export 결과에 View 개수가 메시지로 포함됨"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                return {"success": True, "tables": 3, "rows_dumped": 10, "views": 2}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config, facade=facade)

        success, msg = exporter.export_full_schema('app', str(tmp_path / 'dump'))

        assert success is True
        assert "View 2개" in msg

    def test_export_full_schema_preserves_postgresql_engine_in_rust_payload(self, tmp_path):
        """PostgreSQL export는 Rust dump.run payload에 postgresql endpoint를 보낸다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_dumped": 0}

        facade = FakeFacade()
        config = RustDumpConfig(
            "pg.example.com",
            5432,
            "postgres",
            "secret",
            engine="postgresql",
        )
        exporter = RustDumpExporter(config, facade=facade)

        success, _msg = exporter.export_full_schema("public", str(tmp_path / "dump"))

        assert success is True
        assert facade.payload["source"]["engine"] == "postgresql"
        assert facade.payload["source"]["database"] == "public"

    def test_exporter_default_uses_dedicated_facade_and_shuts_it_down(self, tmp_path, monkeypatch):
        """기본 Exporter는 공유 facade 대신 전용 DbCoreFacade를 생성하고, 사용 후 종료한다"""
        from src.exporters import rust_dump_exporter
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        created = []

        class FakeDedicatedFacade:
            def __init__(self):
                self.client = MagicMock()
                created.append(self)

            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_dumped": 1}

        monkeypatch.setattr(rust_dump_exporter, "DbCoreFacade", FakeDedicatedFacade)

        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config)

        success, _msg = exporter.export_full_schema('app', str(tmp_path / 'dump'))

        assert success is True
        assert len(created) == 1
        created[0].client.shutdown.assert_called_once()

    def test_exporter_does_not_shutdown_injected_facade(self, tmp_path):
        """주입된 facade는 export 성공 후에도 종료되지 않는다"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def __init__(self):
                self.client = MagicMock()

            def run_dump(self, payload, on_event=None):
                return {"success": True, "tables": 1, "rows_dumped": 1}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        exporter = RustDumpExporter(config, facade=facade)

        success, _msg = exporter.export_full_schema('app', str(tmp_path / 'dump'))

        assert success is True
        facade.client.shutdown.assert_not_called()

    def test_exporter_dedicated_residual_shutdown_is_not_swallowed(self, tmp_path, monkeypatch):
        from src.core.db_core_service import (
            DbCoreOutcome,
            DbCoreRequestKind,
            DbCoreServiceError,
            is_db_core_facade_retained,
            release_db_core_facade_retry,
        )
        from src.exporters import rust_dump_exporter
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        residual = DbCoreServiceError(
            "owner still alive",
            code="db_core_residual_process",
            request_kind=DbCoreRequestKind.MUTATION,
            outcome=DbCoreOutcome.FAILED,
        )

        class FakeDedicatedFacade:
            def __init__(self):
                self.client = MagicMock()
                self.client.shutdown.side_effect = residual

            def run_dump(self, payload, on_event=None):
                return {"success": True, "tables": 1, "rows_dumped": 1}

        monkeypatch.setattr(rust_dump_exporter, "DbCoreFacade", FakeDedicatedFacade)
        exporter = RustDumpExporter(
            RustDumpConfig('localhost', 3306, 'root', 'password')
        )

        with pytest.raises(DbCoreServiceError) as raised:
            exporter.export_full_schema('app', str(tmp_path / 'dump'))

        assert raised.value is residual
        assert is_db_core_facade_retained(exporter.facade) is True
        release_db_core_facade_retry(exporter.facade)

    def test_dedicated_residual_facade_is_retained_until_retry_succeeds(self):
        from src.core.db_core_facade import (
            is_db_core_facade_retained,
            retry_retained_db_core_facades,
        )
        from src.core.db_core_service import DbCoreOutcome, DbCoreServiceError
        from src.exporters.rust_dump_exporter import _shutdown_owned_facade

        residual = DbCoreServiceError(
            "owner still alive",
            code="db_core_residual_process",
            outcome=DbCoreOutcome.FAILED,
        )
        facade = MagicMock()
        facade.client.shutdown.side_effect = [residual, None]

        with pytest.raises(DbCoreServiceError) as raised:
            _shutdown_owned_facade(facade, True)

        assert raised.value is residual
        assert is_db_core_facade_retained(facade) is True

        retry_retained_db_core_facades(timeout_seconds=0.5)

        assert facade.client.shutdown.call_count == 2
        assert is_db_core_facade_retained(facade) is False


def test_export_schema_wrapper_preserves_postgresql_engine(monkeypatch, tmp_path):
    from src.exporters import rust_dump_exporter

    captured = {}

    class FakeExporter:
        def __init__(self, config):
            captured["config"] = config

        def export_full_schema(self, schema, output_dir, threads, progress_callback=None):
            captured["schema"] = schema
            captured["output_dir"] = output_dir
            captured["threads"] = threads
            return True, "ok"

    monkeypatch.setattr(rust_dump_exporter, "RustDumpExporter", FakeExporter)

    success, message = rust_dump_exporter.export_schema(
        "pg.example.com",
        5432,
        "postgres",
        "secret",
        "analytics",
        str(tmp_path),
        engine="postgresql",
    )

    assert success is True
    assert message == "ok"
    assert captured["config"].engine == "postgresql"
    assert captured["schema"] == "analytics"


def test_export_tables_wrapper_preserves_postgresql_engine(monkeypatch, tmp_path):
    from src.exporters import rust_dump_exporter

    captured = {}

    class FakeExporter:
        def __init__(self, config):
            captured["config"] = config

        def export_tables(
            self,
            schema,
            tables,
            output_dir,
            threads,
            include_fk_parents=True,
            progress_callback=None,
        ):
            captured["schema"] = schema
            captured["tables"] = tables
            captured["output_dir"] = output_dir
            captured["threads"] = threads
            captured["include_fk_parents"] = include_fk_parents
            return True, "ok", ["users"]

    monkeypatch.setattr(rust_dump_exporter, "RustDumpExporter", FakeExporter)

    success, message, exported = rust_dump_exporter.export_tables(
        "pg.example.com",
        5432,
        "postgres",
        "secret",
        "analytics",
        ["users"],
        str(tmp_path),
        include_fk_parents=False,
        engine="postgresql",
    )

    assert success is True
    assert message == "ok"
    assert exported == ["users"]
    assert captured["config"].engine == "postgresql"
    assert captured["tables"] == ["users"]
    assert captured["include_fk_parents"] is False


def test_import_dump_wrapper_preserves_postgresql_engine(monkeypatch, tmp_path):
    from src.exporters import rust_dump_exporter

    captured = {}

    class FakeImporter:
        def __init__(self, config):
            captured["config"] = config

        def import_dump(
            self,
            input_dir,
            target_schema=None,
            threads=8,
            import_mode="replace",
            progress_callback=None,
            table_chunk_progress_callback=None,
        ):
            captured["input_dir"] = input_dir
            captured["target_schema"] = target_schema
            captured["threads"] = threads
            captured["import_mode"] = import_mode
            return True, "ok", {}

    monkeypatch.setattr(rust_dump_exporter, "RustDumpImporter", FakeImporter)

    success, message, results = rust_dump_exporter.import_dump(
        "pg.example.com",
        5432,
        "postgres",
        "secret",
        str(tmp_path),
        target_schema="analytics",
        import_mode="merge",
        engine="postgresql",
    )

    assert success is True
    assert message == "ok"
    assert results == {}
    assert captured["config"].engine == "postgresql"
    assert captured["target_schema"] == "analytics"
    assert captured["import_mode"] == "merge"


class TestRustDumpImporter:
    """RustDumpImporter 클래스 테스트"""

    def test_importer_initialization(self):
        """Importer 초기화 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config)

        assert importer.config == config

    def test_import_dump_uses_rust_import_command(self, tmp_path):
        """TunnelForge dump import가 Rust dump.import를 호출"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app","tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                self.payload = payload
                if on_event:
                    on_event({"event": "table_progress", "table": "users", "status": "completed", "current": 1, "total": 1})
                return {"success": True, "tables": 1, "rows_imported": 1}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config, facade=facade)

        success, msg, results = importer.import_dump(str(dump_dir), import_mode='replace')

        assert success is True
        assert facade.payload["target"]["database"] == "app"
        assert facade.payload["input_dir"] == str(dump_dir)
        assert facade.payload["threads"] == 8
        assert "mysql_local_infile_policy" not in facade.payload
        assert results["users"]["status"] == "done"
        assert "1" in msg

    def test_importer_default_uses_dedicated_facade_and_shuts_it_down(self, tmp_path, monkeypatch):
        """기본 Importer는 전용 DbCoreFacade를 생성하고, import 완료 후 종료한다"""
        from src.exporters import rust_dump_exporter
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app","tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        created = []

        class FakeDedicatedFacade:
            def __init__(self):
                self.client = MagicMock()
                created.append(self)

            def import_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_imported": 1}

        monkeypatch.setattr(rust_dump_exporter, "DbCoreFacade", FakeDedicatedFacade)

        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config)

        success, _msg, _results = importer.import_dump(str(dump_dir), import_mode='replace')

        assert success is True
        assert len(created) == 1
        created[0].client.shutdown.assert_called_once()

    def test_import_dump_preserves_postgresql_engine_in_rust_payload(self, tmp_path):
        """PostgreSQL import는 Rust dump.import payload에 postgresql endpoint를 보낸다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"public",'
            '"tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_imported": 1}

        facade = FakeFacade()
        config = RustDumpConfig(
            'pg.example.com',
            5432,
            'postgres',
            'secret',
            engine='postgresql',
        )
        importer = RustDumpImporter(config, facade=facade)

        success, _msg, _results = importer.import_dump(str(dump_dir), import_mode='replace')

        assert success is True
        assert facade.payload["target"]["engine"] == "postgresql"
        assert facade.payload["target"]["database"] == "public"

    def test_import_dump_forwards_timezone_and_strict_manifest(self, tmp_path):
        """Import 의도(timezone/strict manifest)가 Rust payload로 전달된다"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app",'
            '"tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_imported": 1}

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config, facade=facade)

        success, _msg, _results = importer.import_dump(
            str(dump_dir),
            import_mode='recreate',
            timezone_sql="SET SESSION time_zone = '+09:00'",
        )

        assert success is True
        assert facade.payload["mode"] == "recreate"
        assert facade.payload["timezone_sql"] == "SET SESSION time_zone = '+09:00'"
        assert facade.payload["strict_manifest"] is True

    def test_import_dump_omits_timezone_sql_when_none(self, tmp_path):
        """Neutral import leaves timezone_sql out of the Rust payload."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app",'
            '"tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_imported": 1}

        facade = FakeFacade()
        importer = RustDumpImporter(
            RustDumpConfig('localhost', 3306, 'root', 'password'),
            facade=facade,
        )

        success, _msg, _results = importer.import_dump(
            str(dump_dir),
            import_mode='replace',
            timezone_sql=None,
        )

        assert success is True
        assert "timezone_sql" not in facade.payload

    def test_import_dump_preserves_classified_core_error(self, tmp_path):
        """Rust classified error code/scope가 Python import 메시지에 보존된다"""
        from src.core.db_core_service import DbCoreServiceError
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app",'
            '"tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                raise DbCoreServiceError(
                    "export_invalid: users: missing chunk_sha256; "
                    "table users has chunks but no chunk_sha256 metadata"
                )

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config, facade=facade)

        success, msg, results = importer.import_dump(str(dump_dir), import_mode='replace')

        assert success is False
        assert results["users"]["status"] == "error"
        assert "export_invalid" in results["users"]["message"]
        assert "missing chunk_sha256" in results["users"]["message"]
        assert "export_invalid" in msg
        assert "users" in msg
        assert "missing chunk_sha256" in msg

    def test_import_dump_marks_remaining_tables_error_on_exception(self, tmp_path):
        """import 도중 예외가 발생하면 done이 아닌 모든 테이블이 error로 표시된다 (재시도 UI 회귀 테스트)"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        dump_dir.mkdir(parents=True)
        (dump_dir / '_tunnelforge_dump.json').write_text(
            json.dumps(
                {
                    "format": "tunnelforge-dump",
                    "format_version": 1,
                    "database": "app",
                    "tables": [
                        {"name": "users", "path": "0001_users", "rows": 1, "chunks": 1},
                        {"name": "orders", "path": "0002_orders", "rows": 1, "chunks": 1},
                        {"name": "products", "path": "0003_products", "rows": 1, "chunks": 1},
                    ],
                }
            ),
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                if on_event:
                    on_event({"event": "table_progress", "table": "users", "status": "completed", "current": 1, "total": 3})
                    on_event({"event": "table_progress", "table": "orders", "status": "importing", "current": 2, "total": 3})
                raise RuntimeError("connection lost during import")

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config, facade=facade)

        status_events = []
        success, msg, results = importer.import_dump(
            str(dump_dir),
            import_mode='replace',
            table_status_callback=lambda table, status, message: status_events.append((table, status, message)),
        )

        assert success is False
        assert results["users"]["status"] == "done"
        assert results["orders"]["status"] == "error"
        assert results["products"]["status"] == "error"
        assert "connection lost during import" in results["orders"]["message"]
        assert "connection lost during import" in results["products"]["message"]
        assert "connection lost during import" in msg

        error_events = {(table, status) for table, status, _ in status_events if status == "error"}
        assert ("orders", "error") in error_events
        assert ("products", "error") in error_events

    def test_import_dump_reports_view_results_in_message(self, tmp_path):
        """import 결과의 views_imported/failed/skipped 가 메시지에 반영됨"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app",'
            '"tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                return {
                    "success": True,
                    "tables": 1,
                    "rows_imported": 1,
                    "views_imported": ["ref_vendor_codes_view", "another_view"],
                    "views_failed": [{"name": "broken_view", "error": "missing base table"}],
                    "views_skipped_cross_engine": [],
                }

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config, facade=facade)

        success, msg, _results = importer.import_dump(str(dump_dir), import_mode='replace')

        assert success is True
        assert "View 2개" in msg
        assert "broken_view" in msg
        assert "생성 실패" in msg

    def test_import_dump_reports_cross_engine_skipped_views(self, tmp_path):
        """크로스 엔진 import 시 건너뛴 View 개수가 메시지에 표시됨"""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        table_dir = dump_dir / '0001_users'
        table_dir.mkdir(parents=True)
        (table_dir / 'chunk_000001.jsonl').write_text('{"id":1}\n', encoding='utf-8')
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":1,"database":"app",'
            '"tables":[{"name":"users","path":"0001_users","rows":1,"chunks":1}]}',
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                return {
                    "success": True,
                    "tables": 1,
                    "rows_imported": 1,
                    "views_skipped_cross_engine": ["v1", "v2", "v3"],
                }

        facade = FakeFacade()
        config = RustDumpConfig('localhost', 3306, 'root', 'password')
        importer = RustDumpImporter(config, facade=facade)

        success, msg, _results = importer.import_dump(str(dump_dir), import_mode='replace')

        assert success is True
        assert "크로스 엔진 View 3개" in msg

    def test_import_row_progress_reports_chunk_counts_to_callback(self):
        """Import row_progress는 rows/total이 아니라 chunks_done/chunks_total을 chunk callback으로 전달한다."""
        from src.exporters.rust_dump_exporter import emit_core_event

        chunk_events = []

        emit_core_event(
            {
                "event": "row_progress",
                "table": "df_subs",
                "rows": 100_000,
                "total": 387_398,
                "chunk_rows": 50_000,
                "chunks_done": 2,
                "chunks_total": 8,
                "chunk_index": 4,
                "load_ms": 1250,
                "strategy": "parallel_load_data_local_infile",
            },
            table_chunk_progress_callback=lambda table, done, total: chunk_events.append(
                (table, done, total)
            ),
        )

        assert chunk_events == [("df_subs", 2, 8)]

    def test_import_row_progress_forwards_cumulative_totals_to_detail_callback(self):
        from src.exporters.rust_dump_exporter import emit_core_event

        details = []

        emit_core_event(
            {
                "event": "row_progress",
                "table": "orders",
                "rows": 25,
                "total": 100,
                "table_rows_done": 25,
                "table_rows_total": 100,
                "overall_rows_done": 1_025,
                "overall_rows_total": 2_000,
                "chunk_rows": 25,
                "load_ms": 500,
                "strategy": "load_data_local_infile",
            },
            detail_callback=details.append,
        )

        assert details == [{
            "event": "row_progress",
            "table": "orders",
            "percent": 51,
            "rows_done": 25,
            "rows_total": 100,
            "overall_rows_done": 1_025,
            "overall_rows_total": 2_000,
            "chunk_rows": 25,
            "rows_sec": 50,
            "speed": "50 rows/s",
            "chunk_index": None,
            "chunks_done": None,
            "chunks_total": None,
            "strategy": "load_data_local_infile",
            "stream_ms": None,
            "read_ms": None,
            "write_ms": None,
            "load_ms": 500,
        }]

    def test_import_phase_hides_local_infile_fallback_noise(self):
        from src.exporters.rust_dump_exporter import emit_core_event

        messages = []

        emit_core_event(
            {
                "event": "phase",
                "message": "MySQL local_infile is disabled; using safe Rust INSERT fallback",
                "strategy": "insert_fallback",
            },
            progress_callback=messages.append,
        )

        assert messages == []

    def test_import_phase_hides_temporary_local_infile_noise(self):
        from src.exporters.rust_dump_exporter import emit_core_event

        messages = []

        emit_core_event(
            {
                "event": "phase",
                "message": "MySQL local_infile is disabled; trying temporary SET GLOBAL local_infile=ON",
                "strategy": "temporary_local_infile",
            },
            progress_callback=messages.append,
        )
        emit_core_event(
            {
                "event": "phase",
                "message": "MySQL local_infile temporarily enabled; using fast LOAD DATA LOCAL import",
                "strategy": "load_data_local_infile",
                "performance": "fast_path",
            },
            progress_callback=messages.append,
        )

        assert messages == []

    def test_import_metadata_reports_table_rows_and_total_rows(self, tmp_path):
        """Import 대시보드가 전체 row 진행률을 계산할 수 있도록 manifest rows를 전달한다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / "dump"
        users_dir = dump_dir / "0001_users"
        orders_dir = dump_dir / "0002_orders"
        users_dir.mkdir(parents=True)
        orders_dir.mkdir(parents=True)
        (users_dir / "chunk_000001.tsv").write_text("1\ta\n", encoding="utf-8")
        (orders_dir / "chunk_000001.tsv").write_text("1\t1\n", encoding="utf-8")
        (dump_dir / "_tunnelforge_dump.json").write_text(
            json.dumps(
                {
                    "format": "tunnelforge-dump",
                    "format_version": 2,
                    "database": "app",
                    "tables": [
                        {"name": "users", "path": "0001_users", "rows": 1, "chunks": 1},
                        {"name": "orders", "path": "0002_orders", "rows": 2, "chunks": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )

        importer = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "password"))

        metadata = importer._analyze_dump_metadata(str(dump_dir))

        assert metadata["table_rows"] == {"users": 1, "orders": 2}
        assert metadata["total_rows"] == 3

    def test_import_metadata_rejects_manifest_path_outside_dump_dir(self, tmp_path):
        """악의적 manifest path가 dump 폴더 밖 chunk를 참조하면 metadata 분석을 거부한다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / "dump"
        outside_dir = tmp_path / "outside"
        dump_dir.mkdir()
        outside_dir.mkdir()
        (outside_dir / "chunk_000001.tsv").write_text("secret\n", encoding="utf-8")
        (dump_dir / "_tunnelforge_dump.json").write_text(
            json.dumps(
                {
                    "format": "tunnelforge-dump",
                    "format_version": 2,
                    "database": "app",
                    "tables": [
                        {"name": "users", "path": "../outside", "rows": 1, "chunks": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )

        importer = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "password"))

        assert importer._analyze_dump_metadata(str(dump_dir)) is None

    def test_import_metadata_rejects_chunk_symlink_outside_dump_dir(self, tmp_path):
        """dump 폴더 내부 chunk symlink가 외부 파일을 가리키면 metadata 분석을 거부한다."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / "dump"
        table_dir = dump_dir / "0001_users"
        outside_dir = tmp_path / "outside"
        table_dir.mkdir(parents=True)
        outside_dir.mkdir()
        outside_file = outside_dir / "chunk_000001.tsv"
        outside_file.write_text("secret\n", encoding="utf-8")
        chunk_link = table_dir / "chunk_000001.tsv"
        try:
            chunk_link.symlink_to(outside_file)
        except OSError:
            return
        (dump_dir / "_tunnelforge_dump.json").write_text(
            json.dumps(
                {
                    "format": "tunnelforge-dump",
                    "format_version": 2,
                    "database": "app",
                    "tables": [
                        {"name": "users", "path": "0001_users", "rows": 1, "chunks": 1},
                    ],
                }
            ),
            encoding="utf-8",
        )

        importer = RustDumpImporter(RustDumpConfig("localhost", 3306, "root", "password"))

        assert importer._analyze_dump_metadata(str(dump_dir)) is None


class TestConvenienceFunctions:
    """편의 함수 테스트"""

    def test_check_rust_dump_function(self):
        """check_rust_dump 함수 테스트"""
        from src.exporters.rust_dump_exporter import check_rust_dump

        with patch('src.exporters.rust_dump_exporter.DbCoreFacade') as facade_class:
            facade_class.return_value.hello.return_value = {
                "service": "tunnelforge-core",
                "protocol_version": "1",
                "capabilities": ["dump.run", "dump.import"],
            }
            installed, msg = check_rust_dump()

        assert isinstance(installed, bool)
        assert isinstance(msg, str)


class TestTableProgressTracker:
    """TableProgressTracker 클래스 테스트"""

    def test_tracker_initialization_with_metadata(self):
        """메타데이터로 초기화 테스트"""
        from src.exporters.rust_dump_exporter import TableProgressTracker

        metadata = {
            'chunk_counts': {'users': 10, 'orders': 50},
            'table_sizes': {'users': 1024000, 'orders': 5120000},
            'total_bytes': 6144000
        }

        tracker = TableProgressTracker(metadata)

        assert tracker.chunk_counts == metadata['chunk_counts']
        assert tracker.table_sizes == metadata['table_sizes']
        assert tracker.total_bytes == 6144000

    def test_tracker_initialization_without_metadata(self):
        """메타데이터 없이 초기화 테스트"""
        from src.exporters.rust_dump_exporter import TableProgressTracker

        tracker = TableProgressTracker(None)

        assert tracker.chunk_counts == {}
        assert tracker.table_sizes == {}
        assert tracker.total_bytes == 0

    def test_format_size(self):
        """크기 포맷팅 테스트"""
        from src.exporters.rust_dump_exporter import TableProgressTracker

        tracker = TableProgressTracker(None)

        assert tracker.format_size(500) == '500 B'
        assert 'KB' in tracker.format_size(2048)
        assert 'MB' in tracker.format_size(5 * 1024 * 1024)
        assert 'GB' in tracker.format_size(2 * 1024 * 1024 * 1024)

    def test_get_table_info(self):
        """테이블 정보 조회 테스트"""
        from src.exporters.rust_dump_exporter import TableProgressTracker

        metadata = {
            'chunk_counts': {'users': 5},
            'table_sizes': {'users': 1024000},
            'total_bytes': 1024000
        }

        tracker = TableProgressTracker(metadata)
        size, chunks = tracker.get_table_info('users')

        assert size == 1024000
        assert chunks == 5

    def test_get_table_info_not_found(self):
        """존재하지 않는 테이블 정보 조회"""
        from src.exporters.rust_dump_exporter import TableProgressTracker

        tracker = TableProgressTracker(None)
        size, chunks = tracker.get_table_info('non_existent')

        assert size == 0
        assert chunks == 1  # 기본값


class TestCoreEventForwarding:
    """Rust Core event forwarding contract tests"""

    def test_emit_core_event_forwards_dump_plan_to_detail_callback(self):
        from src.exporters.rust_dump_exporter import emit_core_event

        details = []
        emit_core_event(
            {
                "event": "dump_plan",
                "tables_total": 2,
                "rows_total": 150,
                "tables": [{"name": "a", "rows": 100}, {"name": "b", "rows": 50}],
            },
            detail_callback=details.append,
        )

        assert details == [{
            "event": "dump_plan",
            "tables_total": 2,
            "rows_total": 150,
            "tables": [{"name": "a", "rows": 100}, {"name": "b", "rows": 50}],
        }]

    def test_emit_core_event_counts_only_completed_table_progress(self):
        from src.exporters.rust_dump_exporter import emit_core_event

        table_progress = []
        statuses = []

        emit_core_event(
            {"event": "table_progress", "table": "users", "status": "dumping", "current": 1, "total": 2},
            table_progress_callback=lambda current, total, table: table_progress.append((current, total, table)),
            table_status_callback=lambda table, status, message: statuses.append((table, status, message)),
        )
        emit_core_event(
            {"event": "table_progress", "table": "users", "status": "completed", "current": 1, "total": 2},
            table_progress_callback=lambda current, total, table: table_progress.append((current, total, table)),
            table_status_callback=lambda table, status, message: statuses.append((table, status, message)),
        )

        assert table_progress == [(1, 2, "users")]
        assert statuses == [("users", "loading", ""), ("users", "done", "")]

    def test_emit_core_event_forwards_dump_schedule_detail(self):
        from src.exporters.rust_dump_exporter import emit_core_event

        details = []

        emit_core_event(
            {
                "event": "dump_schedule",
                "threads": 8,
                "table_workers": 2,
                "range_workers_per_table": 4,
                "chunk_size": 50000,
                "data_format": "tsv",
                "compression": "zstd",
                "scheduled_tables": [{"name": "huge", "rows": 100, "estimated_chunks": 1}],
            },
            detail_callback=details.append,
        )

        assert details == [
            {
                "event": "dump_schedule",
                "threads": 8,
                "table_workers": 2,
                "range_workers_per_table": 4,
                "chunk_size": 50000,
                "data_format": "tsv",
                "compression": "zstd",
                "scheduled_tables": [{"name": "huge", "rows": 100, "estimated_chunks": 1}],
            }
        ]
