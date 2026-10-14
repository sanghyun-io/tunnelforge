"""
RustDumpExporter 테스트
"""
import json
import pytest
from unittest.mock import patch, MagicMock


class TestRustDumpChecker:
    """RustDumpChecker 클래스 테스트"""

    def test_export_surfaces_manifest_warnings(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        facade = MagicMock()
        facade.run_dump.return_value = {
            "success": True, "tables": 1, "rows_dumped": 1,
            "manifest_warnings": ["Object not exported: trigger:audit"],
        }
        progress = []
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)
        success, message = exporter.export_full_schema("app", str(tmp_path), progress_callback=progress.append)
        assert success
        line = "Export 경고: 내보낼 때 일부 객체가 빠졌습니다. (trigger:audit)"
        assert line in progress
        assert message.endswith("\n" + line)

    def test_export_warnings_are_shown_in_korean_and_kept_raw_in_the_raw_output(self, tmp_path):
        from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES, RustDumpConfig, RustDumpExporter

        visibility = "The export account's privileges do not prove it sees every object, so some may be missing: triggers (TRIGGER)"
        result = {"event": "result", "success": True, "tables": 1, "rows_dumped": 1, "manifest_warnings": [
            visibility, "Workers used independent snapshots without a shared lock", "Something new: x"]}

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                on_event(result)
                return result

        raw, progress = [], []
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=FakeFacade())
        success, message = exporter.export_full_schema(
            "app", str(tmp_path), progress_callback=progress.append, raw_output_callback=raw.append
        )

        assert success
        assert progress[-3:] == [
            f"Export 경고: {CORE_CODE_SENTENCES['visibility_unproven']}",
            f"Export 경고: {CORE_CODE_SENTENCES['unsynchronized_snapshot']}",
            f"Export 경고: {CORE_CODE_SENTENCES['source_dump_warning']} (Something new: x)",
        ]
        assert message.endswith("\n".join(progress[-3:]))
        assert "privileges do not prove" not in message
        assert any(visibility in line for line in raw), "the core's own text stays in the raw output"

    def test_manifest_warning_prefixes_follow_the_core(self):
        import re
        from pathlib import Path
        from src.exporters.rust_dump_exporter import _MANIFEST_WARNING_CODES

        source = (Path(__file__).resolve().parents[1] / "migration_core" / "src" / "adapters.rs").read_text(encoding="utf-8")
        core = re.findall(r'pub\(crate\) const WARNING_\w+: &str = "([^"]+)";', source)
        assert core
        for text in core:
            assert any(text.startswith(prefix) for prefix, _ in _MANIFEST_WARNING_CODES), text
        for prefix, _ in _MANIFEST_WARNING_CODES:
            assert any(text.startswith(prefix) for text in core), prefix

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

    def test_export_full_schema_single_connection_forces_one_worker(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {
                    "success": True,
                    "tables": 1,
                    "rows_dumped": 2,
                    "snapshot_policy": "mysql_single_connection_consistent_snapshot",
                }

        facade = FakeFacade()
        exporter = RustDumpExporter(
            RustDumpConfig("localhost", 3306, "root", "password"),
            facade=facade,
        )

        success, message = exporter.export_full_schema(
            "app",
            str(tmp_path / "dump"),
            threads=8,
            mysql_snapshot_mode="single_connection",
        )

        assert success is True
        assert facade.payload["threads"] == 1
        assert facade.payload["mysql_snapshot_mode"] == "single_connection"
        assert "mysql_snapshot_fallback" not in facade.payload
        assert message.endswith(" · 같은 시점으로 내보냄 · 동시 작업 1개 (연결 하나로 읽음)")

    def test_parallel_no_backup_lock_is_an_accepted_snapshot_mode(self):
        from src.exporters.rust_dump_exporter import MYSQL_SNAPSHOT_MODES

        assert "parallel_no_backup_lock" in MYSQL_SNAPSHOT_MODES

    def test_export_full_schema_parallel_no_backup_lock_keeps_threads(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {
                    "success": True,
                    "tables": 1,
                    "rows_dumped": 2,
                    "snapshot_policy": "mysql_single_connection_consistent_snapshot",
                }

        facade = FakeFacade()
        exporter = RustDumpExporter(
            RustDumpConfig("localhost", 3306, "root", "password"),
            facade=facade,
        )

        success, message = exporter.export_full_schema(
            "app",
            str(tmp_path / "dump"),
            threads=8,
            mysql_snapshot_mode="parallel_no_backup_lock",
        )

        assert success is True
        assert facade.payload["threads"] == 8
        assert facade.payload["mysql_snapshot_mode"] == "parallel_no_backup_lock"
        assert message.endswith("동시 작업 1개 (연결 하나로 읽음)"), "the core reads it over one connection"

    @pytest.mark.parametrize("mode, policy, suffix", [
        (
            "parallel_table_lock",
            "mysql_table_lock_shared_snapshot",
            " · 같은 시점으로 내보냄 · 동시 작업 8개 (테이블만 잠깐 잠금)",
        ),
        (
            "parallel_unsynchronized",
            "mysql_parallel_unsynchronized_snapshots",
            " · 테이블마다 시점이 다를 수 있음 · 동시 작업 8개 (시점을 맞추지 않고 나눠 읽음)",
        ),
    ])
    def test_new_parallel_snapshot_modes_keep_threads_and_name_the_policy(
        self, tmp_path, mode, policy, suffix
    ):
        from src.exporters.rust_dump_exporter import (
            MYSQL_SNAPSHOT_MODES, RustDumpConfig, RustDumpExporter,
        )

        facade = MagicMock()
        facade.run_dump.return_value = {
            "success": True, "tables": 1, "rows_dumped": 2, "snapshot_policy": policy,
        }
        exporter = RustDumpExporter(
            RustDumpConfig("localhost", 3306, "root", "password"), facade=facade
        )

        success, message = exporter.export_full_schema(
            "app", str(tmp_path / "dump"), threads=8, mysql_snapshot_mode=mode
        )

        payload = facade.run_dump.call_args.args[0]
        assert mode in MYSQL_SNAPSHOT_MODES
        assert success is True
        assert payload["threads"] == 8
        assert payload["mysql_snapshot_mode"] == mode
        assert message.endswith(suffix)

    @pytest.mark.parametrize("fallback", [None, "single_connection", "parallel_unsynchronized"])
    def test_exporter_sends_auto_snapshot_mode_and_fallback(self, tmp_path, fallback):
        from src.exporters.rust_dump_exporter import MYSQL_SNAPSHOT_AUTO_CAPABILITY, RustDumpConfig, RustDumpExporter

        facade = MagicMock()
        facade.hello.return_value = {"capabilities": ["dump.run", MYSQL_SNAPSHOT_AUTO_CAPABILITY]}
        facade.run_dump.return_value = {"success": True, "tables": 1, "rows_dumped": 2}
        facade.inspect_schema.return_value = {"tables": []}
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)
        extra = {} if fallback is None else {"mysql_snapshot_fallback": fallback}

        assert exporter.export_full_schema("app", str(tmp_path / "full"), threads=8, mysql_snapshot_mode="auto", **extra)[0]
        assert exporter.export_tables("app", ["t"], str(tmp_path / "part"), threads=8, mysql_snapshot_mode="auto", **extra)[0]

        for call in facade.run_dump.call_args_list:
            payload = call.args[0]
            assert payload["mysql_snapshot_mode"] == "auto"
            assert payload["mysql_snapshot_fallback"] == (fallback or "single_connection")
            assert payload["threads"] == 8, "the core lowers the worker count itself"

    def test_exporter_rejects_unknown_snapshot_fallback(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        facade = MagicMock()
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

        success, message = exporter.export_full_schema(
            "app", str(tmp_path / "dump"), mysql_snapshot_mode="auto", mysql_snapshot_fallback="parallel_strict"
        )

        assert success is False
        assert "unsupported mysql_snapshot_fallback" in message
        facade.run_dump.assert_not_called()

    @pytest.mark.parametrize("capabilities", [["dump.run", "dump.import"], None])
    def test_auto_snapshot_is_not_sent_to_a_core_that_does_not_list_it(self, tmp_path, capabilities):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class OlderCore:
            ran = False

            def hello(self):
                return {"capabilities": capabilities}

            def run_dump(self, payload, on_event=None):
                self.ran = True
                raise AssertionError("an older core refuses every auto export in English")

        facade = OlderCore()
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

        success, message = exporter.export_full_schema("app", str(tmp_path / "dump"), mysql_snapshot_mode="auto")
        assert (success, message) == (
            False, "이 Rust DB Core는 내보내기 방법 자동 선택을 지원하지 않습니다. 앱과 함께 배포된 코어를 사용하세요."
        )
        assert exporter.export_tables("app", ["t"], str(tmp_path / "part"), include_fk_parents=False,
                                      mysql_snapshot_mode="auto")[0] is False
        assert not facade.ran

    @pytest.mark.parametrize("result, threads, expected", [
        (
            {"mysql_snapshot_mode": "parallel_strict", "worker_count": 8, "snapshot_fallbacks": []},
            8, "같은 시점으로 내보냄 · 동시 작업 8개",
        ),
        (
            {"mysql_snapshot_mode": "parallel_table_lock", "worker_count": 8,
             "snapshot_fallbacks": [{"mode": "parallel_strict", "reason": "privilege"}]},
            8, "같은 시점으로 내보냄 · 동시 작업 8개 (잠금 권한이 없어 테이블만 잠깐 잠금)",
        ),
        (
            {"mysql_snapshot_mode": "parallel_table_lock", "worker_count": 8,
             "snapshot_fallbacks": [{"mode": "parallel_strict", "reason": "lock_timeout"}]},
            8, "같은 시점으로 내보냄 · 동시 작업 8개 (잠금을 제때 얻지 못해 테이블만 잠깐 잠금)",
        ),
        (
            {"mysql_snapshot_mode": "single_connection", "worker_count": 1,
             "snapshot_fallbacks": [{"mode": "parallel_strict", "reason": "privilege"},
                                    {"mode": "parallel_table_lock", "reason": "connection_limit"}]},
            8, "같은 시점으로 내보냄 · 동시 작업 1개 (동시 연결 수 제한 때문에 연결 하나로 읽음)",
        ),
        (
            {"mysql_snapshot_mode": "parallel_strict", "worker_count": 3, "snapshot_fallbacks": []},
            8, "같은 시점으로 내보냄 · 동시 작업 3개 (계정의 동시 연결 수 제한으로 줄임)",
        ),
        (
            {"mysql_snapshot_mode": "parallel_unsynchronized", "worker_count": 8,
             "snapshot_fallbacks": [{"mode": "parallel_strict", "reason": "privilege"},
                                    {"mode": "parallel_table_lock", "reason": "privilege"}]},
            8, "테이블마다 시점이 다를 수 있음 · 동시 작업 8개 (잠금 권한이 없어 시점을 맞추지 않고 나눠 읽음)",
        ),
        ({"snapshot_policy": "mysql_shared_consistent_snapshot"}, 4, "같은 시점으로 내보냄 · 동시 작업 4개"),
        ({"snapshot_policy": "postgresql_repeatable_read_snapshot", "worker_count": 1}, 8, ""),
    ])
    def test_export_message_names_the_method_actually_used(self, tmp_path, result, threads, expected):
        from src.exporters.rust_dump_exporter import (
            MYSQL_SNAPSHOT_AUTO_CAPABILITY, RustDumpConfig, RustDumpExporter, describe_snapshot_method,
        )

        assert describe_snapshot_method(result, threads) == expected
        facade = MagicMock()
        facade.hello.return_value = {"capabilities": [MYSQL_SNAPSHOT_AUTO_CAPABILITY]}
        facade.run_dump.return_value = {"success": True, "tables": 2, "rows_dumped": 5, **result}
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

        _, message = exporter.export_full_schema("app", str(tmp_path / "dump"), threads=threads, mysql_snapshot_mode="auto")

        assert message == "Rust DB Core export 완료: 2개 테이블, 5 rows" + (f" · {expected}" if expected else "")

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
        import src.exporters.rust_dump_exporter as exporter_module
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        assert not hasattr(exporter_module, "MySQLConnector")

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

    def test_fetch_fk_parents_reads_one_inspect_and_keeps_only_parents_of_this_schema(self):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        facade = MagicMock()
        facade.inspect_schema.return_value = {"tables": [
            {"name": "users", "foreign_keys": [{"name": "fk_self", "referenced_table": "users"}]},
            {"name": "orders", "foreign_keys": [{"name": "fk_orders_users", "referenced_table": "users"},
                                                {"name": "fk_orders_other_db", "referenced_table": "elsewhere"}]},
            {"name": "order_items", "foreign_keys": [{"name": "fk_items_orders", "referenced_table": "orders"}]},
        ]}
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

        deps = exporter.fetch_fk_parents("app")

        assert deps == {"orders": [("fk_orders_users", "users")], "order_items": [("fk_items_orders", "orders")]}
        facade.inspect_schema.assert_called_once()
        assert facade.inspect_schema.call_args.args[0].database == "app"

    def test_fk_parent_closure_is_transitive_and_names_what_it_added(self):
        from src.exporters.rust_dump_exporter import fk_parent_closure, fk_parents_outside

        deps = {"orders": [("fk_orders_users", "users")], "order_items": [("fk_items_orders", "orders")]}

        assert fk_parent_closure(["order_items"], deps) == (["order_items", "orders", "users"], ["orders", "users"])
        assert fk_parent_closure(["products", "users"], deps) == (["products", "users"], [])
        assert fk_parents_outside(["order_items", "users"], deps) == [
            {"child": "order_items", "constraint": "fk_items_orders", "parent": "orders"}]
        assert fk_parents_outside(["order_items", "orders", "users"], deps) == []

    def test_export_tables_sends_exactly_the_given_tables_and_records_the_dialogs_fk_choice(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 2, "rows_dumped": 0}

            def inspect_schema(self, endpoint):
                raise AssertionError("the dialog already resolved the FK parents")

        facade = FakeFacade()
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)
        outside = [{"child": "orders", "constraint": "fk_orders_users", "parent": "users"}]

        success, _, tables = exporter.export_tables(
            "app", ["orders", "customers"], str(tmp_path), auto_added_tables=["customers", "not_exported"],
            fk_parents_outside_dump=outside,
        )

        assert success and tables == ["orders", "customers"] == facade.payload["tables"]
        metadata = json.loads((tmp_path / "_export_metadata.json").read_text(encoding="utf-8"))
        assert metadata["added_fk_tables"] == ["customers"], "only parents that are in the dump"
        assert metadata["fk_parents_outside_dump"] == outside

    def test_scheduled_table_backup_adds_fk_parents_and_leaves_none_outside(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        facade = MagicMock()
        facade.inspect_schema.return_value = {"tables": [
            {"name": "users", "foreign_keys": []},
            {"name": "orders", "foreign_keys": [{"name": "fk_orders_users", "referenced_table": "users"}]},
        ]}
        facade.run_dump.return_value = {"success": True, "tables": 2, "rows_dumped": 0}
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

        assert exporter.export_tables("app", ["orders"], str(tmp_path), include_fk_parents=True)[2] == ["orders", "users"]

        metadata = json.loads((tmp_path / "_export_metadata.json").read_text(encoding="utf-8"))
        assert (metadata["added_fk_tables"], metadata["fk_parents_outside_dump"]) == (["users"], [])

    def test_fk_parents_are_never_added_by_default(self):
        import inspect

        from src.exporters import rust_dump_exporter

        for function in (rust_dump_exporter.RustDumpExporter.export_tables, rust_dump_exporter.export_tables):
            assert inspect.signature(function).parameters["include_fk_parents"].default is False

    def test_last_run_names_the_method_and_the_objects_left_out(self, tmp_path):
        from src.exporters.rust_dump_exporter import MYSQL_SNAPSHOT_AUTO_CAPABILITY, RustDumpConfig, RustDumpExporter

        facade = MagicMock()
        facade.hello.return_value = {"capabilities": [MYSQL_SNAPSHOT_AUTO_CAPABILITY]}
        facade.run_dump.return_value = {
            "success": True, "tables": 1, "rows_dumped": 1, "mysql_snapshot_mode": "parallel_table_lock",
            "worker_count": 8, "snapshot_fallbacks": [{"mode": "parallel_strict", "reason": "privilege"}],
            "manifest_warnings": ["Object not exported: trigger:t:trg", "Workers used independent snapshots"],
        }
        exporter = RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

        assert exporter.export_full_schema("app", str(tmp_path), threads=8, mysql_snapshot_mode="auto")[0]

        assert exporter.last_run == {
            "mode": "parallel_table_lock",
            "method": "같은 시점으로 내보냄 · 동시 작업 8개 (잠금 권한이 없어 테이블만 잠깐 잠금)",
            "omitted": ["Object not exported: trigger:t:trg"],
        }
        facade.run_dump.side_effect = RuntimeError("boom")
        assert exporter.export_full_schema("app", str(tmp_path / "again"), mysql_snapshot_mode="auto")[0] is False
        assert exporter.last_run is None, "a failed export leaves no method behind"

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
        assert facade.payload["source"]["database"] == "postgres"
        assert facade.payload["source"]["schema"] == "public"

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
        import_mode="recreate",
        engine="postgresql",
    )

    assert success is True
    assert message == "ok"
    assert results == {}
    assert captured["config"].engine == "postgresql"
    assert captured["target_schema"] == "analytics"
    assert captured["import_mode"] == "recreate"


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

        success, _msg, _results = importer.import_dump(str(dump_dir), target_schema='public', import_mode='replace')

        assert success is True
        assert facade.payload["target"]["engine"] == "postgresql"
        assert facade.payload["target"]["database"] == "postgres"
        assert facade.payload["target"]["schema"] == "public"

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

    @staticmethod
    def _preview_facade(capabilities):
        class FakeFacade:
            def __init__(self):
                self.commands = []

            def hello(self):
                self.commands.append("service.hello")
                return {"capabilities": capabilities}

            def import_dump(self, payload, on_event=None):
                self.commands.append("dump.import")
                self.import_payload = payload
                return {"success": True, "tables": 1, "rows_imported": 1}

            def preview_import(self, payload):
                self.commands.append("dump.import_preview")
                self.preview_payload = payload
                return {"dry_run": True, "replace": ["users"], "blockers": []}

        return FakeFacade()

    def _write_dump(self, tmp_path):
        dump_dir = tmp_path / 'dump'
        dump_dir.mkdir()
        (dump_dir / '_tunnelforge_dump.json').write_text(
            '{"format":"tunnelforge-dump","format_version":3,"database":"app","source_engine":"mysql",'
            '"tables":[{"name":"users","path":"0001_users","rows":0,"chunks":0}]}',
            encoding='utf-8',
        )
        return str(dump_dir)

    @pytest.mark.parametrize("import_mode", ["replace", "safe"])
    def test_preview_payload_is_import_payload_without_mode(self, tmp_path, import_mode):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = self._write_dump(tmp_path)
        facade = self._preview_facade(["dump.import", "dump.import_preview"])
        importer = RustDumpImporter(RustDumpConfig('localhost', 3306, 'root', 'password'), facade=facade)
        options = dict(target_schema="other", threads=4, timezone_sql="SET SESSION time_zone = '+09:00'",
                       use_source_timezone=False)

        ok, message, preview = importer.preview_import(dump_dir, **options)
        importer.import_dump(dump_dir, import_mode=import_mode, **options)

        assert ok is True and message == ""
        assert preview == {"dry_run": True, "replace": ["users"], "blockers": []}
        assert "mode" not in facade.preview_payload
        assert facade.preview_payload == {key: value for key, value in facade.import_payload.items() if key != "mode"}
        assert facade.preview_payload["target"]["database"] == "other"

    def test_preview_refused_without_core_capability(self, tmp_path):
        """A core built before the preview would not know the command; nothing is sent to it."""
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        facade = self._preview_facade(["dump.run", "dump.import"])
        importer = RustDumpImporter(RustDumpConfig('localhost', 3306, 'root', 'password'), facade=facade)

        ok, message, preview = importer.preview_import(self._write_dump(tmp_path))

        assert ok is False and preview == {}
        assert "미리보기를 지원하지 않습니다" in message
        assert facade.commands == ["service.hello"]

    def test_preview_failure_is_returned_not_raised(self, tmp_path):
        from src.core.db_core_service import DbCoreServiceError
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        facade = self._preview_facade(["dump.import_preview"])

        def refuse(payload):
            raise DbCoreServiceError("connection refused", error_code="connection_failed")

        facade.preview_import = refuse
        importer = RustDumpImporter(RustDumpConfig('localhost', 3306, 'root', 'password'), facade=facade)

        ok, message, preview = importer.preview_import(self._write_dump(tmp_path))

        assert ok is False and preview == {}
        assert "connection refused" in message

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
        assert results["products"]["status"] == "blocked"
        assert "connection lost during import" in results["orders"]["message"]
        assert "not started" in results["products"]["message"]
        assert "connection lost during import" in msg

        error_events = {(table, status) for table, status, _ in status_events if status == "error"}
        assert ("orders", "error") in error_events
        assert ("products", "error") not in error_events

    def test_import_dump_keeps_tables_pending_for_operation_preflight_error(self, tmp_path):
        """대상 전체 사전검사 실패를 덤프의 모든 테이블 실패로 부풀리지 않는다."""
        from src.core.db_core_service import DbCoreServiceError
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        dump_dir.mkdir(parents=True)
        (dump_dir / '_tunnelforge_dump.json').write_text(
            json.dumps(
                {
                    "format": "tunnelforge-dump",
                    "format_version": 2,
                    "database": "app",
                    "tables": [
                        {"name": "users", "path": "0001_users", "rows": 1, "chunks": 1},
                        {"name": "orders", "path": "0002_orders", "rows": 1, "chunks": 1},
                    ],
                }
            ),
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                raise DbCoreServiceError(
                    "preflight_surviving_fk: target-only foreign key is incompatible"
                )

        importer = RustDumpImporter(
            RustDumpConfig('localhost', 3306, 'root', 'password'),
            facade=FakeFacade(),
        )
        status_events = []

        success, message, results = importer.import_dump(
            str(dump_dir),
            table_status_callback=lambda *event: status_events.append(event),
        )

        assert success is False
        assert results["users"]["status"] == "pending"
        assert results["orders"]["status"] == "pending"
        assert not [event for event in status_events if event[1] == "error"]
        assert "preflight_surviving_fk" in message

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

        assert success is False
        assert "View 2개" in msg
        assert "broken_view" in msg
        assert "생성 실패" in msg

    @pytest.mark.parametrize(("engine", "mode", "created", "expected"), [
        ("mysql", "replace", True, "새 DB app을 만들어 넣었습니다"),
        # Without a configured database the core imports into `postgres`; the message says so.
        ("postgresql", "replace", True, "새 스키마 postgres.app을 만들어 넣었습니다"),
        ("mysql", "replace", False, None),
        # A safe restore always creates its candidate; that is not a new target namespace.
        ("mysql", "safe", True, None),
    ])
    def test_import_dump_names_a_target_namespace_the_core_created(self, tmp_path, engine, mode, created, expected):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        (dump_dir / '0001_users').mkdir(parents=True)
        (dump_dir / '_tunnelforge_dump.json').write_text(
            json.dumps({"format": "tunnelforge-dump", "format_version": 3, "source_engine": engine,
                        "database": "app", "source_schema": "app",
                        "tables": [{"name": "users", "path": "0001_users", "rows": 0, "chunks": 0}]}),
            encoding='utf-8',
        )

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                result = {"success": True, "tables": 1, "rows_imported": 0, "namespace_created": created}
                if mode == "safe":
                    result.update(status="ready_for_switch", verified=True, original_unchanged=True,
                                  cutover_pending=True, namespace_existed=True,
                                  candidate_target={"engine": engine, "database": "app_restore"})
                return result

        config = RustDumpConfig('localhost', 3306, 'root', 'password', engine=engine)
        success, msg, _results = RustDumpImporter(config, facade=FakeFacade()).import_dump(str(dump_dir), import_mode=mode)

        assert success is True
        if expected:
            assert msg.endswith("\n" + expected)
        else:
            assert "만들어 넣었습니다" not in msg

    def test_verify_in_copy_stop_shows_each_coded_reason(self, tmp_path):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        dump_dir = tmp_path / 'dump'
        dump_dir.mkdir()
        (dump_dir / '_tunnelforge_dump.json').write_text(json.dumps({
            "format": "tunnelforge-dump", "format_version": 5, "source_engine": "mysql", "database": "app",
            "tables": [{"name": "users", "path": "0001_users", "rows": 0, "chunks": 0}]}), encoding='utf-8')
        payloads = []

        class FakeFacade:
            def import_dump(self, payload, on_event=None):
                payloads.append(payload)
                return {"success": False, "status": "failed_original_untouched", "original_unchanged": True,
                        "candidate_created": False, "error_code": "copy_refused",
                        "error": "copy_refused: verify-in-copy stopped before making a copy: notes.fk references users",
                        "message": "Safe restore failed; original database was not changed. copy_refused: verify-in-copy stopped",
                        "copy_blockers": [
                            {"code": "linked_target_only", "table": "notes", "detail": "notes.fk references users"},
                            {"code": "linked_target_only", "table": "notes_view", "detail": "view notes_view reads users"},
                            {"code": "metadata_visibility_unproven", "table": None, "detail": "grants"},
                            {"code": "something_new", "table": None, "detail": "later core"},
                        ]}

        importer = RustDumpImporter(RustDumpConfig('localhost', 3306, 'root', 'password'), facade=FakeFacade())
        success, message, _results = importer.import_dump(str(dump_dir), import_mode='safe')
        importer.import_dump(str(dump_dir), import_mode='safe', verify_only=True)

        assert success is False
        lines = message.splitlines()
        assert lines[0] == "복사본을 만들기 전에 멈췄습니다. 원본은 그대로입니다."
        assert lines[1].startswith("- 덤프에 없는 테이블이나 뷰가") and lines[1].endswith(" (notes, notes_view)")
        assert "SELECT, SHOW VIEW, PROCESS" in lines[2]
        # A code this version does not know: no code in the sentence, the report has the detail.
        assert lines[3] == "- 복사본에서 검증할 수 없는 다른 이유가 있습니다. Import 보고서에서 확인하세요."
        assert "something_new" not in message and "verify-in-copy" not in message
        assert "verify_only" not in payloads[0] and payloads[1]["verify_only"] is True

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


class TestExportRefusalContract:
    def _exporter(self, facade):
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpExporter

        return RustDumpExporter(RustDumpConfig("localhost", 3306, "root", "password"), facade=facade)

    def test_refusal_event_is_captured_by_error_code_not_message(self, tmp_path):
        from src.core.db_core_client import DbCoreServiceError

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                on_event({
                    "event": "error", "error_code": "unsupported_objects",
                    "message": "unrelated wording", "objects": ["trigger:t:trg", "routine:fn"],
                    "bypassable": True,
                })
                raise DbCoreServiceError("unrelated wording")

        facade = FakeFacade()
        exporter = self._exporter(facade)
        success, _ = exporter.export_full_schema("app", str(tmp_path / "dump"))

        assert success is False
        assert "allow_incomplete" not in facade.payload
        assert exporter.last_refusal == {"objects": ["trigger:t:trg", "routine:fn"], "bypassable": True}

    def test_other_errors_do_not_look_like_refusals(self, tmp_path):
        from src.core.db_core_client import DbCoreServiceError

        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                on_event({"event": "error", "message": "trigger:x mentioned in text"})
                raise DbCoreServiceError("boom")

        exporter = self._exporter(FakeFacade())
        exporter.export_tables("app", ["t"], str(tmp_path / "dump"), include_fk_parents=False)

        assert exporter.last_refusal is None

    def test_incomplete_choice_reaches_core_payload_and_resets_refusal(self, tmp_path):
        class FakeFacade:
            def run_dump(self, payload, on_event=None):
                self.payload = payload
                return {"success": True, "tables": 1, "rows_dumped": 1}

        facade = FakeFacade()
        exporter = self._exporter(facade)
        exporter.last_refusal = {"objects": ["x"], "bypassable": True}
        success, _, _ = exporter.export_tables(
            "app", ["t"], str(tmp_path / "dump"), include_fk_parents=False, allow_incomplete=True
        )

        assert success is True
        assert facade.payload["allow_incomplete"] is True
        assert exporter.last_refusal is None



_HANGUL = range(ord("가"), ord("힣") + 1)


_LOAD_FAILED = "이 테이블을 지우고 다시 만들거나 데이터를 넣는 중에 멈췄습니다."
_REPORT_NOT_SAVED = "Import 보고서를 저장하지 못했습니다. 보고서 내용은 최신이 아닙니다."


@pytest.mark.parametrize(("message", "expected"), [
    ("Rust DB Core import 오류: load_failed: orders: create_table failed: MySqlError { ERROR 1822 (HY000): x }",
     f"orders: {_LOAD_FAILED}"),
    # A failed row load is coded like a failed CREATE.
    ("Rust DB Core import 오류: load_failed: orders: load_rows failed: mysql LOAD DATA error: "
     "MySqlError { ERROR 1406 (22001): Data too long for column 'note' at row 1 }", f"orders: {_LOAD_FAILED}"),
    ("target_create_failed: the target database app was not found (or is not visible to this account) and could not "
     "be created; no target tables were changed: MySqlError { ERROR 1044 (42000): Access denied }",
     "대상 DB(스키마)가 없거나 이 계정에 보이지 않아 새로 만들려 했지만 오류로 멈췄습니다. 대상 테이블은 바뀌지 않았습니다."),
    # Every code the core joins is worded, and an unsaved report is said as well.
    ("Rust DB Core import 오류: ddl_preflight_failed: orders: server rejected create_table before target replacement: "
     "MySqlError { ERROR 1118 (42000): Row size too large }; additionally: ddl_probe_cleanup_failed: owned probes "
     "tf_p_0 could not be removed; original target tables were not changed: MySqlError { ERROR 1142 (42000): x }",
     "orders: 대상에 테이블을 만들 수 없어 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다. "
     "확인용으로 잠시 만든 테이블을 지우지 못했습니다. 원래 테이블은 바뀌지 않았습니다."),
    ("Rust DB Core import 오류: load_failed: orders: load_rows failed: x; failure report could not be persisted: "
     "disk full", f"orders: {_LOAD_FAILED} {_REPORT_NOT_SAVED}"),
    ("Rust DB Core import 오류: Import data was applied, but its completion report could not be persisted: disk full",
     None),
    # A safe restore's message puts the core reason after its own sentence.
    ("Safe restore failed; original database was not changed. cross_engine: safe restore currently verifies "
     "same-engine dumps only", "다른 DB 종류에서 만든 덤프는 복사본에서 검증할 수 없습니다."),
    ("post_load_validation_failed: postgresql SQL execution error: db error; code=23503; message=x",
     "데이터를 넣은 뒤 행 수를 확인하거나 인덱스·외래 키를 만들다가 멈췄습니다."),
    ("mysql connection error: TLS required (error_code=tls_unavailable)",
     "DB 서버가 TLS를 제공하지 않습니다. 서버의 TLS 설정을 확인하세요."),
    # A code is taken only where the core puts one, never from inside a path, an identifier or server text.
    ("secret_table: load_failed: x", None),
    (r"Import 오류: [Errno 2] No such file: C:\x\load_failed: y", None),
    ("Rust DB Core import 오류: mysql error: column note: ddl_preflight_failed: x", None),
    ("secret_table: something failed", None),
    ("connection refused", None),
    ("", None),
])
def test_core_error_codes_map_to_korean(message, expected):
    from src.core import i18n
    from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES, describe_core_code, describe_core_failure

    assert describe_core_failure(message) == expected
    # A preview blocker names its table; an unknown code has no sentence.
    assert describe_core_code("dependent_view", "orders") == "orders: 대상의 뷰가 이 테이블을 쓰고 있어 교체할 수 없습니다."
    assert describe_core_code("SPATIAL_UNSUPPORTED") == CORE_CODE_SENTENCES["spatial_unsupported"]
    assert describe_core_code("not_a_core_code", "orders") is None

    for code, sentence in CORE_CODE_SENTENCES.items():
        assert any(ord(char) in _HANGUL for char in sentence), code
        assert not any(word in sentence.lower() for word in ("preflight", "manifest", "계약", "safe", "replace")), code
    i18n.set_language("en")
    for code in CORE_CODE_SENTENCES:
        assert not any(ord(char) in _HANGUL for char in describe_core_code(code)), code


def test_every_code_the_core_can_emit_has_a_korean_sentence():
    import re
    from pathlib import Path
    from src.exporters.rust_dump_exporter import CORE_CODE_SENTENCES

    emitted = set()
    for path in (Path(__file__).resolve().parents[1] / "migration_core" / "src").rglob("*.rs"):
        if path.name in ("query_job.rs", "explain.rs"):  # the SQL editor words its own codes
            continue
        source = path.read_text(encoding="utf-8").split("#[cfg(test)]")[0]
        # A string that starts with `code: `, as format!("code: ...") writes it.
        emitted.update(re.findall(r'"([a-z]+(?:_[a-z]+)+): ', source))
        emitted.update(re.findall(r'classified_import_error\(\s*"([a-z_]+)"', source))
        if path.name == "import_preview.rs":  # upgrade.analyze has an issue() of its own
            emitted.update(re.findall(r'\b(?:issue|warning)\(\s*"([a-z_]+)"', source))
        # The (code, ...) tuples that feed the preview's blockers and warnings.
        emitted.update(re.findall(r'push\(\(\s*"([a-z_]+)"', source))
        emitted.update(re.findall(r'"error_code"\s*:\s*"([a-z_]+)"', source))
        if path.name == "safe_restore.rs":  # the (code, object, detail) refusals behind copy_blockers
            emitted.update(re.findall(r'\(\s*"([a-z_]+)",\s*(?:None|Some\()', source))
        for body in re.findall(r"fn manifest_warning_code\b.*?\n\}", source, re.DOTALL):
            emitted.update(re.findall(r'"([a-z_]+)"', body))
    assert {"load_failed", "dependent_object", "views_outside_snapshot", "partial_table_filter", "no_tables",
            "safe_promotion", "safe_restore_content_mismatch"} <= emitted
    assert {"copy_refused", "copy_checks_unavailable", "target_events", "linked_target_only", "previous_copy_leftover"} <= emitted
    assert sorted(emitted - set(CORE_CODE_SENTENCES)) == []

@pytest.mark.parametrize("code, expected", [
    ("trigger:orders:trg_audit", "테이블 orders: 트리거 trg_audit는 덤프에 담을 수 없습니다"),
    ("non_transactional_table:logs:MEMORY", "테이블 logs: MEMORY 엔진 테이블이라 같은 시점으로 담을 수 없습니다"),
    ("generated_column:orders:total", "테이블 orders: 계산 열 total의 정의를 덤프에 담을 수 없습니다"),
    ("cross_schema_fk:orders:fk_other", "테이블 orders: 외래 키 fk_other가 다른 DB의 테이블을 가리켜 덤프에 담을 수 없습니다"),
    ("routine:fn_total", "저장 프로시저·함수 fn_total: 덤프에 담을 수 없습니다"),
    ("event:ev_nightly", "예약 이벤트 ev_nightly: 덤프에 담을 수 없습니다"),
    # An unknown code still names its table; the code itself stays behind "자세히".
    ("future_kind:orders:x", "테이블 orders: 이 객체는 담을 수 없습니다"),
    ("future_kind", "future_kind: 이 객체는 담을 수 없습니다"),
])
def test_refusal_text_names_table_and_reason(code, expected):
    from src.exporters.rust_dump_exporter import describe_export_refusal

    assert describe_export_refusal([code]) == [expected]


def test_every_refusal_kind_the_core_reports_has_a_sentence():
    import re
    from pathlib import Path

    from src.core import i18n
    from src.exporters.rust_dump_exporter import describe_export_refusal

    source = (Path(__file__).resolve().parents[1] / "migration_core" / "src" / "dump.rs").read_text(encoding="utf-8")
    lossy = re.search(r"LOSSY_SCHEMA_KINDS: \[&str; \d+\] = \[([^\]]*)\]", source).group(1)
    omitted = re.search(r"fn scoped_omitted_objects\b.*?\n\}", source, re.DOTALL).group(0)
    kinds = set(re.findall(r'"([a-z_]+)"', lossy)) | set(re.findall(r'"([a-z_]+)"', omitted))
    assert {"generated_column", "cross_schema_fk", "trigger", "non_transactional_table", "routine", "event"} <= kinds

    for kind in sorted(kinds):
        line = describe_export_refusal([f"{kind}:orders:x"])[0]
        assert "이 객체는 담을 수 없습니다" not in line, kind
        assert "x" in line and ":" in line, kind
    i18n.set_language("en")
    try:
        lines = describe_export_refusal([f"{kind}:orders:x" for kind in sorted(kinds)] + ["future_kind:orders:x"])
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
    assert [line for line in lines if any(ord(char) in _HANGUL for char in line)] == []
