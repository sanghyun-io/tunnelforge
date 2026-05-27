"""
RustDumpExporter 테스트
"""
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

    def test_get_uri(self):
        """URI 생성 테스트"""
        from src.exporters.rust_dump_exporter import RustDumpConfig

        config = RustDumpConfig(
            host='localhost',
            port=3306,
            user='root',
            password='secret123'
        )

        uri = config.get_uri()

        assert uri == 'root:secret123@localhost:3306'

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


class TestForeignKeyResolver:
    """ForeignKeyResolver 클래스 테스트"""

    @pytest.fixture
    def mock_connector(self):
        """MySQLConnector Mock"""
        connector = MagicMock()
        return connector

    def test_resolve_required_tables_no_deps(self, mock_connector):
        """FK 의존성 없는 경우"""
        from src.exporters.rust_dump_exporter import ForeignKeyResolver

        # 빈 FK 정보 반환
        mock_connector.execute.return_value = []

        resolver = ForeignKeyResolver(mock_connector)
        selected = ['users', 'products']
        required, added = resolver.resolve_required_tables(selected, 'mydb')

        assert 'users' in required
        assert 'products' in required
        assert len(added) == 0

    def test_resolve_required_tables_with_deps(self, mock_connector):
        """FK 의존성 있는 경우"""
        from src.exporters.rust_dump_exporter import ForeignKeyResolver

        # FK 정보 반환: orders -> users, order_items -> orders
        mock_connector.execute.return_value = [
            {'TABLE_NAME': 'orders', 'REFERENCED_TABLE_NAME': 'users'},
            {'TABLE_NAME': 'order_items', 'REFERENCED_TABLE_NAME': 'orders'}
        ]

        resolver = ForeignKeyResolver(mock_connector)
        # order_items만 선택하면 orders와 users도 추가되어야 함
        selected = ['order_items']
        required, added = resolver.resolve_required_tables(selected, 'mydb')

        assert 'order_items' in required
        assert 'orders' in required or 'orders' in added
        assert 'users' in required or 'users' in added

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
        assert exporter._connector is None

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
        assert results["users"]["status"] == "done"
        assert "1" in msg

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

    def test_import_phase_explains_local_infile_fallback_as_non_error(self):
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

        assert messages == [
            "MySQL local_infile 비활성화: 안전 INSERT fallback으로 진행합니다. "
            "에러는 아니지만 LOAD DATA LOCAL보다 느립니다."
        ]

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
