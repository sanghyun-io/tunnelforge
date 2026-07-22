# TunnelForge Clean Code 리팩토링 마스터 플랜 (2026-07-09)

> **입력**: `.claude/investigation-clean-code-audit-2026-07-09.md`의 검증된 255건 (HIGH 40 / MEDIUM 134 / LOW 81).
> **설계**: Workflow 29개 WP 초안 병렬 작성 → 적대적 리뷰어(Opus) 검증 → 리뷰 지적 4건 반영.
> **성격**: 순수 **동작 보존(behavior-preserving) 리팩토링**. 정합성 버그는 이전 감사(2026-07-08)에서 이미 전량 해결됨 — 본 플랜은 가독성/유지보수성만 다룬다.
> **회귀 기준선(main, 2026-07-09)**: `pytest` **1810 passed / 0 failed**, `cargo test` 전부 ok. (단 worktree에서는 `test_macos_validation_artifact_download_script_uses_local_head_after_pr_merge` 1건이 GitHub CI 의존으로 항상 실패 — 회귀 판정에서 제외.)

---

## Cross-cutting Directives (모든 WP 구속)

1. **실행 모델**
   - 각 WP는 main 기반 독립 git worktree에서 수행. 같은 라운드 내 WP는 병렬 실행.
   - WP 리스트 밖의 source/test 파일을 건드려야 하면 **즉시 중단하고 재스케줄 요청**. "그냥 손대기" 금지.
   - 어떤 PR에서도 버전 bump 금지(`version:*` 라벨/`src/version.py` 무접촉).
   - Python DB 드라이버 핫패스 재도입 금지 — DB 연산 소유권은 `tunnelforge-core`.

2. **동작 보존 원칙**
   - 기능 변경 0. 갓파일/갓클래스 분할은 코드 이동 + 재수출(re-export)로 기존 import 경로를 100% 유지.
   - 새 모듈로 심볼을 옮긴 뒤, 원본 파일은 `from ... import *` 스타일 재수출 + `__all__` 선언만 남긴다(로직 0줄).
   - 시그니처를 넓힐 때는 레거시 위치 인자 시그니처를 유지(예: keyword-only 파라미터 추가)해 콜사이트 무수정.

3. **라운드별 회귀 게이트**
   - 공통: `python -m pytest` (macOS validation 1건 실패는 무시), touched Python 파일 전부 `python -m py_compile`.
   - Rust-touching WP 추가: `cargo test --manifest-path migration_core/Cargo.toml`, `cargo build --manifest-path migration_core/Cargo.toml --release`.

4. **`__init__.py` 재수출 규율**
   - 새 core 모듈 신설 시 `src/core/__init__.py`(또는 해당 패키지 `__init__`)의 재수출 표면은 **원본 파일이 재수출을 유지하므로 수정 불필요**를 기본 전제로 한다. `__init__` 수정이 불가피하면 리스크에 명시하고 라운드 내 단독 소유를 확인.

5. **Rust 분할 특칙 (WP-1.8)**
   - `migration_core/src/lib.rs`(17,006줄)는 검증된 클러스터 경계로 11개 모듈 파일로 분리, `lib.rs`는 `pub mod` 재수출 루트로 축소.
   - `#[cfg(test)] mod tests`의 각 `#[test]`는 **대상 함수를 소유한 모듈에 공치**. 테스트가 private 함수를 직접 호출하므로 통합테스트 크레이트로 추출 금지.
   - 모듈 하나 추출할 때마다 `cargo build`/`cargo test` 검증. 로직 변경 절대 금지(move + re-export only).

---

## Round 1 — 공유 모듈 추출 + 갓파일 기계적 분할 (Enabler)

### WP-1.1 — core-service-split
**Branch:** `refactor/cc-r1-db-core-service-split` · **Size:** L · **발견:** 4건 (H1/M1/L2)

**Findings covered:** CC-001, CC-006, CC-007, CC-014

**수정 파일:** `src/core/db_core_service.py`, `tests/test_db_core_service.py`
**신규 파일:** `src/core/db_core_client.py`, `src/core/db_core_facade.py`, `src/core/db_core_dbapi_shim.py`
**테스트:** `tests/test_db_core_service.py`, `tests/test_connection_test_worker.py`, `tests/test_rust_dump_exporter.py`

**가이드:**
- [CC-001] src/core/db_core_client.py 신설: DbCoreServiceError, _format_error_event(L25-42), SUPPORTED_DB_ENGINES, parse_db_version_tuple(L48-67), normalize_db_engine(L70-79), default_database_for_engine(L82-85), DbCoreServiceClient(L110-253)를 그대로 이동. import 는 json/re/subprocess/threading/uuid/collections.deque/typing + cross_engine_migration(db_core_executable, parse_helper_event), logger, platform_integration(no_window_creation_flags)만 필요. 각 새 모듈은 get_logger("db_core_service")를 그대로 써서 로그 채널명 유지.
- [CC-001] src/core/db_core_facade.py 신설: DbEndpoint dataclass(L88-107), DbCoreFacade(L256-422), _shared_facade_lock/_shared_facade, get_shared_db_core_facade, shutdown_shared_db_core_facade(L424-447), atexit.register 호출을 이동. atexit 등록 코드는 이 모듈에만 남기고 db_core_service.py 재수출 파일에는 절대 남기지 않는다(이중 등록 방지). db_core_client 에서 DbCoreServiceClient/DbCoreServiceError 를 import.
- [CC-001] src/core/db_core_dbapi_shim.py 신설: RustDbConnector(L450-615), create_rust_db_connector(L617-638), RustDbConnection(L641-718), RustDbCursor(L721-777), quote_mysql_ident(L780-781)를 이동. SYSTEM_SCHEMAS(constants), statement_returns_rows(sql_query_classifier) import 는 이 모듈로 옮기고, db_core_client 의 normalize_db_engine/default_database_for_engine/parse_db_version_tuple/DbCoreServiceError, db_core_facade 의 DbEndpoint/DbCoreFacade/get_shared_db_core_facade/RustDbConnection 관련 의존을 import 한다.
- [CC-001] src/core/db_core_service.py 는 순수 재수출 모듈로 축소: 세 새 모듈에서 기존 공개/준공개 이름 전부(DbCoreServiceError, _format_error_event, SUPPORTED_DB_ENGINES, parse_db_version_tuple, normalize_db_engine, default_database_for_engine, DbEndpoint, DbCoreServiceClient, DbCoreFacade, get_shared_db_core_facade, shutdown_shared_db_core_facade, RustDbConnector, create_rust_db_connector, RustDbConnection, RustDbCursor, quote_mysql_ident)를 from-import 하고 __all__ 선언. 로직 0줄. 소비 파일 13개(scripts 4, src/core/db_connector.py, postgres_connector.py, scheduler.py, tunnel_monitor.py, src/exporters/rust_dump_exporter.py, src/ui/workers/test_worker.py, src/ui/dialogs/sql_editor_dialog.py, sql_editor_workers.py, test_dialogs.py, tests 3개 중 2개)는 절대 수정하지 않는다. 주의: tests/test_db_core_service.py:532-533 이 bind_sql_params/sql_literal 부재를 assert 하므로 재수출에 이 이름들을 추가하지 말 것.
- [CC-001] tests/test_db_core_service.py 의 monkeypatch 대상 3곳 이동 필수: L678/L689 의 monkeypatch.setattr(db_core_service, "get_shared_db_core_facade", ...) 과 L702 의 monkeypatch.setattr(db_core_service, "SYSTEM_SCHEMAS", ...) 은 분할 후 RustDbConnector 가 자기 모듈 네임스페이스에서 이름을 해석하므로 무효가 된다 → import src.core.db_core_dbapi_shim as db_core_dbapi_shim 하여 shim 모듈을 patch 하도록 수정. 그 외 from-import 기반 테스트는 재수출 덕에 무수정. 추가로 재수출 회귀 테스트 1개를 test_db_core_service.py 에 신설: 위 공개 이름 전부가 src.core.db_core_service 에서 import 가능함을 assert.
- [CC-007] create_rust_db_connector 가 resolved_engine/default_database_for_engine 으로 DbEndpoint 를 1회 생성해 RustDbConnector(endpoint=..., facade=facade) 로 전달하도록 변경. RustDbConnector.__init__ 는 keyword-only endpoint: Optional[DbEndpoint] 파라미터를 추가하되 레거시 7-param 위치 인자 시그니처를 그대로 유지(endpoint 지정 시 그대로 사용, 미지정 시 기존과 동일하게 database or ("postgres" if engine == "postgresql" else "") 로직으로 DbEndpoint 조립). 이렇게 하면 직접 생성 사이트(db_connector.py:115, test_worker.py:244/246 — 모두 위치 인자)와 create_rust_db_connector 호출 사이트 5곳을 전혀 건드리지 않고 이중 repackaging 을 제거한다. 시그니처 완전 이관(endpoint 전용)은 라운드 2/3 콜사이트 WP 의 몫으로 남긴다.
- [CC-014] RustDbCursor.execute(L736-753)의 hasattr(facade, "execute_on_connection_result") 분기와 last_rows_affected getattr fallback(else 절 L747-753)을 제거하고 execute_on_connection_result 를 무조건 호출. 사전 검증됨: 프로덕션 facade(DbCoreFacade)와 테스트 double(L327, L445, L460, L474) 전부 이 메서드를 정의하며, L349 double 은 executemany 전용이라 execute 경로를 타지 않음. 동시에 tests/test_db_core_service.py L326-345 의 test_rust_db_cursor_rowcount_uses_call_local_rows_affected 를 정리: 오해 소지 있는 last_rows_affected=999 클래스 속성과 이제 죽은 execute_on_connection 메서드를 FakeFacade 에서 제거하고 테스트명을 결과 API 무조건 호출을 반영하도록 갱신(예: test_rust_db_cursor_calls_execute_on_connection_result_unconditionally).
- [CC-006] LOW 일괄 정리(shim 모듈 내부): RustDbConnector.get_tables(현 L546-557)와 RustDbConnection.select_db(현 L707-716)의 7필드 수동 DbEndpoint 재구성을 dataclasses.replace 로 교체 — get_tables 는 new_database/new_schema 를 기존 조건 로직으로 먼저 계산한 뒤 replace(self.endpoint, database=..., schema=...), select_db 는 replace(self.endpoint, database=database). shim 모듈에 from dataclasses import replace 추가.
- 하드 제약: 순수 동작 보존 리팩토링(기능 변경 0), 기존 import 경로는 db_core_service.py 재수출로 전부 유지, 버전 bump 금지(version:* 라벨/버전 파일 무접촉), Python DB 드라이버 핫패스 재도입 금지(DB 연산 소유권은 tunnelforge-core 유지). files_touched/new_files 밖 파일 수정이 필요해지면 즉시 중단하고 재스케줄 요청 — 위 설계(재수출 + 레거시 시그니처 유지)대로면 그런 상황은 발생하지 않는다.
- tests/test_connection_test_worker.py 는 sys.modules["src.core.db_core_service"] 를 fake 모듈로 치환하는 패턴(L42/L74)이라 test_worker.py 의 지연 import 가 레거시 경로를 유지하는 한 무수정으로 통과한다 — 이 테스트 파일은 실행만 하고 수정하지 않는다.

**검증:**
- `python -m py_compile src/core/db_core_service.py src/core/db_core_client.py src/core/db_core_facade.py src/core/db_core_dbapi_shim.py`
- `python -m pytest tests/test_db_core_service.py tests/test_connection_test_worker.py tests/test_rust_dump_exporter.py -q`
- `python -m pytest`

**리스크:**
- import fan-out 이 큼: src.core.db_core_service 소비 파일이 13개(scripts 4 + src 6 + tests 3). 재수출이 하나라도 누락되면 광범위 ImportError — 재수출 회귀 테스트로 방어하되, 준공개 이름(_format_error_event 는 재수출 필요, tests 가 db_core_service 모듈 객체를 직접 참조)까지 포함해야 함.
- tests/test_db_core_service.py 의 module-object monkeypatch 3곳(L678, L689, L702)은 분할 후 조용히 무효화되어 '테스트는 통과하지만 아무것도 검증하지 않는' 상태가 될 수 있음 — patch 대상을 shim 모듈로 반드시 이동해야 하며, 이 파일 수정은 이 WP 의 필수 범위.
- CC-007 의 완전한 해결(endpoint 전용 시그니처)은 소비 파일 6개(db_connector.py, test_worker.py, scheduler.py, tunnel_monitor.py, sql_editor_workers.py, test_dialogs.py) 수정이 필요해 이 WP 범위를 벗어남 — 본 WP 는 keyword-only endpoint 추가 + 레거시 시그니처 유지로 한정하고, 콜사이트 마이그레이션은 해당 파일을 소유하는 라운드 2(core)/라운드 3(UI) WP 에 이월(라운드 간 겹침은 허용).
- db_connector.py, postgres_connector.py, scheduler.py, tunnel_monitor.py 및 UI dialogs/workers 파일들은 후속 라운드 WP 가 건드릴 가능성이 높음 — 이 WP 가 해당 파일들을 무수정으로 유지하는 것이 라운드 간 충돌 방지의 전제.
- atexit.register(shutdown_shared_db_core_facade) 위치 이동: db_core_facade.py 에만 두고 재수출 모듈에 남기면 이중 등록됨(shutdown 은 멱등이라 실해는 없지만 규율 위반) — 코드 리뷰 체크포인트.
- 로컬 전체 pytest 실행 시 macOS validation 관련 테스트는 GitHub CI 의존으로 항상 실패(기존 known-flaky) — 이 WP 의 회귀 판정에서 제외할 것.
- PyInstaller 패키징은 정적 from-import 기반이라 새 모듈 3개가 자동 수집될 것으로 예상되나, 릴리스 빌드에서 hidden-import 누락 가능성은 잔존(낮음).

### WP-1.2 — validator-split
**Branch:** `refactor/cc-r1-sql-validator-split` · **Size:** L · **발견:** 5건 (H1/M3/L1)

**Findings covered:** CC-002, CC-008, CC-009, CC-010, CC-016

**수정 파일:** `src/core/sql_validator.py`, `src/core/constants.py`, `tests/test_sql_validator.py`
**신규 파일:** `src/core/sql_identifier_utils.py`, `src/core/sql_metadata.py`, `src/core/sql_autocompleter.py`
**테스트:** `tests/test_sql_validator.py`, `tests/test_sql_editor_dialog.py`, `tests/test_db_core_service.py`, `tests/test_db_connector.py`

**가이드:**
- [CC-002] src/core/sql_identifier_utils.py 신설: ALIAS_STOP_WORDS, _normalize_identifier, _read_identifier, _skip_balanced_parentheses, extract_cte_names, extract_derived_table_aliases, extract_table_aliases 를 코드 변경 없이 그대로 이동. extract_table_aliases 의 'SchemaMetadata' 타입 힌트는 문자열 어노테이션 + `if TYPE_CHECKING: from src.core.sql_metadata import SchemaMetadata` 로 처리 (런타임 내부 import 0개 유지 — 실제 사용은 metadata.has_table/metadata.tables 덕타이핑뿐).
- [CC-002] src/core/sql_metadata.py 신설: _schema_key, SchemaMetadata, SchemaMetadataProvider (+ 신규 상수 FUZZY_MATCH_CUTOFF) 이동. 원 계획의 3파일 분할은 순환참조를 만든다 — sql_validator 가 SQLAutoCompleter 를 재수출해야 하는데 SQLAutoCompleter 는 SchemaMetadataProvider 가 필요하므로, 메타데이터 계층을 4번째 모듈로 분리해 의존 방향을 sql_identifier_utils/sql_metadata → sql_validator/sql_autocompleter 단방향으로 고정한다 (이 레포의 god-file-split 순환참조 사고 이력 있음).
- [CC-002] src/core/sql_autocompleter.py 신설: SQLAutoCompleter 클래스 이동. import 는 sql_metadata (SchemaMetadataProvider) 와 sql_identifier_utils (extract_table_aliases, extract_cte_names, extract_derived_table_aliases) 에서만 — 절대 sql_validator 를 import 하지 않는다.
- [CC-002] src/core/sql_validator.py 에는 IssueSeverity, ValidationIssue, SQLValidator 만 남긴다. 파일 상단에 하위호환 재수출 블록 추가: `from src.core.sql_identifier_utils import ALIAS_STOP_WORDS, _normalize_identifier, _read_identifier, _skip_balanced_parentheses, extract_cte_names, extract_derived_table_aliases, extract_table_aliases` / `from src.core.sql_metadata import _schema_key, FUZZY_MATCH_CUTOFF, SchemaMetadata, SchemaMetadataProvider` / `from src.core.sql_autocompleter import SQLAutoCompleter` (+ __all__ 정의). 기존 소비자 6곳(src/core/__init__.py, src/ui/dialogs/sql_editor_dialog.py, src/ui/dialogs/sql_editor_highlighters.py, src/ui/workers/validation_worker.py, tests/test_sql_validator.py, tests/test_sql_editor_dialog.py)은 old path 로 계속 동작하므로 절대 수정하지 않는다.
- [CC-008] src/core/constants.py 의 SYSTEM_SCHEMAS frozenset 에 'ndbinfo' 추가 (finding 권고에 따른 단일 소스화; tests/test_sql_validator.py:335 의 ndbinfo 제외 테스트가 이를 요구). SQLValidator 의 사설 SYSTEM_SCHEMAS 딕셔너리 리터럴(L408-414)은 삭제하고 파생 상수로 교체: `SYSTEM_SCHEMAS = frozenset(s.upper() for s in constants.SYSTEM_SCHEMAS)` — L515 의 `schema_name.upper() in self.SYSTEM_SCHEMAS` 비교 코드는 그대로 유지되고 드리프트가 원천 차단된다. db_connector.py/db_core_service.py 는 constants 를 import 만 하므로 수정 불필요.
- [CC-009] _validate_version_compatibility 의 두 스캔 루프(L606-620 키워드, L623-637 함수)를 SQLValidator 사설 메서드 `_flag_unsupported_items(sql, items, pattern_template, message_template, string_regions, line_offsets, major, minor)` 하나로 추출해 2회 호출. ValidationIssue 의 message 문자열, severity, end_column 계산(len(item))은 바이트 단위로 동일하게 유지 (테스트가 메시지 텍스트를 검증할 수 있음).
- [CC-010] SQLAutoCompleter.get_completions(727-809) 을 4개 사설 헬퍼로 분해: _complete_tables(metadata, prefix), _complete_columns_for_table(sql, metadata, target_table, prefix), _complete_columns_from_from_clause(sql, metadata, prefix), _complete_keywords_and_functions(context, prefix). public 시그니처(get_completions(sql, cursor_pos, schema=None))와 반환 리스트의 항목 순서/dict 형태({label,type,detail})는 완전 동일 유지 — 기존 테스트가 순서를 검증한다.
- [CC-016] sql_metadata.py 에 모듈 레벨 상수 `FUZZY_MATCH_CUTOFF = 0.5` 정의, get_similar_tables/get_similar_columns 두 호출부에서 cutoff=FUZZY_MATCH_CUTOFF 로 참조 (LOW 스윕 항목).
- tests/test_sql_validator.py 에 가드 테스트 추가: (1) old path 재수출 identity 확인 (`from src.core.sql_validator import SQLAutoCompleter, SchemaMetadata, extract_table_aliases` 가 새 모듈의 객체와 `is` 동일), (2) SQLValidator.SYSTEM_SCHEMAS 가 constants.SYSTEM_SCHEMAS 의 대문자 파생임을 확인, (3) 'NDBINFO' 포함 확인. 기존 테스트는 수정 없이 전부 통과해야 한다.
- 순환참조 검증 필수: `python -c "import src.core.sql_autocompleter"` 와 `python -c "import src.core.sql_validator"` 를 각각 단독 실행해 어느 모듈을 먼저 import 해도 ImportError 가 없는지 확인 (이 레포 MEMORY 의 god-file-split 함정 대응).
- 하드 제약: 동작 보존 리팩토링만 (기능 변경 금지 — 유일한 문서화된 예외는 CC-008 의 ndbinfo 중앙 상수 추가), 버전 bump 금지, Python DB 드라이버 hot path 재도입 금지 (이 WP 는 순수 파싱/검증 로직만 다루므로 DB 코드 접점 없음). files_touched/new_files 외 파일 수정이 필요해지면 즉시 중단하고 재스케줄 요청 — 소비자 6곳은 재수출로 무수정이 보장되므로 이 상황은 발생하지 않아야 한다.

**검증:**
- `python -m py_compile src/core/sql_validator.py src/core/sql_identifier_utils.py src/core/sql_metadata.py src/core/sql_autocompleter.py src/core/constants.py`
- `python -c "import src.core.sql_autocompleter" (순환참조 부재 — autocompleter 선행 import)`
- `python -c "import src.core.sql_validator; import src.core; from src.core.sql_validator import SQLValidator, SQLAutoCompleter, SchemaMetadataProvider, SchemaMetadata, ValidationIssue, IssueSeverity, extract_table_aliases" (재수출 표면 확인)`
- `python -m pytest tests/test_sql_validator.py -q`
- `python -m pytest tests/test_sql_editor_dialog.py tests/test_db_core_service.py tests/test_db_connector.py -q`
- `python -m pytest (전체 회귀 — macOS validation CI 의존 테스트의 로컬 상시 실패는 기존 베이스라인이므로 회귀로 판정하지 않음)`

**리스크:**
- CC-008 로 constants.SYSTEM_SCHEMAS 에 'ndbinfo' 를 추가하면 db_connector.py:187 / db_core_service.py:537 의 DB 목록 필터링도 ndbinfo 를 제외하게 됨 — NDB Cluster 환경에서만 관측되는 의도된 미세 동작 변화 (finding 권고 명시). tests/test_db_core_service.py:702 는 SYSTEM_SCHEMAS 를 monkeypatch 하므로 영향 없음 확인 완료.
- old import path 소비자 fan-out 6개 파일 (src/core/__init__.py, src/ui/dialogs/sql_editor_dialog.py L157/L2493, src/ui/dialogs/sql_editor_highlighters.py L152, src/ui/workers/validation_worker.py L80, tests/test_sql_validator.py, tests/test_sql_editor_dialog.py) — 재수출 유지로 전부 무수정. 재수출 목록에서 하나라도 빠지면 즉시 ImportError 이므로 위 verification 3번 명령으로 게이트.
- 같은 라운드 1 의 다른 WP 가 src/core/constants.py (공유 상수 승격 테마) 를 건드릴 가능성 있음 — 동일 라운드 파일 중복은 금지이므로 오케스트레이터가 라운드 1 WP 간 constants.py 소유권을 확인해야 함. 이 WP 의 constants.py 변경은 frozenset 에 한 줄 추가로 최소화됨.
- sql_editor_dialog.py / validation_worker.py / sql_editor_highlighters.py 는 라운드 3 UI WP 의 대상일 수 있음 — later-round overlap 이므로 허용 (이 WP 는 해당 파일을 건드리지 않음).
- tests/test_sql_editor_dialog.py 는 PyQt 테스트 — headless 환경에서 QApplication 관련 hang 이력 있음 (프로젝트 MEMORY). 파일 단위 실행 + 타임아웃 감시 권장.
- 이론상 3파일 분할(원 theme)과 달리 sql_metadata.py 4번째 모듈이 추가됨 — sql_validator ↔ sql_autocompleter 순환 import 를 구조적으로 제거하기 위한 의도된 설계 변경이며, theme 이 명명한 3개 파일은 모두 생성됨.

### WP-1.3 — manager-group-split
**Branch:** `refactor/cc-r1-config-manager-group-split` · **Size:** M · **발견:** 3건 (H1/M1/L1)

**Findings covered:** CC-000, CC-003, CC-017

**수정 파일:** `src/core/config_manager.py`
**신규 파일:** `src/core/group_manager.py`, `tests/test_group_manager.py`
**테스트:** `tests/test_config_manager.py`, `tests/test_group_manager.py (new)`

**가이드:**
- [CC-000] src/core/group_manager.py 신설, TunnelGroupManager 클래스를 만들고 생성자에서 ConfigManager 인스턴스를 주입받는다 (def __init__(self, config_manager)). config_manager.py L617-808의 7개 메서드(get_groups, add_group, update_group, delete_group, move_tunnel_to_group, get_tunnel_group, save_group_collapsed_state) 본문을 그대로 이동하되, 읽기는 self._config.load_config(), 쓰기는 self._config._mutate_config(...)를 통해 수행한다. 메시지 문자열/반환 튜플 형태는 1글자도 바꾸지 않는다.
- [CC-000] ConfigManager에는 동일 시그니처의 위임(facade) 메서드 7개를 남긴다 — 각각 lazy 프로퍼티 group_manager(기존 encryptor 프로퍼티 L582-587 패턴과 동일: self._group_manager is None이면 생성)로 위임하는 1줄 구현. 이렇게 해야 src/ui/main_window.py의 8개 호출부(L340/496/507/519/530/549/558/571)와 src/ui/widgets/tunnel_tree.py L410을 수정하지 않고 통과한다 (UI 파일은 Round 3 소유 — 절대 건드리지 말 것).
- [CC-000] group_manager.py는 config_manager를 모듈 레벨에서 import하지 않는다 (config_manager가 group_manager를 import하므로 순환 import 발생). 타입 힌트가 필요하면 typing.TYPE_CHECKING + 문자열 어노테이션 사용. 또한 group_manager.py에는 APP_DIR/CONFIG_FILE/_CONFIG_LOCK 같은 모듈 레벨 경로/락 상수를 절대 두지 않는다 — tests/test_config_manager.py가 importlib.reload(src.core.config_manager)로 테스트별 격리를 하므로, 모든 상태는 주입된 ConfigManager 인스턴스를 통해서만 접근해야 reload 격리가 유지된다.
- [CC-003] ConfigManager에 private 헬퍼 _mutate_config(self, mutator)를 추가한다: with _CONFIG_LOCK: config = self.load_config(); should_save, result = mutator(config); if should_save: self.save_config(config); return result. mutator는 (should_save: bool, result) 튜플을 반환하는 클로저. 주의: finding 권고안의 '항상 save' 형태로 만들면 안 된다 — add_group(중복 이름), update_group(중복/미발견), delete_group(미발견), move_tunnel_to_group(터널/그룹 미발견), save_group_collapsed_state(미발견)는 현재 save_config 없이 조기 return하므로, 항상 저장하면 백업 로테이션/리비전 동작이 변한다 (behavior-preserving 위반).
- [CC-003] 7개 호출부 전환: set_app_setting(L573-580)과 save_active_tunnels(L600-606)은 ConfigManager 내부에서 _mutate_config를 직접 사용, 그룹 5개 mutator는 TunnelGroupManager에서 self._config._mutate_config로 사용. 로그 호출 위치 보존: add_group/save_active_tunnels/move_tunnel_to_group은 현재 락 해제 후(with 블록 밖) 로깅하므로 _mutate_config 반환 후에 로깅하고, update_group/delete_group은 현재 락 안에서 로깅하므로 mutator 클로저 내부에서 로깅한다.
- [CC-017] config_manager.py 상단 모듈 상수 영역(MAX_BACKUPS 근처)에 FILE_ATTRIBUTE_HIDDEN = 0x02를 정의하고(Win32 SetFileAttributesW 플래그라는 주석 포함) CredentialEncryptor._ensure_key_exists의 L66 리터럴 0x02를 이 상수로 교체한다. CredentialEncryptor 클래스 자체는 이 WP 범위 밖이므로 config_manager.py에 그대로 둔다.
- 신규 tests/test_group_manager.py 작성: tests/test_config_manager.py의 env patch(LOCALAPPDATA/HOME) + importlib.reload 패턴을 재사용한다. 커버 항목 — add_group 중복 이름 거부(저장 없음), update_group 미발견/이름 중복 rename 거부, delete_group 시 tunnel_ids가 ungrouped_order로 이동, move_tunnel_to_group의 3분기(존재하지 않는 터널/존재하지 않는 대상 그룹/group_id=None), save_group_collapsed_state 성공·미발견, 그리고 ConfigManager facade 위임(config_mgr.get_groups() 등 기존 경로)이 여전히 동작하는지 회귀 테스트. 현재 그룹 CRUD에 유닛 테스트가 전무하므로 이 파일이 추출의 게이트다.
- src/core/__init__.py는 건드리지 않는다 (동일 라운드 다른 WP와의 파일 충돌 방지). TunnelGroupManager가 직접 필요하면 from src.core.group_manager import TunnelGroupManager로 import한다. files_touched/new_files 밖의 파일 수정이 필요해 보이면 즉시 중단하고 재스케줄을 요청할 것 — 설계상 facade 유지로 그런 상황이 발생하지 않아야 한다.
- 동작 보존 전용 리팩토링: 기능 변경/메시지 변경/반환 형태 변경/버전 bump 금지. DB 관련 코드는 이 WP에 없으며 tunnelforge-core 소유권 원칙에 영향 없음.

**검증:**
- `python -m py_compile src/core/config_manager.py src/core/group_manager.py`
- `python -m pytest tests/test_config_manager.py tests/test_group_manager.py -q`
- `python -m pytest -q`

**리스크:**
- 그룹 메서드 소비자가 UI 2개 파일(src/ui/main_window.py 8개 호출부, src/ui/widgets/tunnel_tree.py L410)에 있으므로 ConfigManager에 동일 시그니처 facade를 반드시 남겨야 한다 — facade 누락 시 Round 3 파일을 건드리게 되어 라운드 규칙 위반.
- _mutate_config를 finding 권고 그대로(항상 save) 구현하면 7개 중 5개 사이트의 조기 no-save return 의미가 깨져 백업 로테이션/스냅샷 리비전 동작이 변한다 — should_save 플래그 방식 필수.
- tests/test_config_manager.py는 importlib.reload(src.core.config_manager)로 테스트 격리를 한다. group_manager 모듈은 reload되지 않고 캐시되므로, group_manager.py가 모듈 레벨에서 경로 상수나 _CONFIG_LOCK을 참조하면 stale 참조로 테스트가 오염된다 — 인스턴스 주입 방식으로만 접근해야 함.
- config_manager(모듈 레벨에서 TunnelGroupManager import) <-> group_manager 순환 import 위험: group_manager 쪽에서 config_manager를 모듈 레벨 import하지 않는 것으로 회피 (TYPE_CHECKING 전용).
- src/core/__init__.py에 TunnelGroupManager export를 추가하지 않기로 함 — 동일 라운드 타 WP가 __init__.py를 수정할 가능성이 있어 충돌 방지 목적. 필요 시 후속 라운드에서 export 추가 가능.
- src/core/config_manager.py는 CC-000 외 나머지 책임(백업/머지/import-export 검증)이 남아 있어 Round 2 WP가 같은 파일을 다시 다룰 수 있음 — 라운드가 다르므로 허용되나 merge 순서상 이 WP가 선행되어야 함.
- 전체 python -m pytest 실행 시 macOS validation 테스트는 로컬에서 항상 실패하는 GitHub CI 의존 테스트(레포 메모리 기록)이므로 해당 실패는 회귀로 판정하지 말 것.

### WP-1.4 — split
**Branch:** `refactor/cc-r1-i18n-split` · **Size:** L · **발견:** 6건 (H1/M3/L2)

**Findings covered:** CC-041, CC-042, CC-043, CC-044, CC-045, CC-046

**수정 파일:** `src/core/i18n.py`, `tests/test_i18n.py`
**신규 파일:** `src/core/i18n/__init__.py`, `src/core/i18n/keys.py`, `src/core/i18n/legacy_translate.py`, `src/core/i18n/qt_hooks.py`
**테스트:** `tests/test_i18n.py`

**가이드:**
- [CC-041] src/core/i18n.py(1627줄)를 패키지로 전환: 기존 파일을 삭제하고 src/core/i18n/ 패키지를 생성한다. keys.py = 1-250줄 영역(DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, INSTALLER_LANGUAGE_HINT_FILE, _current_language 상태, _TRANSLATIONS, normalize_language, detect_system_language, installer_language_hint_path/read_installer_language_hint/consume_installer_language_hint, language_from_args, current_language, set_language, configure_language, tr, language_label). legacy_translate.py = 253-1389줄 영역(_EN_TEXT/_EN_PHRASE/_EN_REGEX/_EN_WORD_TRANSLATIONS 4개 테이블, _has_hangul, translate_text, _translate_sequence — keys에서 current_language, DEFAULT_LANGUAGE를 import). qt_hooks.py = 1392-1627줄 영역(install_qt_i18n, translate_qt_arg, patch_init/patch_method/patch_all_string_args_method + 인라인 패치 블록 — legacy_translate에서 translate_text, _translate_sequence를 import). 같은 경로에 i18n.py 파일과 i18n/ 디렉토리를 공존시키면 안 됨(패키지가 모듈을 가림) — 파일 삭제 필수.
- [CC-041] __init__.py는 순수 재수출 shim으로 작성: 소비자 12개 import 사이트(main.py x2, src/ui/main_window.py, src/ui/theme_manager.py, src/ui/widgets/tunnel_tree.py, src/ui/dialogs/settings.py 및 dialogs 4개, tests)가 쓰는 이름 전부 재수출 — configure_language, install_qt_i18n, tr, translate_text, SUPPORTED_LANGUAGES, current_language, set_language, DEFAULT_LANGUAGE, INSTALLER_LANGUAGE_HINT_FILE, normalize_language, detect_system_language, installer_language_hint_path, read_installer_language_hint, consume_installer_language_hint, language_from_args, language_label. 소비자 파일(main.py, src/ui/*)은 한 줄도 수정하지 않는다.
- [CC-041] 상태 소유권: _current_language 전역은 keys.py 단독 소유로 두고 다른 모듈은 반드시 current_language()/set_language() 함수 경유로 접근한다(translate_text는 이미 current_language()를 호출하므로 함수 import면 언어 전환이 즉시 반영됨 — 전역 변수 자체를 from-import 하면 스냅샷이 복사되어 버그 발생, 금지). _qt_i18n_installed 플래그는 qt_hooks.py로 이동.
- [CC-041 테스트 동기화] tests/test_i18n.py 수정 2건 필수: (a) test_en_phrase_translations_have_no_duplicate_source_keys(178-200줄)의 하드코딩 경로 'src/core/i18n.py'를 'src/core/i18n/legacy_translate.py'로 변경하고, _EN_PHRASE_TRANSLATIONS 할당 노드를 하나도 못 찾으면 실패하도록 found 플래그 assert를 추가(무음 통과 방지). (b) test_configure_language_uses_installer_hint_before_system_locale(47-58줄)의 monkeypatch.setattr(i18n, 'installer_language_hint_path'/'detect_system_language', ...)는 패키지 attr 패치라 keys.py 내부 참조에 적용되지 않음 — monkeypatch 대상을 src.core.i18n.keys 모듈로 변경(프로젝트 메모리 god-file-split-pitfalls의 monkeypatch 오지정 함정과 동일 케이스). 나머지 테스트는 shim 재수출로 그대로 통과해야 한다.
- [CC-043] qt_hooks.py에서 8개 인라인 monkey-patch 블록(구 1517-1523, 1525-1531, 1533-1539, 1541-1550, 1552-1563, 1565-1576, 1578-1598, 1604-1624)의 공통 스캐폴딩(원본 저장 → _tf_i18n_wrapped 가드 → wrapped._tf_i18n_wrapped = True → setattr 재할당)을 단일 제네릭 헬퍼로 추출. 예: _wrap_callable(container, name, make_args) — make_args는 사이트별 인자 변환 함수. 각 사이트의 인자 처리 의미는 정확히 보존: QToolTip.showText는 args[1]만, QMenu.addAction/addMenu는 args[0]이 str이면 args[0] 아니면 args[1], QMessageBox 4종 static은 args[1,2] + kwargs title/text, QFileDialog 3종 static은 args[1,3] + kwargs caption/filter, 헤더 라벨 3종(QTreeWidget.setHeaderLabels, QTableWidget.setHorizontal/VerticalHeaderLabels)은 _translate_sequence(list(labels)) — 인자 정책을 통합·일반화하지 말 것(동작 보존).
- [CC-044] legacy_translate.py에서 translate_text()를 명명된 파이프라인으로 분해: _apply_regex_pairs(value), _apply_phrase_substitutions(value), _apply_word_substitutions(value), _strip_korean_particles(value) 사설 함수를 만들고 translate_text()는 기존 조기반환(비문자열/현재언어==ko/한글없음 passthrough, exact dict lookup, stripped dict lookup) 후 이들을 순서대로 호출하는 짧은 본문으로 만든다. 공개 시그니처(translate_text(value), 임의 객체 허용)와 출력은 완전 동일 유지.
- [CC-045] 매 호출 재정렬 제거: legacy_translate.py 모듈 로드 시 _SORTED_PHRASE_TRANSLATIONS = tuple(sorted(_EN_PHRASE_TRANSLATIONS.items(), key=lambda kv: len(kv[0]), reverse=True)) 와 워드 테이블 등가물 _SORTED_WORD_TRANSLATIONS를 프리컴퓨트하고, 파이프라인 함수가 이 튜플을 순회하도록 변경(구 1371, 1374줄의 sorted() 호출 제거). sorted는 안정 정렬이므로 동일 길이 키의 tie-order가 dict 삽입순으로 유지되어 결과 문자열이 바이트 단위로 동일함.
- [CC-046][CC-042 — LOW sweep] (a) legacy_translate.py에 _HANGUL_SYLLABLES_START = "가", _HANGUL_SYLLABLES_END = "힣" 상수를 'Hangul Syllables Unicode block 경계(가~힣)' 주석과 함께 도입하고, _has_hangul의 비교식과 워드 경계 정규식(구 1376줄)의 문자 클래스를 모두 이 상수로부터 구성(예: _HANGUL_CHAR_CLASS = f"[{_HANGUL_SYLLABLES_START}-{_HANGUL_SYLLABLES_END}]"). (b) 낡은 'Small runtime i18n layer' docstring은 분할로 자연 해소 — __init__.py에는 패키지 전체 개요(structured tr() keys + legacy Korean auto-translation shim + PyQt widget-text patching), 각 서브모듈에는 자기 역할만 기술하는 정확한 docstring을 부여한다.
- 하드 제약: 동작 보존 리팩토링만 수행(기능 변화 금지, 버전 bump 금지, DB 경로 무관). 수정 허용 파일은 src/core/i18n.py(삭제·전환), 신규 패키지 4파일, tests/test_i18n.py 뿐 — main.py, src/ui/*, CLAUDE.md, docs/* 를 고쳐야 하는 상황이 생기면 즉시 중단하고 재스케줄을 요청한다(재수출 shim 설계상 발생하지 않아야 정상). 커밋 전 grep으로 'from src.core.i18n import' 소비자들이 쓰는 이름이 __init__.py 재수출 목록에 전부 있는지 교차 확인.

**검증:**
- `python -m py_compile src/core/i18n/__init__.py src/core/i18n/keys.py src/core/i18n/legacy_translate.py src/core/i18n/qt_hooks.py tests/test_i18n.py`
- `python -m pytest tests/test_i18n.py -v`
- `python -m pytest -q`

**리스크:**
- import fan-out: 10개 파일/12개 import 사이트가 src.core.i18n을 소비 — 재수출 shim으로 전부 흡수되지만, 재수출 이름이 하나라도 누락되면 앱 기동(main.py의 configure_language/install_qt_i18n) 자체가 실패한다. 커밋 전 grep 교차검증 필수.
- tests/test_i18n.py가 소스 경로를 하드코딩(AST 파싱 테스트: src/core/i18n.py)하고 모듈 attr monkeypatch를 사용 — 이 WP에서 테스트를 함께 수정하지 않으면 test_en_phrase_translations_have_no_duplicate_source_keys(FileNotFoundError 또는 무음 통과)와 test_configure_language_uses_installer_hint_before_system_locale(패치 미적용)이 깨진다.
- 모듈→패키지 전환 특성: i18n.py와 i18n/ 디렉토리가 같은 경로에 공존하면 패키지가 모듈을 가려 예측 불가능한 import가 됨 — 기존 파일 삭제가 필수 단계. 또한 CLAUDE.md의 'python -m py_compile src/core/*.py' 예시 커맨드는 새 패키지 하위 파일을 커버하지 못하나 CLAUDE.md는 이 WP 범위 밖(문서 갱신은 후속 처리, 이 WP의 verification이 패키지 파일을 명시적으로 나열해 보완).
- 별도의 감사 플랜(.claude/audit-master-plan-2026-07-08.md)의 WP-1.4도 src/core/i18n.py:1066/1507을 대상으로 함 — 그 플랜이 병행 실행되면 같은 파일에서 충돌하므로 실행 순서 조정 필요(이 클린코드 플랜 내 같은 라운드 중복은 없음: round 1에서 i18n을 만지는 WP는 본 WP뿐).
- 전체 pytest 실행 시 macOS validation 테스트는 로컬에서 항상 실패하는 GitHub CI 의존 테스트(프로젝트 메모리 기록) — 회귀 판정에서 제외할 것. PyQt 테스트는 offscreen 플랫폼을 테스트가 자체 설정하지만 Windows 환경에서 hang 이력이 있어 timeout을 두고 실행 권장.
- 동작 보존 함정 2건: (1) 정렬 프리컴퓨트는 안정 정렬의 tie-order(딕셔너리 삽입순) 유지가 전제 — sorted 키 함수를 바꾸면 안 됨. (2) monkey-patch 헬퍼 추출 시 사이트별 인자 semantics(위치 인덱스/kwargs 이름/시퀀스 list() 정규화)를 통합하려 들면 QComboBox 식별자 보존 같은 의도된 비대칭이 깨진다 — 스캐폴딩만 공통화.

### WP-1.5 — shared-constants
**Branch:** `refactor/cc-r1-migration-shared-constants` · **Size:** M · **발견:** 5건 (H1/M3/L1)

**Findings covered:** CC-051, CC-062, CC-063, CC-146, CC-147

**수정 파일:** `src/core/migration_analyzer.py`, `src/core/migration_constants.py`, `src/core/migration_dump_analyzer.py`, `src/ui/dialogs/migration_dialogs.py`, `src/ui/dialogs/fix_wizard_issue_selection_page.py`, `src/ui/dialogs/fix_wizard_option_page.py`, `src/ui/dialogs/migration_manual_guide_dialog.py`
**신규 파일:** `src/core/migration_identifier_matchers.py`
**테스트:** `tests/test_migration_constants.py`, `tests/test_migration_analyzer.py`, `tests/test_migration_rules.py`, `tests/test_fix_wizard_dialog.py`, `tests/test_migration_fix_wizard.py`

**가이드:**
- [CC-051] migration_analyzer.py: MigrationAnalyzer.NEW_RESERVED_KEYWORDS 클래스 속성(179-186)을 완전히 삭제하고, import 블록(19-35)에 `ALL_RESERVED_KEYWORDS`를 추가한다. check_reserved_keywords(422)의 `self.NEW_RESERVED_KEYWORDS`를 모듈 상수 `ALL_RESERVED_KEYWORDS`로 교체한다(list→tuple 이지만 set 컴프리헨션 결과 동일 → 동작 불변). 이 속성을 참조하는 외부/테스트 소비자는 없음(내부 2곳뿐)이므로 alias 없이 삭제해도 안전.
- [CC-051] migration_dump_analyzer.py: 모듈 상단 import 블록(9-21)에 `ALL_RESERVED_KEYWORDS`를 추가하고, _analyze_sql_file 내부의 함수 스코프 `from src.core.migration_analyzer import MigrationAnalyzer`(140)를 제거한다. 258줄 `MigrationAnalyzer.NEW_RESERVED_KEYWORDS`를 `ALL_RESERVED_KEYWORDS`로 교체. 이 함수 스코프 import는 순환참조 회피용이었으나 이제 필요 없어짐(MigrationAnalyzer는 이 파일 내 다른 곳에서 안 쓰임 — 확인됨).
- [CC-062] 새 모듈 src/core/migration_identifier_matchers.py 생성: `_IdentifierIssuePattern`(433-485), `_ContextualDotPattern`(488-512), 헬퍼 `_CONTROL_CHAR_INNER_PATTERN`(584-587), 그리고 이들에 의존하는 모듈 인스턴스 DOLLAR_SIGN_PATTERN(590)/TRAILING_SPACE_PATTERN(593-595)/CONTROL_CHAR_PATTERN(598-600)/INVALID_57_NAME_MULTIPLE_DOTS_PATTERN(668)을 이동. 새 모듈은 `import re`만 의존(자기완결적). 422-432의 설계 근거 주석도 함께 이동.
- [CC-062] 하위호환 필수: migration_constants.py는 이동한 인스턴스들을 그대로 re-export 한다 — 삭제된 정의 위치에 `from src.core.migration_identifier_matchers import (DOLLAR_SIGN_PATTERN, TRAILING_SPACE_PATTERN, CONTROL_CHAR_PATTERN, INVALID_57_NAME_MULTIPLE_DOTS_PATTERN)` 추가. schema_rules.py(41-54)와 test_migration_constants.py(48-53)는 여전히 `from src.core.migration_constants import ...`로 읽으므로 절대 수정 금지. 순환참조 없음(새 모듈은 constants를 import 안 함).
- [CC-062] 이동 시 중간에 끼어있는 일반 re.Pattern 상수들(TIMESTAMP_PATTERN, BLOB_TEXT_DEFAULT_PATTERN, GENERATED_COLUMN_PATTERN, PARTITION_PREFIX_KEY_PATTERN 등)은 matcher 클래스 기반이 아니므로 migration_constants.py에 그대로 남긴다. 매처 클래스 파생 4개 인스턴스만 선택적으로 이동.
- [CC-063] migration_constants.py: typing import(10)를 `from typing import Any, Dict, List, Optional, Tuple`로 확장하고, STORAGE_ENGINE_STATUS 어노테이션(302)을 `Dict[str, any]` → `Dict[str, Any]`로 변경. 런타임 값 불변 → test_migration_constants.py(198-208) 그대로 통과.
- [CC-146] migration_constants.py에 `AUTO_FIXABLE_ISSUE_TYPES: frozenset[IssueType]`(7개: INVALID_DATE, CHARSET_ISSUE, ZEROFILL_USAGE, FLOAT_PRECISION, INT_DISPLAY_WIDTH, DEPRECATED_ENGINE, ENUM_EMPTY_VALUE) 단일 소스 신설. migration_dialogs.py는 클래스 속성을 `AUTO_FIXABLE_TYPES = AUTO_FIXABLE_ISSUE_TYPES`(import 후)로 바꿔 내부 사용처(1171/1172/1197/1243) 무변경 유지. fix_wizard_issue_selection_page.py의 로컬 set(93-101)을 삭제하고 공유 상수 참조로 교체.
- [CC-147] migration_constants.py에 canonical `ISSUE_TYPE_DISPLAY_NAMES: Dict[IssueType, str]` 신설 — 5개 dict(migration_dialogs 871-889, migration_manual_guide_dialog 199-205, fix_wizard_issue_selection_page 103-114, fix_wizard_option_page 59-68 및 362-370)의 합집합 키를 모두 포함하고, 드리프트된 라벨은 가장 서술적인 값으로 통일(예: RESERVED_KEYWORD='예약어', CHARSET_ISSUE='문자셋 이슈', ZEROFILL_USAGE='ZEROFILL 속성'). 각 파일의 로컬 type_names dict를 삭제하고 `.get(issue_type, str(issue_type.value))` 폴백은 유지한 채 공유 상수를 참조.
- [CC-146/CC-147] import 배선: 4개 UI 파일 각각에 `from src.core.migration_constants import ISSUE_TYPE_DISPLAY_NAMES, AUTO_FIXABLE_ISSUE_TYPES`(해당 파일에서 실제 쓰는 것만) 추가. migration_dialogs.py/migration_manual_guide_dialog.py는 IssueType을 migration_analyzer 경유로 import 중이나, 신규 상수는 migration_constants에서 직접 import하면 되고 소비 파일 외 파일은 건드리지 않는다.
- [HARD CONSTRAINTS] 순수 behavior-preserving 리팩터: 로직/제어흐름 변경 금지, 버전 bump 금지, Python DB 드라이버 hot path 재도입 금지(이 WP는 DB 접근과 무관). 구 import 경로는 전부 re-export/alias로 유지. files_touched/new_files 밖 파일(schema_rules.py, test_*, 다른 소비자)을 수정해야 하는 상황이 생기면 즉시 멈추고 리스케줄 요청 — 본 설계는 re-export 전략으로 그럴 필요가 없도록 되어 있음.
- [LOW sweep] 표시 라벨 통일은 의도된 텍스트 정규화이며(드리프트 제거), 특정 다이얼로그의 일부 한국어 라벨이 미세하게 바뀔 수 있음(예: fix_wizard_issue_selection의 'ZEROFILL'→'ZEROFILL 속성'). 이는 CC-147의 명시적 목표이므로 허용하되, 로직/enum 값은 절대 변경하지 않는다.

**검증:**
- `python -m py_compile src/core/migration_constants.py src/core/migration_identifier_matchers.py src/core/migration_analyzer.py src/core/migration_dump_analyzer.py src/ui/dialogs/migration_dialogs.py src/ui/dialogs/fix_wizard_issue_selection_page.py src/ui/dialogs/fix_wizard_option_page.py src/ui/dialogs/migration_manual_guide_dialog.py`
- `python -m pytest tests/test_migration_constants.py tests/test_migration_analyzer.py tests/test_migration_rules.py tests/test_fix_wizard_dialog.py tests/test_migration_fix_wizard.py -q`
- `python -m pytest -q`

**리스크:**
- CC-147 통합은 4개 파일의 실제 사용자 표시 라벨을 하나로 강제하므로, 컨텍스트별로 의도적으로 달랐던 일부 라벨(예: fix_wizard_option_page.show_current_issue의 '잘못된 날짜 (0000-00-00)'처럼 예시가 붙은 라벨)이 다른 다이얼로그와 동일해진다. enum 값이 아닌 순수 표시 텍스트 변경이며 CC-147의 목표이지만, 스크린샷 기반 UI 회귀 테스트가 있다면 라벨 diff가 감지될 수 있음.
- CC-062 새 모듈 재배치 후 migration_constants.py의 re-export를 빠뜨리면 schema_rules.py(41-54)와 test_migration_constants.py(48-53)가 ImportError로 즉시 깨진다 — re-export가 이 WP의 핵심 안전장치. schema_rules.py는 files_touched에 없으므로 절대 수정 금지(수정이 필요하다고 판단되면 멈추고 리스케줄).
- 교차 라운드 파일 중복(허용됨, 동일 라운드 아님): migration_analyzer.py/migration_constants.py는 Round 2 core 정리 WP에서, migration_dialogs.py/fix_wizard_*/migration_manual_guide_dialog.py는 Round 3 UI WP에서 다시 손댈 가능성이 높다. 본 WP가 공유 상수(ISSUE_TYPE_DISPLAY_NAMES, AUTO_FIXABLE_ISSUE_TYPES)를 신설하는 foundational WP이므로 Round 2/3 WP들이 이 WP에 depends_on 되어야 하며, 이 WP가 먼저 머지되어야 한다.
- migration_dialogs.py와 migration_manual_guide_dialog.py는 IssueType을 migration_analyzer 경유로 import한다(migration_analyzer가 constants를 re-export). 신규 상수를 migration_analyzer가 아닌 migration_constants에서 직접 import하도록 배선하면 소비 파일 범위를 벗어나지 않는다 — 실수로 migration_analyzer에도 re-export를 추가할 필요는 없음(추가 시 불필요한 결합).
- CC-051에서 NEW_RESERVED_KEYWORDS(list) → ALL_RESERVED_KEYWORDS(tuple) 타입이 바뀌지만 두 소비처 모두 set 컴프리헨션으로 소비하므로 동작 동일. 다만 향후 누군가 인덱싱/mutation을 기대하지 않는지 확인(현재 코드엔 없음).

### WP-1.6 — unification
**Branch:** `refactor/cc-r1-styles-unification` · **Size:** M · **발견:** 2건 (H2/M0/L0)

**Findings covered:** CC-181, CC-204

**수정 파일:** `src/ui/dialogs/settings.py`, `src/ui/styles.py`
**테스트:** `tests/test_settings_update_launch.py`, `tests/test_settings_update_actions.py`, `tests/test_tunnel_config_dialog.py`, `tests/test_main_window_export_import_labels.py`, `tests/test_tunnel_tree.py`

**가이드:**
- [CC-181] settings.py 의 12개 인라인 QPushButton `setStyleSheet("""...""")` 블록을 styles.ButtonStyles 상수 참조로 전량 치환한다. 치환 전 각 블록을 대상 상수 값과 바이트 단위로 재diff 하여 CSS 파싱 결과가 동일할 때만 같은 상수를 공유(동작 보존). settings.py 는 스타일 치환만 하고 레이아웃/시그널/로직은 손대지 않는다(갓클래스 분해는 Round 3 WP-3.7).
- [CC-181] btn_save(67-73) -> `ButtonStyles.PRIMARY`, btn_cancel(77-83) -> `ButtonStyles.SECONDARY`. 두 상수는 인라인 블록에 없던 `QPushButton:disabled` 규칙을 추가로 포함하나, 이 두 버튼은 비활성화되는 코드 경로가 없어 관찰 가능한 시각 변화가 없다(의도된 무해한 델타로 PR 설명에 명시).
- [CC-181] styles.ButtonStyles 에 신규 상수 추가 — 실제 바이트-동일 그룹만 공유: `INFO_SMALL`(파랑 #3498db, padding 6px 12px, font-size 11px, disabled 없음) -> btn_export(271-278)+btn_refresh_log(466-473); `MUTED_SMALL`(회색 #95a5a6 bg/white, padding 6px 12px, font-size 11px) -> btn_import(283-290)+btn_open_log_folder(479-486). 이 두 쌍만 실측상 동일하다.
- [CC-181] 주의: finding 의 byte-identical 주장 일부가 부정확하다. 아래는 서로 달라 각각 별도 상수로 두고 시각 변화를 만들지 말 것 — btn_test(200-208)=회색+`min-height:26px` 추가분 전용 상수(예: `MUTED_SMALL_TALL`); btn_restore(259-266)=초록 #27ae60/6px12px/fs11/hover #219a52 -> `SUCCESS_SMALL`; btn_clear_log(492-499)=빨강/6px12px/fs11 -> `DANGER_SMALL`; btn_cancel_download(657-664)=빨강이지만 8px12px/fs12 로 clear_log 와 다름 -> `DANGER_MD` 별도(finding 이 둘을 묶은 것은 오류); btn_check_update(601-609)=파랑 8px16px/fs12/disabled #bdc3c7 -> `PRIMARY_MD`; btn_download(644-652)=초록 8px16px/fs12/hover #229954/disabled #bdc3c7 -> `SUCCESS_MD`.
- [CC-181] install 재스타일(902-909, `_on_download_finished` 에서 btn_download 를 보라색으로 전환)=보라 #9b59b6/8px16px/fs12/font-weight bold/hover #8e44ad -> `ButtonStyles.INSTALL` 신규 상수로 치환.
- [CC-181] 신규 상수 문자열은 원본 인라인 CSS 를 공백/개행까지 그대로 옮긴다. 치환 후 settings.py 에 QPushButton 인라인 스타일 블록이 0개인지 확인. backup_list(QListWidget), update_status(QTextBrowser), 각종 QLabel 인라인 스타일 등 non-button 인라인은 이 WP 범위 밖이므로 그대로 둔다.
- [CC-204] 경쟁 시스템 정리는 styles.py 내부에서만 수행한다. 활성 ButtonStyles/LabelStyles 는 main_window.py, dialogs/tunnel_config.py, dialogs/group_dialog.py 3개 Round-3 파일이 여전히 사용하므로 삭제 금지 — call-site 마이그레이션과 ButtonStyles/LabelStyles 삭제는 Round 3 로 이연(이 파일들을 수정해야 하면 즉시 중단하고 재스케줄 요청).
- [CC-204] styles.py 에서 외부 참조 0인 완전 사장(dead) 경쟁 코드만 삭제: 클래스 `TableStyles`,`TabStyles`,`DialogStyles`,`ProgressStyles`,`TextEditStyles`,`GroupBoxStyles`,`InputStyles`,`Colors`(styles 모듈 색상 클래스 — scripts/smart_release.py 의 동명 ANSI Colors 와 무관) 및 헬퍼 `apply_button_style`,`apply_label_style`. 모두 get_dynamic_* 함수군으로 대체되어 있고 repo 전역 grep 상 styles.py 밖 참조가 없음(검증 완료).
- [CC-204] `get_dynamic_button_style`/`get_dynamic_label_style` 는 현재 미사용이나 Round 3 테마 마이그레이션의 목표 API 이므로 삭제하지 말고 유지(주석으로 'Round 3 call-site 마이그레이션 대상' 표시). `get_full_app_style` 과 그것이 호출하는 get_dynamic_input/table/tab/list/scrollbar/progress/groupbox_style 은 활성 코드이므로 유지.
- [CC-204] 삭제 직전 각 대상 심볼마다 `grep -rn "<symbol>" src tests` 로 styles.py 외 참조 0 을 재확인. 하나라도 참조가 있으면 그 심볼은 삭제하지 말고 risks 로 기록하고 나머지만 진행. widgets/tunnel_tree.py:14 의 사용되지 않는 `from src.ui.styles import ButtonStyles` 는 Round-3 파일이므로 건드리지 말고 남겨둔다(무해).
- (sweep) 버전 bump 금지·src/version.py 등 무관 파일 수정 금지. DB 로직 불변(순수 UI 스타일 영역, tunnelforge-core 소유권 유지). 모든 신규 상수는 styles.py 안에 두어 기존 `from src.ui.styles import ButtonStyles/LabelStyles/...` import 경로가 그대로 동작하도록 한다(심볼 이동/재export 없음).

**검증:**
- `python -m py_compile src/ui/styles.py src/ui/dialogs/settings.py`
- `python -c "from src.ui.styles import ButtonStyles, LabelStyles, get_full_app_style, get_dynamic_button_style, get_dynamic_label_style"`
- `python -m pytest tests/test_settings_update_launch.py tests/test_settings_update_actions.py tests/test_tunnel_config_dialog.py tests/test_main_window_export_import_labels.py tests/test_tunnel_tree.py -q`
- `python -m pytest`

**리스크:**
- CC-181 의 byte-identity 주장 일부 부정확: btn_clear_log != btn_cancel_download(padding/font-size 상이), btn_restore/btn_check_update/btn_download 는 각각 고유 변형이라 finding 이 제안한 4개 상수로는 부족. 실제로는 ~9개 상수가 필요(guidance 에 매핑 명시). 무비판적 통합 시 버튼 padding/font-size 가 바뀌는 시각 회귀 발생.
- btn_save/btn_cancel -> PRIMARY/SECONDARY 치환은 인라인에 없던 :disabled 규칙을 추가하는 비-동일 치환(해당 버튼이 절대 disabled 되지 않아 실질 무해하나 엄밀히는 스타일 델타 — 의도적 결정으로 문서화 필요).
- CC-204 완전 해결(call-site 마이그레이션 + ButtonStyles/LabelStyles 삭제)은 main_window.py/tunnel_config.py/group_dialog.py(모두 Round-3 파일, files_touched 밖) 수정을 요구하므로 Round 1 에서는 dead-code 삭제만 수행하고 활성 클래스 삭제는 이연. 실행 에이전트가 이 3개 파일 수정이 필요하다고 판단하면 중단·재스케줄 대상.
- settings.py 는 Round-3 갓클래스(WP-3.7)로 later-round 에서 다시 수정됨 — later-round overlap 은 허용이나 WP-3.7 이 본 WP 의 중앙화된 상수 기반으로 rebase 해야 함. 같은 Round 1 내에서 다른 WP 가 settings.py 또는 styles.py 를 claim 하면 금지된 동일-라운드 overlap 이므로 착수 전 확인 필요(현재로선 미발견).
- styles.py 는 직접 테스트 커버리지가 없음(어떤 test 도 import 하지 않음). 동작 보존은 py_compile + UI smoke 테스트(tunnel_config/main_window/tunnel_tree import)로만 간접 검증되므로 버튼 외형 회귀는 정적 검증만으로 잡히지 않음 — 완화책은 신규 상수에 원본 CSS 를 verbatim 복사.
- get_dynamic_dialog_style(styles.py:570) 도 현재 미사용(get_full_app_style 미포함)이나 dynamic 계열이라 삭제 대상에서 제외하고 유지 권장 — dead-class 삭제 범위를 초과 확장하지 말 것.

### WP-1.7 — scripts-dedup
**Branch:** `refactor/cc-r1-release-scripts-dedup` · **Size:** S · **발견:** 2건 (H1/M1/L0)

**Findings covered:** CC-219, CC-220

**수정 파일:** `scripts/smart_release.py`, `scripts/bump_version.py`
**테스트:** `tests/test_bump_version.py`, `tests/test_smart_release.py (new)`

**가이드:**
- [CC-219] scripts/smart_release.py 상단에 `sys.path.insert(0, str(Path(__file__).resolve().parent))` 를 추가한 뒤 `from versioning import read_version, write_version, sync_pyproject, sync_installer, bump_version, compare_versions` 로 통합 임포트한다 (Path/sys 는 이미 임포트됨). scripts/ 가 실행 시 sys.path[0] 이지만 import-as-module 대비로 명시 insert — bump_version.py 패턴과 동일하게 맞춘다.
- [CC-219] smart_release.py 의 중복 재구현 4개를 삭제한다: `compare_versions`(59-70), `get_local_version`(73-79), `bump_version`(113-122, main 에서 호출되지 않는 dead code), `update_version_file`(125-132). 삭제 후 versioning.py 의 검증/robustness(잘못된 bump_type·버전형식 ValueError, write_version 의 re.subn count 체크) 를 자동 승계한다.
- [CC-219] Step1 호출부(174)를 `local_version, version_content = get_local_version(version_file)` → `local_version = read_version(version_file)` 로 바꾸고 version_content 변수를 제거한다 (write_version 이 파일을 자체 재읽기하므로 content 전달 불필요). 기존 `except ValueError` 는 read_version 도 ValueError 를 던지므로 그대로 유지.
- [CC-219] 버전 선택 메뉴(225-232)의 인라인 파싱 `parts=[int(x)...]` 3중 중복도 제거: patch_ver/minor_ver/major_ver 를 각각 `bump_version(local_version,'patch'|'minor'|'major')` 로 계산한다. UI 출력 문구/색상은 그대로 보존.
- [CC-219] Step5(320-325)의 `update_version_file(version_file, version_content, new_version)` → `write_version(version_file, new_version)` 로 교체하고, 그 직후 `pyproject_file = Path('pyproject.toml')`, `installer_file = Path('installer/TunnelForge.iss')` (os.chdir(project_root) 이후라 상대경로 OK) 를 각각 `.exists()` 가드로 `sync_pyproject`/`sync_installer` 호출해 HIGH 드리프트를 닫는다 — 이는 문서화된 '긴급 fallback' 경로가 CI(bump_version.py) 와 동일한 3파일 동기화를 갖게 하려는 CC-219 의 핵심 의도다. 반드시 needs_bump 경로에서만 동기화하고, needs_bump=False(로컬이 이미 높음) 경로는 기존대로 write/sync 를 건너뛴다.
- [CC-219] 동기화 추가에 맞춰 커밋 일관성 유지: git add(342) 를 version_file 단독이 아니라 실제 존재하는 동기화 대상(version_file + 존재 시 pyproject_file/installer_file) 목록으로 확장하고, dry-run 프리뷰(298-315)의 '업데이트될 파일' 목록에도 pyproject.toml / installer/TunnelForge.iss 를 추가해 미리보기와 실제 동작을 일치시킨다.
- [CC-220] scripts/bump_version.py 에 모듈 레벨 헬퍼 `_apply_sync(sync_fn, path, new_version, label, required) -> int | None` 를 신설한다: required=False 이고 not path.exists() 이면 `[SKIP] {path} 없음...` 출력 후 None 반환; try 로 `sync_fn(path, new_version)` 실행해 성공 시 `[OK] {path} 업데이트 완료` 후 None; `except (ValueError, OSError) as e` 는 `ERROR: {label} 쓰기 실패: {e}` 출력 후 1 반환. write_version/sync_pyproject/sync_installer 셋 다 `(path, new_version)` 시그니처라 동일 헬퍼로 처리 가능.
- [CC-220] main() 의 122-148 블록을 순차 3회 호출로 치환하되 단축평가(short-circuit)를 반드시 보존한다: `rc=_apply_sync(write_version, version_file, new_version, 'version 파일', required=True); if rc is not None: return rc` 를 pyproject('pyproject.toml', required=False), installer('installer .iss', required=False) 순으로 반복. version 쓰기 실패 시 pyproject 동기화를 시도하지 않던 기존 흐름을 그대로 유지해야 한다.
- [CC-220] ERROR/OK/SKIP stderr 문구는 기존과 문자 그대로 일치시킨다(label 은 'version 파일'/'pyproject.toml'/'installer .iss'). 테스트는 stderr 문자열을 검증하지 않지만 회귀 위험을 없애기 위해 동일 문구 유지.
- [제약] 두 파일 모두 내부 리팩터만 수행한다: 공개 함수 시그니처·CLI 인자·exit code·stdout(`new_version=...`) 은 불변. versioning.py 는 수정하지 않고 import 로만 재사용한다(재-export 불필요 — 소비자 없음). 버전 bump 금지, DB 관련 없음. files_touched/new_files 밖 파일(예: main.py, pyproject.toml, installer/TunnelForge.iss 정적 편집)을 건드려야 하면 중단하고 리스케줄 요청 — smart_release.py 는 런타임에만 그 파일들을 수정하므로 정적 편집은 발생하지 않아야 한다.
- [테스트] tests/test_smart_release.py (신규) 를 추가해 dedup 을 락킹한다: scripts 를 sys.path 에 추가 후 `import smart_release` 와 `import versioning` 를 모두 임포트하고, `smart_release.read_version is versioning.read_version`, `smart_release.write_version is versioning.write_version`, `smart_release.compare_versions is versioning.compare_versions`, `smart_release.bump_version is versioning.bump_version` 를 assert. 또한 smart_release 모듈 소스에 `def get_local_version`/`def update_version_file` 이 더 이상 존재하지 않음을 검증(재구현 재발 방지). smart_release import 는 부작용이 없어야 하므로 main() 호출 금지.

**검증:**
- `python -m py_compile scripts/smart_release.py scripts/bump_version.py`
- `python -m pytest tests/test_bump_version.py -q`
- `python -m pytest tests/test_smart_release.py -q`
- `python -m pytest -q`
- `(선택, 네트워크/ git remote 필요 - 하드 게이트 아님) python scripts/smart_release.py --dry-run`

**리스크:**
- 테마에 'main.py 의 발견도 해결' 이 적혀 있으나 WP JSON findings 에는 main.py 항목이 없고, main.py 는 grep 상 version/__version__ 참조가 0건이다. main.py 는 본 WP 범위 밖이며 절대 수정하지 않는다.
- CC-219 의 pyproject/installer 동기화 추가는 순수 behavior-preserving 을 넘어서는 의도된 기능 변경이다(스크립트가 bump 시 pyproject.toml 및 installer/TunnelForge.iss 도 수정·git add 하게 됨). needs_bump 경로 한정 + .exists() 가드 + git-add/dry-run 프리뷰 반영으로 릴리스 커밋 일관성을 유지해야 하며, 이 파일들의 정적 편집은 발생하지 않는다(런타임 수정만).
- smart_release.py 는 git remote/GitHub API/사용자 input 에 의존해 오프라인 end-to-end 유닛테스트가 불가능하다. 신규 test_smart_release.py 는 import 레벨 dedup 만 락킹하고, 전체 릴리스 흐름은 --dry-run 수동 확인으로만 검증 가능하다.
- test_bump_version.py 는 exit code/stdout(new_version)/파일 내용만 검증하고 stderr 문구는 검증하지 않으므로, CC-220 헬퍼 리팩터는 return-code 단축평가 순서만 보존하면 안전하다.
- 라운드1 파일 disjoint 확인됨: 다른 라운드1 WP(god-file split/UI)는 scripts/smart_release.py·scripts/bump_version.py·tests/test_bump_version.py 를 건드리지 않는다. versioning.py 는 본 WP 에서 수정하지 않고 import 로만 재사용하므로 동시 충돌 없음. test_rust_core_packaging.py / test_build_docs.py 의 MyAppVersion 검증은 리포지토리 정적 3파일 동기화 체크로 스크립트 리팩터와 무관.

### WP-1.8 — core-module-split
**Branch:** `refactor/cc-r1-rust-core-module-split` · **Size:** L · **발견:** 1건 (H1/M0/L0)

**Findings covered:** CC-254

**수정 파일:** `migration_core/src/lib.rs`
**신규 파일:** `migration_core/src/adapters.rs`, `migration_core/src/protocol.rs`, `migration_core/src/dump.rs`, `migration_core/src/import.rs`, `migration_core/src/query.rs`, `migration_core/src/schema.rs`, `migration_core/src/oneclick.rs`, `migration_core/src/migrate.rs`, `migration_core/src/compare.rs`, `migration_core/src/dump_format.rs`, `migration_core/src/ddl.rs`
**테스트:** `migration_core/tests/jsonl_cli.rs`, `migration_core/tests/live_roundtrip.rs`, `migration_core/tests/stress_rss.rs`

**가이드:**
- [CC-254] lib.rs(17,006줄)를 검증된 라인 경계로 11개 모듈에 기계적 이동(move only). 로직/시그니처/본문 절대 변경 금지. 경계: adapters.rs(1-728), protocol.rs(729-1343), dump.rs(1344-3215), import.rs(3216-4453), query.rs(4454-4657), schema.rs(4658-5441), oneclick.rs(5442-7137, oneclick_* 42개), migrate.rs(7138-8790), compare.rs(8791-9146 + 11613-11688 두 구간 병합), dump_format.rs(9147-10098), ddl.rs(10099-11612).
- [CC-254] lib.rs는 얇은 재수출 루트로만 남긴다: `mod adapters; mod protocol; ... mod ddl;` 선언 + 각 모듈에 대해 `pub use crate::<module>::*;`로 flatten 재수출. 이래야 크레이트 루트 공개 경로(`migration_core::handle_request`, `handle_request_streaming`, `CoreService`, `Request`, `Endpoint`, `migrate_with_adapters`, `verify_with_adapters`, `MigrationOptions`, `Normalized*` 등)가 유지되어 main.rs·tests/*.rs를 건드리지 않는다. `pub mod`로 노출해 `migration_core::protocol::handle_request` 형태가 되면 main.rs/통합테스트가 깨지므로 금지.
- [CC-254] 각 모듈 파일 상단에 실제로 쓰는 `use`만 개별 추가(serde/serde_json/sha2/std::*, mysql, postgres). 최상단 공유 use(1-13)는 adapters.rs로 옮기고 나머지 모듈은 필요한 것만 import한다. 미사용 import 경고는 제거.
- [CC-254] 최상단 공유 const(15-28: MYSQL_*, DUMP_DIR_MARKER)와 공유 데이터 모델 struct/enum(Request~InspectionResult, 30-292)은 adapters.rs에 둔다. 이미 pub인 struct는 그대로, private const가 타 모듈에서 참조되면 `pub(crate)`로 승격하고 참조 측은 `use crate::{...}`로 가져온다.
- [CC-254] 핵심 기계 작업: 현재 최상단 private free 함수가 295개(pub 52개). 분할 후 타 모듈에서 호출되는 private 함수/const는 컴파일 에러로 드러난다 — 그때마다 해당 항목만 `pub(crate)`로 승격 + 호출 측에 `use crate::<정의모듈>::<이름>;` 추가. 가시성 키워드와 use 문 외에는 아무것도 바꾸지 않는다.
- [CC-254] impl 블록은 클러스터 내부에 있으므로 그대로 이동: impl MemoryAdapter / MigrationAdapter for MemoryAdapter / impl LiveAdapter / MigrationAdapter for LiveAdapter + trait MigrationAdapter(299) → adapters.rs; impl CoreService(734) + impl Default for CoreService(845) → protocol.rs; impl DumpParallelLimits(1980) → dump.rs.
- [CC-254] 통합테스트 크레이트 추출 금지(검증됨): 테스트가 private 함수(strip_mysql_definer, sanitize_view_definition 등)를 직접 호출한다. 단일 `mod tests`(11689-17006, #[test] 216개)를 각 모듈 파일 내부 `#[cfg(test)] mod tests { use super::*; ... }`로 공치(co-locate) 분할한다.
- [CC-254] 테스트 배치: 각 테스트가 호출하는 private 헬퍼가 정의된 모듈로 보낸다(view-sanitization 테스트 16744-16995 + strip_mysql_definer/sanitize_view_definition → schema.rs; dump-manifest 계열 → dump.rs/dump_format.rs; migrate/verify 테스트 + mod tests 내 mock MigrationAdapter impl 11692-11785 → migrate.rs). 공개 API만 쓰는 테스트는 주제 모듈로. 216개 전부 이동 후 `cargo test` 개수 보존 확인.
- [CC-254] 증분 추출: 한 번에 한 모듈씩 잘라내고 매번 `cargo build`+`cargo test`로 초록 확인 후 다음 모듈 진행. dump.rs(~1870줄)는 이번 라운드는 단일 파일 유지 — 하위 dump/ 분할은 로직 정리를 수반하므로 round-2 WP로 미룬다.
- [CC-254] glob 재수출(`pub use module::*`) 시 서로 다른 모듈의 동일 공개 이름 충돌이 나면 그 항목만 명시적 `pub use module::{name}`으로 좁힌다. private 항목은 glob에 노출되지 않아 캡슐화는 유지된다.
- 하드 제약: 동작 변경 0, 버전 bump 금지, DB 연산은 계속 tunnelforge-core 소유(Python DB 드라이버 hot path 재도입 금지). main.rs와 tests/*.rs는 수정 대상 아님 — 재수출로도 공개 경로가 유지되지 않아 이들을 고쳐야 하는 상황이 오면 즉시 멈추고 재조정 요청.
- [리뷰반영/바인딩 규칙] #[cfg(test)] 각 #[test]는 "테스트 대상 함수를 소유한 모듈"에 공치한다(다운스트림 WP-2.10~2.12의 파일 소유권을 결정론적으로 만들기 위함). 쟁점 블록 명시 배치: adaptive dump-limit 테스트 → dump.rs(dump_parallel_limits와 동거), dump_import_row_progress_event 테스트 → 해당 fn 보유 모듈(import.rs), DumpManifest serde round-trip 쌍 → adapters.rs, insert_rows_literal_sql_for_table 테스트 → ddl.rs, bigint-PK range-dump 테스트 → dump_format.rs. 이 규칙으로 WP-2.10~2.12의 STOP-and-reschedule 경로 제거.

**검증:**
- `cargo build --manifest-path migration_core/Cargo.toml`
- `cargo test --manifest-path migration_core/Cargo.toml`
- `cargo build --manifest-path migration_core/Cargo.toml --release`
- `각 모듈 추출마다 cargo build 재실행(증분 검증); 최종 cargo test 로 216개 unit + 3개 통합테스트 파일(jsonl_cli/live_roundtrip/stress_rss) 통과 및 테스트 개수 보존 확인`

**리스크:**
- pub(crate) 승격 fan-out: 최상단 private free 함수 295개 중 클러스터 경계를 넘어 호출되는 것들을 컴파일 에러로 하나씩 찾아 pub(crate)로 올려야 함 — 사전 완전 열거 불가, 컴파일러 주도 반복 필요(가시성/‘use’만 추가, 로직 불변).
- 공개 API 경로 보존이 실패하면(예: 어떤 pub 항목이 flatten 재수출에 누락) main.rs(use migration_core::{handle_request_streaming, CoreService, Request})와 tests/live_roundtrip.rs·tests/stress_rss.rs가 컴파일 실패 → 이 파일들은 files_touched 밖이므로 수정 시 즉시 STOP+재조정 대상. lib.rs의 `pub use module::*`로 반드시 크레이트 루트 평탄화 유지.
- 216개 unit 테스트 분할이 최대 난도: 테스트를 잘못된 모듈에 배치하면 그 모듈에 없는 private 헬퍼 참조로 컴파일 실패. strip_mysql_definer/sanitize_view_definition(schema.rs), mock MigrationAdapter impl(11692-11785, migrate.rs) 등이 배치 제약.
- glob 재수출 시 모듈 간 동일 공개 이름 충돌 가능성(347개 free fn 규모). 충돌 시 명시적 재수출로 좁혀야 함.
- 라운드 의존: WP-2.10~2.13(Rust per-module cleanup)이 본 WP가 만드는 새 모듈 파일(dump.rs 등)에 의존 — 반드시 WP-1.8 머지 후 진행. 같은-라운드(round 1)에서 migration_core를 건드리는 다른 WP는 없어 파일 충돌 없음(WP-1.8 단독 소유).
- Windows에서 mysql/postgres crate 빌드 필요 — 기존 lib.rs가 이미 빌드되므로 신규 툴체인 이슈는 없어야 하나, 분할 중 중간 상태에서 unresolved import로 빌드가 깨질 수 있어 증분 커밋 전 반드시 초록 확인.

---

## Round 2 — Core 로직 정리 + Rust 모듈별 정리

### WP-2.1 — analyzer-decomposition
**Branch:** `refactor/cc-r2-migration-analyzer-decomposition` · **Size:** L · **발견:** 11건 (H1/M4/L6) · **의존:** WP-1.5

**Findings covered:** CC-050, CC-052, CC-053, CC-054, CC-055, CC-056, CC-057, CC-058, CC-059, CC-071, CC-080

**수정 파일:** `src/core/migration_analyzer.py`, `src/core/migration_fix_generator.py`, `tests/test_migration_analyzer.py`, `tests/test_migration_mapping_coverage.py`, `tests/test_migration_fix_generator.py`
**신규 파일:** `src/core/migration_analysis_models.py`, `src/core/migration_fk_analyzer.py`, `src/core/migration_compat_checker.py`, `src/core/migration_cleanup_planner.py`
**테스트:** `tests/test_migration_analyzer.py`, `tests/test_migration_mapping_coverage.py`, `tests/test_migration_fix_generator.py (to be deleted with FixQueryGenerator)`

**가이드:**
- [CC-050] MigrationAnalyzer God-class를 4개 협력 모듈로 분해하되 MigrationAnalyzer는 얇은 파사드로 남긴다. 새 모듈: (a) src/core/migration_analysis_models.py — ActionType/OrphanRecord/ForeignKeyInfo/CleanupAction/AnalysisResult(+ to_dict/from_dict) 데이터클래스를 그대로 이동, (b) src/core/migration_fk_analyzer.py — ForeignKeyAnalyzer(get_foreign_keys, build_fk_tree, _get_table_row_count, find_orphan_records, get_fk_visualization), (c) src/core/migration_compat_checker.py — MySQLUpgradeCompatibilityChecker(14개 check_* 전부 + NEW_RESERVED_KEYWORDS/DEPRECATED_FUNCTIONS 상수), (d) src/core/migration_cleanup_planner.py — OrphanCleanupPlanner(generate_cleanup_sql, execute_cleanup). analyze_schema는 오케스트레이션만 남긴다.
- [CC-050] 순환 import 방지(god-file 분해 사고 이력): 협력 모듈은 데이터클래스를 migration_analysis_models.py에서만 import하고 migration_analyzer.py에서는 절대 import하지 않는다. migration_analyzer.py는 models+협력 모듈을 import하고, 하위호환을 위해 MigrationAnalyzer/AnalysisResult/OrphanRecord/CleanupAction/ActionType/ForeignKeyInfo/DumpFileAnalyzer/DumpAnalysisResult를 모듈 최상위에서 반드시 re-export한다. src/core/__init__.py:6과 tests/test_migration_analyzer.py:14의 import가 이에 의존하며 __init__.py는 절대 수정 금지.
- [CC-050] 파사드 MigrationAnalyzer는 __init__에서 connector와 공유 self._log를 각 협력 객체에 주입해 구성하고, 기존 public 메서드(check_* 13종, get_foreign_keys, build_fk_tree, find_orphan_records, get_fk_visualization, generate_cleanup_sql, execute_cleanup)를 협력 객체로 위임하는 얇은 래퍼로 남긴다. test_migration_analyzer.py가 이 메서드들을 인스턴스에서 직접 호출하므로 동작 보존을 위해 위임 래퍼 유지가 필수(해당 테스트 무수정).
- [CC-053] analyze_schema의 public 시그니처(schema + 15개 check_* 불리언, 기본값 모두 True — 특히 check_int_display_width 기본 True 유지)는 절대 변경 금지. Round3의 migration_worker.py/migration_dialogs.py가 키워드 인자로 호출한다. 대신 SchemaCheckOptions 데이터클래스(models 모듈)를 도입해 analyze_schema가 15 kwargs를 options로 묶어 _analyze_schema_impl(schema, options)로 단 한 번 전달, 두 메서드 간 15-인자 verbatim 재선언/일대일 pass-through 중복만 제거한다. tests/test_migration_analyzer.py의 _analyze_schema_impl 직접호출 2곳(라인 431/443, _pipeline_kwargs 헬퍼)만 SchemaCheckOptions를 넘기도록 갱신한다.
- [CC-054] find_orphan_records의 NOT EXISTS(대용량)/LEFT JOIN(일반) 분기가 count 쿼리와 sample 쿼리에 중복된 것을 ForeignKeyAnalyzer 내부 private 헬퍼 _build_orphan_query(schema, fk, is_large, select_expr, limit=None)로 추출해 count/sample 두 형태를 한 곳에서 생성한다. 생성되는 SQL 텍스트는 기존과 바이트 동일하게 유지(백틱/컬럼식/DISTINCT/LIMIT 위치 보존).
- [CC-052] migration_compat_checker.py에서 컬럼 스캔형 8개 체크(check_zerofill_columns, check_float_precision, check_fk_name_length, check_year2_type, check_deprecated_engines, check_enum_empty_value, check_timestamp_range, check_int_display_width)의 log→단일 INFORMATION_SCHEMA 쿼리→행 루프→요약 log 보일러플레이트를 선언형 CheckSpec(query, issue_type, severity, describe_fn/suggest_fn 등) + 단일 _run_column_scan(schema, spec) 헬퍼로 통합한다. 형태가 다른 나머지 체크(charset/reserved_keywords/routines/sql_modes/auth_plugins/invalid_date)는 억지로 합치지 말고 그대로 둔다. 각 CompatibilityIssue의 location/description/suggestion/severity/table_name/column_name/fix_query 산출값은 기존과 완전히 동일해야 한다(동작 보존).
- [CC-059] _analyze_schema_impl의 하드코딩 '[N/15]' 스텝 카운터 15쌍을 (flag, log_message, check_callable) 선언형 리스트 + enumerate로 대체해 번호/총계가 자동 계산되게 한다. 고아 탐지+cleanup 생성 흐름(check_orphans)은 별도 처리이므로, 최종 로그 문구가 기존 문자열과 동일하게 출력되도록 맞춘다(로그 문구도 동작으로 간주).
- [CC-058] find_orphan_records의 두 임계값을 migration_fk_analyzer.py 모듈 최상위 상수 LARGE_TABLE_ROW_THRESHOLD = 500_000, SIZE_INFO_LOG_THRESHOLD = 100_000로 명명해 인라인 매직넘버(기본값 500000, 283행 100000)를 대체한다. finding의 'migration_constants.py에 추가' 제안 대신 로컬 상수로 두어 공유 모듈(타 WP 영역) 수정을 회피한다 — 값은 불변.
- [CC-055] execute_cleanup(OrphanCleanupPlanner로 이동)에서 `if not dry_run: raise RuntimeError(...)` 직후의 중복 `if dry_run:` 래퍼를 제거하고 본문을 한 단계 dedent한다. fail-closed raise(dry_run=False → 항상 RuntimeError, DB 변경은 Rust Core 소유)는 load-bearing이므로 반드시 유지하고, docstring에 'dry_run=False는 항상 RuntimeError를 던진다'를 명시한다. execute_cleanup(action, dry_run=True) 시그니처/반환 튜플(bool,str,int)은 불변(test_migration_analyzer.py:606 회귀 테스트가 raise를 검증).
- [CC-056][CC-057] 스윕(순수 삭제, 동작 영향 없음): migration_analyzer.py 13행 미사용 `from pathlib import Path` 삭제; analyze_schema 682행의 중복 `from datetime import datetime` 삭제(impl쪽 것만 유지 또는 모듈 스코프로 이동); 파일 끝(1280-1285)의 이관된 DumpFileAnalyzer용 stale 섹션 헤더 주석 삭제.
- [CC-071] FixQueryGenerator는 src/ 내 프로덕션 호출자 0개이고 fix_query가 src/ui/에서 읽히지 않음(검증됨). SmartFixGenerator와의 통합은 SQL 산출물이 달라 동작 보존이 불가하므로 dead code로 간주해 src/core/migration_fix_generator.py 전체와 tests/test_migration_fix_generator.py를 삭제한다. tests/test_migration_mapping_coverage.py에서는 FixQueryGenerator import·TestFixQueryGeneratorCoverage 클래스·TestEndToEndFlow 내 FixQueryGenerator 단계만 제거하고 SmartFixGenerator 커버리지는 유지한다(migration_fix_wizard.py는 절대 수정 금지). 삭제 전 반드시 `FixQueryGenerator`를 src/ 전역에 재-grep; 프로덕션 호출자가 하나라도 나오면 삭제를 포기하고 CC-071을 deferred로 기록한 뒤 아래 CC-080 폴백만 적용한다.
- [CC-080] 폴백(위 CC-071 삭제가 실행되면 파일과 함께 자동 해소됨): 만약 caller 발견으로 파일을 유지하기로 결정한 경우에만, _gen_removed_sysvar_fix의 182행 지역 `import re`를 제거한다(7행 모듈 스코프 import가 이미 커버).
- [리뷰반영/사전조건] WP-1.5는 migration_analyzer.py에서 NEW_RESERVED_KEYWORDS 속성 제거 + check_reserved_keywords를 모듈 레벨 ALL_RESERVED_KEYWORDS로 재배선만 한다(클래스 분할 아님). 따라서 본 WP는 라운드1 머지된 migration_analyzer.py(이미 NEW_RESERVED_KEYWORDS 제거, ALL_RESERVED_KEYWORDS가 단일 소스) 위에서 rebase만 하면 됨 — 재스케줄 불필요. depends_on WP-1.5는 라운드 순서로 이미 충족.

**검증:**
- `python -m py_compile src/core/migration_analyzer.py src/core/migration_analysis_models.py src/core/migration_fk_analyzer.py src/core/migration_compat_checker.py src/core/migration_cleanup_planner.py`
- `python -c "from src.core import MigrationAnalyzer, AnalysisResult, OrphanRecord, CleanupAction, ActionType"`
- `python -c "from src.core.migration_analyzer import MigrationAnalyzer, DumpFileAnalyzer, AnalysisResult, DumpAnalysisResult, OrphanRecord, CleanupAction, ActionType, ForeignKeyInfo"`
- `python -m pytest tests/test_migration_analyzer.py tests/test_migration_mapping_coverage.py -q`
- `python -m pytest -q`

**리스크:**
- src/core/__init__.py:6 은 migration_analyzer 에서 MigrationAnalyzer/AnalysisResult/OrphanRecord/CleanupAction/ActionType 를 import 한다. __init__.py 는 이 WP 범위 밖이므로 수정 금지 — 분해 후에도 이 5개(+ForeignKeyInfo/DumpFileAnalyzer/DumpAnalysisResult)가 migration_analyzer.py 최상위에서 반드시 import 가능해야 한다(re-export). 누락 시 앱 전역 import 실패.
- consumers migration_worker.py(라인 59/62/128)와 migration_dialogs.py(라인 966/972/1091/1098) 는 Round3 UI 영역이라 이 WP 가 건드리면 안 된다. MigrationAnalyzer(connector), set_progress_callback, analyze_schema(schema, check_*=...), execute_cleanup(action, dry_run=True), generate_cleanup_sql(orphan, action, schema, dry_run=...) 의 public 시그니처를 100% 보존해야 한다. 특히 migration_worker 는 check_int_display_width 를 넘기지 않으므로 기본값 True 유지 필수.
- tests/test_migration_analyzer.py 는 check_* 13종과 _analyze_schema_impl(**kwargs) 를 인스턴스에서 직접 호출한다. 파사드 위임 래퍼를 유지해 이 테스트는 무수정으로 통과시키되, CC-053 때문에 _analyze_schema_impl 직접호출 2곳(431/443)만 SchemaCheckOptions 로 갱신 — 그 외 테스트 시그니처는 바꾸지 말 것.
- CC-071 삭제는 비가역적이다. FixQueryGenerator 는 현재 프로덕션 호출자 0개로 검증됐으나, 삭제 실행 전 src/ 전역 재-grep 을 hard precondition 으로 둔다. 호출자가 나오면 삭제 포기 + CC-071 deferred 처리(CC-080 폴백만 적용). test_migration_mapping_coverage.py 는 SmartFixGenerator(migration_fix_wizard.py)도 테스트하므로 FixQueryGenerator 부분만 정확히 도려내야 하며 migration_fix_wizard.py 는 절대 건드리지 않는다.
- 순환 import 위험(과거 god-file 분해 사고): 새 협력 모듈이 데이터클래스를 migration_analyzer.py 에서 import 하면 순환이 발생한다. 반드시 migration_analysis_models.py 에서만 import 하도록 배선한다. 조용한 NameError 방지를 위해 분해 후 위 import 스모크 3종을 먼저 돌린다.
- CC-058 은 finding 의 문자 그대로 migration_constants.py 에 상수를 추가하는 대신 로컬(FK analyzer) 상수로 배치한다. 이는 file-disjoint 실행에서 공유 모듈 충돌을 피하기 위한 의도적 편차이며 값(500000/100000)은 불변 — 동작 보존.
- check_float_precision/check_int_display_width 등의 INFORMATION_SCHEMA 쿼리는 %% 및 백슬래시 REGEXP 이스케이프가 많다. 협력 모듈로 옮길 때 쿼리 문자열을 바이트 동일하게 보존하지 않으면 탐지 동작이 깨진다(예: 파라미터 %s + '%%ZEROFILL%%').
- 이 WP 는 migration_analyzer.py 를 God-class 분해하는 유일한 WP 라는 전제(Round1 mechanical split 이 이 파일을 선-분해하지 않음)로 depends_on=[] 로 둔다. 만약 Round1 WP 가 migration_analyzer.py 를 이미 분해했다면 same-file 충돌이므로 즉시 리스케줄 필요.

### WP-2.2 — fix-wizard-split
**Branch:** `refactor/cc-r2-migration-fix-wizard-split` · **Size:** L · **발견:** 11건 (H2/M6/L3)

**Findings covered:** CC-065, CC-066, CC-067, CC-068, CC-069, CC-070, CC-072, CC-073, CC-074, CC-075, CC-081

**수정 파일:** `src/core/migration_fix_wizard.py`, `src/core/migration_fix_models.py`, `src/core/migration_rollback_sql_generator.py`, `tests/test_migration_fix_wizard.py`
**신규 파일:** `src/core/migration_fix_option_generator.py`, `src/core/migration_fk_graph.py`, `src/core/migration_fk_safe_charset.py`, `src/core/migration_batch_fix_executor.py`, `src/core/migration_charset_fix_plan.py`
**테스트:** `tests/test_migration_fix_wizard.py`, `tests/test_migration_mapping_coverage.py`, `tests/test_fix_wizard_dialog.py`, `tests/test_current_status_docs.py`

**가이드:**
- [CC-065] migration_fix_wizard.py(1472줄)를 5개 신규 모듈로 물리 분할한다: SmartFixGenerator+create_wizard_steps→migration_fix_option_generator.py, CollationFKGraphBuilder→migration_fk_graph.py, FKSafeCharsetChanger→migration_fk_safe_charset.py, BatchFixExecutor→migration_batch_fix_executor.py, CharsetFixPlanBuilder→migration_charset_fix_plan.py. 원본 migration_fix_wizard.py는 얇은 facade로 남겨, 소비자가 쓰는 모든 이름을 re-export한다(FixStrategy, FKDefinition, FixOption, FixWizardStep, FixExecutionResult, BatchExecutionResult, CharsetTableInfo, RollbackSQLGenerator, SmartFixGenerator, CollationFKGraphBuilder, FKSafeCharsetChanger, BatchFixExecutor, CharsetFixPlanBuilder, create_wizard_steps). __all__을 명시해 누락 방지. 소비자 9곳(fix_wizard_worker.py + 6개 dialog + test_migration_fix_wizard.py + test_migration_mapping_coverage.py)은 절대 수정하지 않는다(import 경로 유지가 목표).
- [CC-065] 신규 모듈 간 import DAG는 순환 없이 구성한다. migration_fk_graph.py가 leaf(모델+connector만 import). migration_fk_safe_charset는 fk_graph를 import. option_generator/batch_fix_executor/charset_fix_plan은 fk_graph와 fk_safe_charset을 모두 import(각각 FKSafeCharsetChanger를 직접 생성하므로 반드시 import 포함). 분할 후 각 파일에 대해 미정의 이름/미사용 import 체크(AST 또는 py_compile)를 돌려 조용한 NameError를 차단한다(MEMORY god-file split pitfalls).
- [CC-068] 4개 클래스에 verbatim 중복된 lazy-init(CollationFKGraphBuilder(...) 생성 + build_graph())을 migration_fk_graph.py에 module-level 헬퍼 build_fk_graph(connector, schema)->CollationFKGraphBuilder로 추출한다. 각 클래스의 _get_fk_graph_builder/get_fk_graph_builder는 per-instance 캐시 가드(if self._fk_graph_builder is None)만 유지하고 생성 부분만 build_fk_graph 호출로 교체. 4개 모듈 모두 fk_graph에서 이 헬퍼를 import.
- [CC-066][CC-067][CC-074] migration_batch_fix_executor.py 내부에서 execute_batch(~254줄)를 3개 private 헬퍼로 분해: _execute_fk_safe_clusters(steps)->(List[FixExecutionResult], Set[int]), _execute_collation_single_merges(steps)->(List[FixExecutionResult], Set[int]), _execute_remaining_steps(steps, already_handled_ids)->List[FixExecutionResult]. execute_batch는 순차 호출 후 결과 concat + 집계(success/fail/skip/total_affected) 계산만 담당. dry_run=False raise 가드는 그대로 유지. [CC-067] 항상 빈 rollback_sql 지역변수는 제거하고 BatchExecutionResult(..., rollback_sql="")로 리터럴 인라인(테스트의 r.rollback_sql=="" 계약 유지). _execute_single은 삭제 금지(테스트 test_private_single_execution_hook_is_fail_closed + docs가 의도된 fail-closed 가드로 문서화) — docstring에 '(defense-in-depth: bypassing execute_batch guard여도 fail-closed)'만 보강. [CC-074] BatchFixExecutor 클래스 docstring을 dry-run/추정 전용으로 재작성하고 '트랜잭션 실행'·'FOREIGN_KEY_CHECKS=0 전체 감싸기' 문구 제거(실제 실행은 Rust Core 소유, FK_CHECKS 래핑은 SmartFixGenerator에 존재).
- [CC-075] migration_batch_fix_executor.py에 module-level logger(logging.getLogger(__name__)) 추가. _sort_steps_by_fk_order의 broad except와 _estimate_affected_rows의 sql_mode save/restore except는 fallback 계약 보존을 위해 broad catch 자체는 유지하되 logging.exception/warning으로 진단 로그를 남긴다(예외 전파 대상 변경 금지 = 동작 보존). 특히 finally의 sql_mode 복원 실패 시 warning 로그를 반드시 남겨 세션 모드가 조용히 완화된 채 후속 dry-run이 진행되는 상황을 감지 가능하게 한다.
- [CC-069] migration_fix_option_generator.py의 _get_invalid_date_options에서 3회 반복되는 invalid-date WHERE 절을 private 헬퍼 _invalid_date_where_clause(column: str)->str로 추출하고, DATE_TO_NULL/DATE_TO_MIN/DATE_TO_CUSTOM 3개 sql_template을 이 헬퍼로 조립(문자열 출력은 기존과 바이트 단위 동일해야 함).
- [CC-072] migration_fix_models.py에 DEFAULT_TARGET_CHARSET="utf8mb4", DEFAULT_TARGET_COLLATION="utf8mb4_unicode_ci" 상수 1회 정의. 분할된 wizard-domain 모듈들(option_generator, fk_safe_charset, batch_fix_executor, charset_fix_plan)의 8+ 리터럴을 이 상수로 교체하고, generate_safe_charset_sql/execute_safe_charset_change/generate_fix_sql의 기본 파라미터 값도 상수로 지정. ⛔ src/core/migration_fix_generator.py(라인 76,161)는 이 WP 범위 밖(다른 파일) — 절대 편집하지 말 것. 값이 동일하므로 남겨두어도 동작 보존.
- [CC-070] INFORMATION_SCHEMA.TABLES+COLLATION join 조회를 migration_fix_models.py에 get_table_charset(connector, schema, table)->Tuple[str,str] 공유 함수로 추출(_format_default_sql_clause 옆). CharsetFixPlanBuilder._get_table_charset는 이 함수에 위임, RollbackSQLGenerator.capture_table_charset은 이 함수를 호출한 뒤 자신의 dict 캐시로 래핑. fallback 기본값('utf8mb3'/'utf8mb3_general_ci')은 그대로. 이 조회는 read-only이며 mutation 경로 아님(Rust-core 백엔드 connector shim 재사용).
- [CC-073] migration_fix_models.py FixWizardStep에서 한 번도 대입되지 않는 included_by/included_reason 필드를 제거하고, migration_rollback_sql_generator.py generate_batch_rollback의 도달 불가 분기(if step.included_by is not None: continue, 371-373)를 삭제한다(중복 롤백은 기존 processed_tables 테이블-레벨 dedup가 이미 방지). 이에 맞춰 tests/test_migration_fix_wizard.py의 obsolete assert 2줄(step.included_by is None / step.included_reason == "")을 삭제. grep 확인 결과 included_by/included_reason를 kwargs로 생성하는 코드는 없어 안전.
- [CC-081] migration_rollback_sql_generator.py generate_rollback_sql(~146줄)을 전략별 private 헬퍼로 추출: _rollback_date(step), _rollback_collation_single(step, original_state), _rollback_collation_fk(step, original_state, all_pre_states). 각 헬퍼가 자체 SQL 문자열 반환. generate_rollback_sql은 location 파싱 preamble + 전략 dispatch만 담당. 기존 주석/라인 출력 문자열을 정확히 보존해 rollback SQL 테스트(766~973행 다수)가 그대로 통과하도록 한다.
- [HARD CONSTRAINTS] 전 항목 동작 보존 리팩터만 수행: public 시그니처/반환 형태/출력 문자열 유지, 버전 bump 금지, DB mutation 경로 재도입 금지(dry_run=False는 계속 RuntimeError raise, DB 실행은 Rust Core 소유). 소비자 dialog/worker 파일은 facade re-export로 무변경 유지. 만약 어떤 수정이 files_touched/new_files 밖 파일(UI dialog, migration_fix_generator.py 등) 편집을 요구하면 즉시 중단하고 재스케줄 요청 — 본 가이드는 그런 상황이 발생하지 않도록 내부 변경 + facade re-export로 설계됨.
- [리뷰반영] 검증/테스트 목록에서 tests/test_migration_fix_generator.py 제거 — 이 파일은 WP-2.1이 삭제 소유. 머지 순서상 WP-2.2를 WP-2.1보다 먼저 머지하여 본 WP worktree 실행 중에는 해당 파일이 아직 존재하므로 자체 스위트는 정상 통과.

**검증:**
- `python -m py_compile src/core/migration_fix_wizard.py src/core/migration_fix_models.py src/core/migration_rollback_sql_generator.py src/core/migration_fix_option_generator.py src/core/migration_fk_graph.py src/core/migration_fk_safe_charset.py src/core/migration_batch_fix_executor.py src/core/migration_charset_fix_plan.py`
- `python -m pytest`

**리스크:**
- migration_fix_models.py는 공유 모듈인데 이 WP가 편집한다(CC-070 공유 get_table_charset, CC-072 상수, CC-073 dead field 제거). 다른 round-2 WP가 동일 파일을 편집하면 금지된 same-round overlap — 반드시 직렬화 필요. 다만 이 파일은 migration-fix-wizard 도메인 전용이라 본 WP 소유가 자연스러움.
- migration_fix_wizard.py에는 9개 소비자 import 사이트가 있다(src/ui/workers/fix_wizard_worker.py, src/ui/dialogs/fix_wizard_charset_page.py/dialog.py/execution_page.py/issue_selection_page.py/option_page.py/preview_page.py, tests/test_migration_fix_wizard.py, tests/test_migration_mapping_coverage.py). facade가 전체 이름 집합을 re-export하지 못하면 다수 소비자가 ImportError로 깨진다 — __all__ 완결성 필수.
- CC-072 대상 리터럴이 src/core/migration_fix_generator.py(라인 76,161)에도 존재하나 이 파일은 본 WP 범위 밖이므로 의도적으로 미편집. 값이 동일해 동작 보존이며, 편집 시 다른 WP와 cross-file overlap 위험.
- CC-070으로 read-only INFORMATION_SCHEMA 조회 함수가 pure-ish였던 migration_fix_models.py에 들어간다. 기존 동작(Rust-core 백엔드 connector shim)의 단순 재배치라 허용되지만, 절대 mutation 경로로 확장하지 말 것(프로젝트 규칙: DB 작업은 tunnelforge-core 소유).
- CC-075에서 except 타입을 좁히면 지금은 삼켜지던 예외가 전파되어 동작이 바뀔 수 있음 → 동작 보존을 위해 broad catch 유지 + 로깅만 추가 권장.
- god-file split 함정(MEMORY): 순환 import/조용한 NameError 주의. 테스트는 executor 인스턴스에 patch.object(_execute_single)만 사용하고 src.core.migration_fix_wizard.<Class>를 module-level로 monkeypatch하지 않음(grep 확인) — 따라서 실제 사용처가 신규 모듈로 이동해도 patch가 조용히 무력화되는 문제는 없음.
- docs/current_status.md와 tests/test_current_status_docs.py가 'migration_fix_wizard.py' 및 BatchFixExecutor._execute_single을 참조 — facade 파일 잔존 + 클래스/메서드 이름 보존으로 해당 테스트는 green 유지되어야 함(회귀 확인 필수).

### WP-2.3 — rules-parsers-cleanup
**Branch:** `refactor/cc-r2-migration-rules-parsers-cleanup` · **Size:** L · **발견:** 17건 (H2/M12/L3)

**Findings covered:** CC-060, CC-061, CC-064, CC-082, CC-083, CC-084, CC-085, CC-086, CC-087, CC-088, CC-089, CC-090, CC-091, CC-092, CC-093, CC-094, CC-095

**수정 파일:** `src/core/migration_dump_analyzer.py`, `src/core/migration_parsers.py`, `src/core/migration_rules/data_rules.py`, `src/core/migration_rules/schema_rules.py`, `src/core/migration_rules/storage_rules.py`, `tests/test_migration_rules.py`, `tests/test_migration_parsers.py`
**신규 파일:** `src/core/migration_rules/_base.py`, `src/core/migration_rules/identifier_rules.py`, `src/core/migration_rules/index_charset_rules.py`, `src/core/migration_rules/definer_rules.py`, `src/core/migration_rules/syntax_rules.py`
**테스트:** `tests/test_migration_rules.py`, `tests/test_migration_parsers.py`, `tests/test_migration_analyzer.py`, `tests/test_migration_constants.py`

**가이드:**
- [CC-091] 신규 src/core/migration_rules/_base.py 에 ProgressLoggingRuleBase 추가: 세 클래스에서 바이트 단위로 동일한 __init__/set_progress_callback/_log 을 이 베이스로 올리고, 'if issues: warn else: success' 요약 로그 패턴을 _log_summary(self, issues, item_label) 로 캡슐화. SchemaRules/StorageRules/DataIntegrityRules 가 이 베이스를 상속하도록 변경(data_rules.py/schema_rules.py/storage_rules.py 3파일). migration_rules/__init__.py 의 재export 3줄은 절대 수정 금지(import 경로 유지).
- [CC-090] schema_rules.py 7군데(123,318,522,689,712,736,759)에 복붙된 3줄 '매치 주변 소스라인 추출' 스니펫(line_start=rfind... / line_end=find... / line=content[...].strip())을 _base.py 의 ProgressLoggingRuleBase._extract_source_line(content, match) 로 한 번만 정의하고 7개 호출부를 교체. 반환/슬라이싱(line[:80] 등)은 기존과 동일해야 함(동작 보존).
- [CC-083][CC-093] data_rules.py 의 수제 SQL 토크나이저 4메서드(_find_statement_end/_iter_create_table_statements/_iter_values_rows/_split_sql_values, 166-342)를 migration_parsers.py 로 이동해 SqlStatementScanner 클래스로 만들고 CreateTableParser 근처에 배치(import 방향 parsers ← data_rules 유지, 순환 없음; 이 4메서드를 직접 호출하는 테스트 없음 확인됨 → 델리게이터 불필요). 이어서 CC-093: check_enum_numeric_index(344-414)의 5중 중첩을 _find_numeric_enum_value(values, enum_col_indices, cols)->Optional[Tuple[str,str]] 헬퍼로 최내부 루프를 빼내고 found_in_current_insert 불리언+이중 break 를 제거해 2-3레벨로 축소. 이슈 개수/텍스트 불변.
- [CC-086] check_latin1_non_ascii(726-821)와 check_zerofill_data_dependency(826-943)의 공통 골격을 _batch_scan_columns(self, schema, columns, *, build_query, issue_type, severity, describe) private 헬퍼로 추출: partial-scan 상한 / groupby+_table_key 그룹핑 / 테이블별 배치쿼리 try-except / 마지막 partial-scan info 이슈를 헬퍼가 소유. 각 check 는 컬럼선택 쿼리와 per-컬럼 SQL조각/설명 콜백만 제공. 중복 로컬 'from itertools import groupby'(759,873)는 파일 상단 모듈 import 로 승격.
- [CC-087] check_4byte_utf8_in_data(542)/check_null_byte_in_data(600)/check_timestamp_range(654)/check_invalid_datetime(948)의 read-loop/truncation/SCAN_TRUNCATED 보일러플레이트를 _scan_file_lines(self, file_path, mode, per_line_check) 템플릿 메서드로 추출. 로컬 max_lines=10000/max_samples=3 을 클래스 상수 _MAX_SCAN_LINES=10000/_MAX_SAMPLE_VALUES=3 로 승격(_MAX_COLUMNS_TO_CHECK 관례와 통일). 각 check 는 per-line 판정/이슈생성 콜백만 제공.
- [CC-088] (동작 변화 주의 — 순수 리팩터 아님) check_invalid_datetime except 블록(1009-1011)에 형제 3함수와 동일하게 info severity CompatibilityIssue(issue_type=IssueType.INVALID_DATE, description=f"DATETIME 스캔 미완료: {str(e)[:80]}")를 append 후 return. tests/test_migration_rules.py 에 파일읽기 실패(존재하지 않는/디렉토리 경로 등) 전용 테스트를 신규 추가해 info 이슈 1건 반환을 검증.
- [CC-064] migration_parsers.py 의 extract_create_table_statements/extract_create_user_statements/extract_grant_statements(738-781)를 _extract_statements(self, pattern, content) 단일 헬퍼 + 클래스레벨 precompiled 상수 _CREATE_TABLE_STMT_PATTERN/_CREATE_USER_STMT_PATTERN/_GRANT_STMT_PATTERN 로 리팩터. 세 public 메서드 시그니처/반환은 그대로 유지(test_migration_parsers.py 가 직접 호출). CC-083 과 동일 파일이므로 편집 순서 조율(먼저 SqlStatementScanner 추가 후 이 리팩터).
- [CC-060][CC-061] migration_dump_analyzer.py _analyze_sql_file(130-285)의 8개 검사(ZEROFILL/FLOAT/FK-name/auth-plugin/FTS_/SUPER/removed-sysvar/reserved-keyword)를 각각 _check_*(self, content, file_name)->List[CompatibilityIssue] private 헬퍼로 분리하고 _analyze_sql_file 은 파일읽기+100MB 가드레일+각 헬퍼 호출+concat 만 담당(바깥 try/except 유지). CC-061: 인라인 table_pattern(249)/column_pattern(253)을 파일 상단 모듈레벨 precompiled 상수(예 _CREATE_TABLE_NAME_PATTERN/_TYPED_COLUMN_NAME_PATTERN)로 승격 — migration_constants.py 는 이 WP 범위 밖이므로 상수를 dump analyzer 파일 내부에 둔다.
- [CC-082] storage_rules.py 의 미사용 import INVALID_ENGINE_FK_PATTERN(17-23 import 블록에서 해당 이름만) 삭제. check_invalid_engine_fk(196-223)의 로컬 create_table_pattern(테이블명 캡처 그룹 필요)은 그대로 유지 — 공유 상수는 테이블명 캡처가 없어 drop-in 이 아니고 constants 확장은 범위 밖. 데드 import 제거만으로 finding 해소(migration_constants.py 의 정의는 건드리지 않음).
- [CC-089] SchemaRules(61-850, 29메서드)를 믹스인 분할로 정리: 신규 identifier_rules.py(S07-S09,S27,S30,S31), index_charset_rules.py(S02-S04 + calculate_column_byte_size), definer_rules.py(S23-S25 + _fetch_existing_definers_or_issue), syntax_rules.py(S05,S06,S16-S18,S28,S29 + _matches_sql_function_call) 각각에 *RulesMixin 클래스를 만들고 메서드 본문을 그대로 이동. schema_rules.py 의 SchemaRules 는 이 믹스인들 + ProgressLoggingRuleBase 를 상속하는 얇은 클래스로 남겨 모든 메서드(check_all_live_db/check_all_sql_content 및 테스트가 직접 호출하는 calculate_column_byte_size 등)가 인스턴스에 그대로 노출되게 함(MRO 로 cross-family self 호출도 자동 해소, 동작·테스트 불변). 반드시 CC-091/CC-090 이후 마지막에 수행하고, 이동 후 라인번호가 전면 변동하므로 남은 편집은 재탐색해서 적용.
- [CC-084][CC-085][CC-092] docstring/주석만 수정(동작 무관, 참조 테스트 없음 확인): data_rules.py 모듈 docstring 에서 D12/D13 제거하고 '13개 규칙 구현'->'11개 규칙 구현(D01-D11)'; schema_rules.py docstring 에서 S19-S22 제거하고 '36개 규칙 구현'->실제 구현분(S01-S09,S16-S18,S23-S31 = 22개 checks)로 정정; storage_rules.py 의 중복 'S16'(11행 docstring + 194행 섹션주석)을 미사용 ID 'S32' 로 리넘버링(schema_rules 의 생성컬럼함수 S16 은 유지).
- [CC-094][CC-095] LOW sweep: data_rules.py:22 미사용 'from dataclasses import dataclass' import 삭제(CC-094); schema_rules.py calculate_column_byte_size 의 DECIMAL 하드코딩 16(226-229)을 클래스 상수 _DECIMAL_ESTIMATED_MAX_BYTES = 16 로 승격하고 짧은 주석(MySQL DECIMAL 저장 바이트 근사) 추가 — 반환값 불변, index_charset 믹스인으로 메서드 이동 시 상수도 함께 이동, migration_constants.py 로 옮기지 않음.

**검증:**
- `python -m py_compile src/core/migration_dump_analyzer.py src/core/migration_parsers.py src/core/migration_rules/__init__.py src/core/migration_rules/_base.py src/core/migration_rules/data_rules.py src/core/migration_rules/schema_rules.py src/core/migration_rules/storage_rules.py src/core/migration_rules/identifier_rules.py src/core/migration_rules/index_charset_rules.py src/core/migration_rules/definer_rules.py src/core/migration_rules/syntax_rules.py`
- `python -m pytest tests/test_migration_rules.py tests/test_migration_parsers.py tests/test_migration_analyzer.py tests/test_migration_constants.py -q`
- `python -m pytest -q`

**리스크:**
- migration_constants.py 는 이 WP 파일셋에 없음. CC-061/CC-082(옵션b)/CC-095 의 '상수를 migration_constants.py 로 중앙화' 권고는 파일-분리 유지를 위해 소유 파일 내부(dump analyzer 모듈 상수 / storage 데드 import 삭제 / SchemaRules 클래스 상수)로 대체 이행. 만약 다른 WP 가 migration_constants.py 를 소유해 중앙화가 필요하면 그 이상은 후속으로 미룸 — 실행 에이전트는 migration_constants.py 를 절대 편집하지 말 것(범위 이탈 시 리스케줄 요청).
- CC-088 은 순수 리팩터가 아니라 에러 경로 동작 변경(파일읽기 실패 시 info 이슈 +1 방출)이다. 파일 스캔 실패 리포트 카운트가 바뀌므로 전용 테스트를 반드시 추가하고, 다른 finding 의 '이슈 개수 불변' 검증과 구분할 것.
- CC-089(god-class 믹스인 분할)가 최대·최고위험 항목. 믹스인 방식이면 SchemaRules 인스턴스에 전 메서드가 그대로 남아 MRO 로 cross-family self 호출/테스트가 보존되지만, schema_rules.py 는 5개 finding(CC-085/089/090/091/095)이 겹쳐 편집돼 라인번호가 전면 변동한다. 반드시 순서 준수: (1) CC-091 _base.py -> (2) CC-090 _extract_source_line 를 base 로 -> (3) CC-095 상수/CC-085 docstring -> (4) 마지막에 CC-089 믹스인 이동. 각 단계 후 라인기반 편집은 재탐색.
- CC-083/CC-064 는 둘 다 migration_parsers.py 를 편집한다(동일 WP라 충돌 아님). SqlStatementScanner 추가를 먼저 하고 extract_* 리팩터를 뒤에 적용해 편집 겹침을 피할 것.
- dedup 추출(CC-064/086/087/090/093)은 반드시 동작 보존이어야 함 — 기존 테스트가 issue_type 과 정확 개수를 assert(test_migration_rules.py: check_enum_numeric_index/calculate_column_byte_size/latin1/zerofill/invalid_datetime 등)한다. 콜백 리팩터 후에도 이슈 텍스트/severity/개수가 1:1 동일해야 함.
- 이 WP 는 5개 소스파일(migration_dump_analyzer.py, migration_parsers.py, migration_rules/{data,schema,storage}_rules.py)을 라운드2 내에서 배타적으로 소유해야 함. 동일 라운드의 다른 WP 가 이 파일들 또는 migration_rules/__init__.py 를 건드리면 충돌 — 스케줄러가 file-disjoint 를 보장할 것.
- 로컬 pytest 에는 항상 실패하는 GitHub-CI 의존 macOS validation 테스트가 있음(MEMORY 기록). 전체 pytest 회귀 판정 시 해당 known-flaky 케이스는 무시.

### WP-2.4 — split
**Branch:** `refactor/cc-r2-scheduler-split` · **Size:** L · **발견:** 5건 (H1/M3/L1)

**Findings covered:** CC-018, CC-019, CC-020, CC-021, CC-022

**수정 파일:** `src/core/scheduler.py`, `tests/test_scheduler.py`
**신규 파일:** `src/core/schedule_config.py`, `src/core/cron_parser.py`, `src/core/execution_log_writer.py`, `src/core/retention_policy.py`, `src/core/backup_task_executor.py`, `src/core/sql_query_task_executor.py`
**테스트:** `tests/test_scheduler.py`

**가이드:**
- [CC-018] 분할 전제: scheduler.py에는 BackupScheduler(스케줄링 엔진+오케스트레이터)만 남기고 책임을 6개 새 모듈로 분리한다. 단 ScheduleConfig / CronParser / BackupScheduler / ScheduleTaskType는 반드시 `from src.core.scheduler import ...`로 계속 import되도록 scheduler.py 상단에서 re-export한다(consumer: src/ui/dialogs/schedule_dialog.py, src/ui/main_window.py는 Round3라 절대 수정 금지 — 재노출로 무변경 유지). BackupScheduler의 공개 메서드(start/stop/is_running/get_schedules/get_schedule/add_schedule/update_schedule/remove_schedule/set_enabled/run_now/add_callback/remove_callback/get_backup_logs)는 시그니처 그대로 유지.
- [CC-018] 저위험 우선 분리: `src/core/schedule_config.py`로 ScheduleTaskType, ScheduleConfig, _ExecutionJob, _ResolvedConnection를, `src/core/cron_parser.py`로 CronParser를 로직 변경 없이 이동하고 scheduler.py에서 재노출. BackupScheduler 내부가 쓰는 _ExecutionJob(run_now/_snapshot_due_jobs)과 _ResolvedConnection(_resolve_connection)도 이 import로 참조.
- [CC-018][CC-020] `src/core/execution_log_writer.py`에 ExecutionLogWriter 신설: 기존 _log_backup→`log_execution(schedule, success, message)`, get_backup_logs 로직→`get_logs(days)`로 이관. BackupScheduler는 self._log_writer=ExecutionLogWriter()를 보유하고 `get_backup_logs(self, days=7)`는 self._log_writer.get_logs(days)로 위임(공개 API 유지 — schedule_dialog.py:1066이 호출). ⛔ 온디스크 디렉토리명 `backup_logs`와 파일 접두사 `backup_`는 절대 변경 금지(런타임 동작·기존 로그·get_logs 왕복 호환 보존). 이름 변경은 메서드명·docstring·지역변수 한정.
- [CC-019] `src/core/retention_policy.py`에 공용 헬퍼 `select_paths_for_retention(entries: List[Tuple[str, datetime]], retention_days: int, retention_count: int) -> List[str]` 신설. 알고리즘(현행과 완전 동일): entries를 timestamp 오름차순 정렬 → now-timedelta(days=retention_days)보다 오래된 항목을 삭제대상에 추가 → 남은 것 중 retention_count 초과분을 가장 오래된 것부터 삭제대상에 추가 → 삭제대상 path 리스트 반환. 각 caller(_cleanup_old_backups는 os.listdir 디렉토리+폴더명 파싱 timestamp, _cleanup_old_results는 파일+os.path.getmtime)의 목록화·삭제 side effect만 남기고 선정 로직은 이 헬퍼로 대체.
- [CC-018] `src/core/backup_task_executor.py`에 BackupTaskExecutor 신설: _execute_backup + _cleanup_old_backups 이관. 생성자에 resolve_connection(콜백), log_writer 주입. ⛔ RustDumpExporter/RustDumpConfig는 반드시 execute 메서드 내부 지역 import(현행 589행 그대로) — 테스트가 `src.exporters.rust_dump_exporter.RustDumpExporter`를 monkeypatch(테스트 460/504/551/648행)하므로 호출 시점 조회 필수. 성공 시 schedule.last_run 갱신 동작 보존(_run_execution_job이 이 값을 읽어 반영).
- [CC-018] `src/core/sql_query_task_executor.py`에 SqlQueryTaskExecutor 신설: _execute_sql_query / _execute_single_query / _save_query_result / _save_as_csv / _save_as_json / _cleanup_old_results / _parse_sql_queries 이관. 생성자에 resolve_connection, connector_factory, log_writer 주입. parse_sql_statements(sql_statement_parser)와 classify_sql_statement(sql_query_classifier)는 이 모듈에서 직접 import(monkeypatch 대상 아님 — 안전).
- [CC-018][monkeypatch 보존 — 최중요] connector 생성은 반드시 scheduler.py에 정의된 래퍼를 경유한다: scheduler.py 모듈 스코프에 `from src.core.db_core_service import create_rust_db_connector, normalize_db_engine` 유지하고, BackupScheduler에 `_make_connector(self, *a, **k): return create_rust_db_connector(*a, **k)` 메서드를 두어 `SqlQueryTaskExecutor(connector_factory=self._make_connector, ...)`로 주입. 이 래퍼가 scheduler.py 모듈 전역 이름을 호출 시점에 조회하므로 `monkeypatch.setattr("src.core.scheduler.create_rust_db_connector", ...)`(테스트 613/707/1131행)가 그대로 반영된다. ⛔ executor 모듈에서 create_rust_db_connector를 직접 import하면 테스트가 깨진다.
- [CC-018][테스트 표면 보존] BackupScheduler에 얇은 위임 메서드를 남긴다: _execute_backup→self._backup_executor.execute, _execute_sql_query→self._sql_executor.execute, _execute_single_query(connector, schedule, query, timestamp, query_index)→self._sql_executor.execute_single(...) (5-인자 시그니처 그대로), _parse_sql_queries→self._sql_executor.parse_queries. _execute_task / _snapshot_due_jobs / _run_loop / _ensure_execution_thread / _execution_worker_loop / _run_execution_job / _resolve_connection / _active_schedule_ids / _lock / _thread / _callbacks / _notify_callbacks는 BackupScheduler에 그대로 유지(테스트가 인스턴스에서 직접 호출/설정/접근: 예 self.scheduler._execute_task=fake, _active_schedule_ids, _lock).
- [CC-022] _execute_single_query(이관 후) 885-886행: 중간 변수 engine→endpoint 이름만 변경(`endpoint = getattr(getattr(connector, "connection", None), "endpoint", None)`; `engine_name = getattr(endpoint, "engine", "mysql")`). ⛔ Option B(시그니처에 resolved.engine 파라미터 추가) 채택 금지 — 테스트 854/904/957행이 정확히 5-인자로 직접 호출하므로 시그니처를 절대 바꾸지 말 것.
- [CC-021] scheduler.py 23행 import에서 미사용 DEFAULT_MYSQL_PORT 제거. DEFAULT_LOCAL_HOST는 _resolve_connection(575행)에서 사용하므로 유지(`from src.core.constants import DEFAULT_LOCAL_HOST`).
- [순환참조 방지] 새 모듈 6개는 절대 scheduler.py를 import하지 않는다(의존은 전부 DI로 주입) — leaf 모듈로 유지해 조용한 순환참조/NameError를 예방. 각 새 모듈에 이동 코드가 참조하는 심볼의 import를 빠짐없이 추가(datetime/timedelta, os, csv, json, shutil, re, get_logger 등). scheduler.py는 새 모듈들을 import하여 BackupScheduler.__init__에서 collaborator를 조립.
- [검증·안전] DB 작업 소유권은 tunnelforge-core — Python DB 드라이버 직접 경로 재도입 금지(create_rust_db_connector 경유 유지). 버전 bump·기능 변경 금지(순수 행위 보존 리팩터). 리팩터 후 `python -m pytest tests/test_scheduler.py -q` 전체 통과 확인 후 `python -m pytest -q`로 회귀 없음 확인. 만약 files_touched/new_files 밖 파일(예: schedule_dialog.py, main_window.py) 수정이 필요해지면 즉시 중단하고 재스케줄 요청(재노출·공개 시그니처 유지로 발생하지 않도록 설계됨).

**검증:**
- `python -m py_compile src/core/scheduler.py src/core/schedule_config.py src/core/cron_parser.py src/core/execution_log_writer.py src/core/retention_policy.py src/core/backup_task_executor.py src/core/sql_query_task_executor.py`
- `python -m pytest tests/test_scheduler.py -q`
- `python -m pytest -q`

**리스크:**
- 강한 화이트박스 테스트 결합: tests/test_scheduler.py가 BackupScheduler의 private 메서드(_execute_backup/_execute_sql_query/_execute_single_query/_parse_sql_queries)와 private 속성(_active_schedule_ids/_lock/_thread/_callbacks/_execute_task)을 인스턴스에서 직접 사용. 위임 메서드/속성을 BackupScheduler에 반드시 남기지 않으면 다수 테스트가 깨진다.
- monkeypatch 대상 `src.core.scheduler.create_rust_db_connector`(테스트 613/707/1131행 3곳)이 유효해야 함 — scheduler.py 모듈 스코프 import 유지 + scheduler.py 내부 래퍼(_make_connector) 경유로 connector 생성해야 반영됨. SqlQueryTaskExecutor가 create_rust_db_connector를 직접 import하면 monkeypatch 미반영으로 테스트 실패.
- _execute_single_query는 5-인자 시그니처(connector, schedule, query, timestamp, query_index) 고정 — 테스트 854/904/957행이 정확히 5개 위치인자로 호출. CC-022는 반드시 변수 rename(Option A)만 적용, 시그니처 확장(Option B) 금지.
- CC-020 로그 파일 레이아웃(backup_logs 디렉토리, backup_ 접두사) 변경은 기능 변경이자 get_backup_logs 읽기·기존 사용자 로그 호환 파괴 → 메서드/docstring/지역변수 rename만 허용.
- consumer 파일 src/ui/dialogs/schedule_dialog.py(ScheduleConfig, CronParser, BackupScheduler, ScheduleTaskType import + get_backup_logs 호출)와 src/ui/main_window.py(BackupScheduler import)는 Round3 대상 — 본 WP에서 수정 금지. scheduler.py 재노출로 무변경 유지(consumer 약 2개 파일).
- god-file split 시 순환참조/조용한 NameError 위험(프로젝트 MEMORY god-file-split-pitfalls). 새 모듈은 scheduler.py를 import하지 않는 leaf로 유지하고 이동 코드가 쓰는 표준 라이브러리/logger import를 각 모듈에 누락 없이 추가할 것.
- RustDumpExporter/RustDumpConfig를 BackupTaskExecutor.execute 내부 지역 import로 유지하지 않으면 `src.exporters.rust_dump_exporter.RustDumpExporter` monkeypatch(테스트 460/504/551/648행)가 무효화되어 백업 테스트가 깨진다.
- scheduler.py / tests/test_scheduler.py는 이 WP 단독 소유여야 함 — 동일 라운드(Round2) 내 다른 WP가 같은 파일을 건드리면 충돌. (schedule_dialog.py/main_window.py를 손대는 Round3 UI WP와는 파일 분리되어 무관.)

### WP-2.5 — tools-cleanup
**Branch:** `refactor/cc-r2-schema-tools-cleanup` · **Size:** L · **발견:** 10건 (H4/M4/L2)

**Findings covered:** CC-096, CC-097, CC-098, CC-105, CC-106, CC-107, CC-108, CC-109, CC-110, CC-111

**수정 파일:** `src/core/schema_comparator.py`, `src/core/schema_diff_models.py`, `src/core/schema_extractor.py`, `src/core/schema_severity_classifier.py`, `src/core/schema_sync_script_generator.py`
**테스트:** `tests/test_schema_diff.py`, `tests/test_severity_classifier.py`, `tests/test_diff_dialog.py`

**가이드:**
- [CC-096] SchemaComparator에 제네릭 private 헬퍼 `_compare_named_entities(source_map, target_map, content_key_fn, diff_builder)`를 추가해 (1)이름 매칭→MODIFIED/UNCHANGED, (2)미매칭 항목 content-key 인덱싱→'첫 미점유 후보 win' RENAMED 감지, (3)잔여 ADDED/REMOVED 3단계 알고리즘을 한 번만 구현한다. `_compare_indexes`/`_compare_foreign_keys`는 각 엔티티의 content_key(`_index_content_key`/`_fk_content_key`)와 diff 생성 클로저(IndexDiff/ForeignKeyDiff + 필드별 한글 메시지, MODIFIED 시 컬럼/unique vs ref_table/columns/on_delete/on_update)를 넘기는 ~15줄 wrapper로 축소. sorted() 순회 순서와 매칭 우선순위를 그대로 보존해 결과 리스트 순서가 불변이어야 한다.
- [CC-097][CC-105] `generate_sync_script`(12-173)를 5개 phase 메서드 `_generate_fk_drops`/`_generate_table_drops`/`_generate_table_creates`(기존 `_generate_create_table` 재사용)/`_generate_alter_statements`/`_generate_fk_adds`로 분해(각각 List[str] 반환), 오케스트레이터는 헤더+각 phase 결과를 join. 반복되는 `ALTER TABLE \`{schema}\`.\`{table}\` ...` 접두어(13개소: 40,47,53,95,100,105,116,120,127,132,137,152,160)는 모듈/메서드 헬퍼 `_alter_table(target_schema, table_name, clause)`로 통일. 반드시 기존과 byte-identical 출력(개행·섹션 헤더·세미콜론 포함) 유지 — 테스트가 정확한 SQL 문자열을 assert한다.
- [CC-106] schema_diff_models.py의 `_normalize_column_extra` 옆(dataclass 정의보다 위)에 `PRIMARY_KEY_INDEX_NAME = "PRIMARY"`와 `def is_primary_key_index(name: str) -> bool: return name.upper() == PRIMARY_KEY_INDEX_NAME`를 추가하고, 4개 call site(schema_sync_script_generator.py:111, :191, schema_diff_models.py:115, schema_severity_classifier.py:157)를 헬퍼 호출로 교체한다. exact-case였던 111/115가 case-insensitive로 통일되는데, MySQL INFORMATION_SCHEMA는 항상 'PRIMARY' 대문자를 반환하므로 런타임 영향이 없고 기존 테스트도 대문자를 쓴다(의도된 통일).
- [CC-098] classifier의 암묵적 문자열 접두어 의존을 공유 상수로 명시화한다. schema_diff_models.py에 `DIFF_PREFIX_TYPE="타입:"`, `DIFF_PREFIX_NULLABLE="Nullable:"`, `DIFF_PREFIX_DEFAULT="Default:"`, `DIFF_PREFIX_EXTRA="Extra:"`, `DIFF_PREFIX_CHARSET="Charset:"`, `DIFF_PREFIX_COLLATION="Collation:"`, `AUTO_INCREMENT_KEYWORD="auto_increment"` 상수를 정의하고, 생산자 `_compare_columns`(schema_comparator.py:143-166)의 f-string 접두어와 소비자 `_classify_column`/`_classify_extra_change`(schema_severity_classifier.py:88-100,140-145)의 startswith/`in` 검사를 모두 동일 상수로 교체. ⚠️ 상수 값이 현재 리터럴과 정확히 일치해야 생성 문자열·diff_dialog 표시·기존 테스트가 불변. finding이 권한 구조화 enum(differences를 dataclass로 승격)은 behavior-preserving 위반이자 test_severity_classifier의 List[str] differences를 깨므로 채택하지 않는다.
- [CC-109] schema_diff_models.py에 `_quote_ident(name)`→백틱 래핑, `_quote_idents(names)`→백틱 콤마조인 헬퍼를 `_normalize_column_extra` 옆(dataclass 위)에 추가하고, `ColumnInfo.to_sql_definition`(81)/`IndexInfo.to_sql_definition`(114)/`ForeignKeyInfo.to_sql_definition`(135-136)의 인라인 백틱 조립을 헬퍼로 교체. schema_sync_script_generator.py의 인라인 식별자 백틱 조립도 가능한 곳은 동일 헬퍼 사용. 모든 출력 문자열은 불변 유지.
- [CC-110][CC-111] (sweep) schema_severity_classifier.py 정리: `_max_severity`가 매 호출 생성하던 order dict를 클래스 상수 `_SEVERITY_ORDER = {DiffSeverity.CRITICAL:3, DiffSeverity.WARNING:2, DiffSeverity.INFO:1}`(`_INTEGER_TYPES` 옆)로 승격 후 참조로 교체. `_classify_type_change(self, diff_text, col_diff)`(106)의 미사용 `diff_text` 파라미터를 제거해 `_classify_type_change(self, col_diff)`로 만들고 호출부(90)를 `self._classify_type_change(diff)`로 수정.
- [CC-107] schema_extractor.py의 반복되는 `try: <query+parse> except Exception as e: logger.error(...); return <default>` 보일러플레이트만 내부 헬퍼로 DRY 정리하되, 각 메서드의 반환 계약·로깅·예외 swallow 동작을 그대로 보존한다. re-raise 또는 error/empty 구분 sentinel 도입은 금지 — 이는 기능 변경이며 test_get_table_options_defaults_on_failure / test_get_row_count_exception_returns_zero를 깨고 behavior-preserving 제약을 위반한다. finding의 심층 의미(진짜 빈 결과 vs 쿼리 실패 구분)는 별도 기능 변경 작업으로 이월(risks 참조).
- [CC-108] N+1 및 중복 COUNT(*) 최적화는 이번 라운드에서 손대지 않고 이월한다. TABLE_ROWS(근사치) 교체는 결과값을 바꾸고, `_get_table_options` 시그니처 변경/`_get_row_count` 제거는 caller(extract_table_schema)와 기존 테스트를 깨며, 스키마 일괄 조회 재구성은 쿼리 형태·파싱 로직을 바꾸는 기능/성능 변경이라 behavior-preserving 제약과 충돌한다. 안전한 무변경 정리분이 없으므로 이번 WP에서는 제외하고 별도 성능 작업으로 남긴다(risks에 명시).
- (HARD CONSTRAINTS) 전 변경은 동작 보존 리팩터링만 — 기능 변경 없음, 버전 bump 없음, DB 작업 소유권은 tunnelforge-core 유지(Python DB 드라이버 hot path 재도입 금지). `src/core/schema_diff.py` 재수출 루트와 public 클래스/메서드 시그니처는 불변으로 두어 소비자 diff_dialog.py·diff_workers.py 및 그 테스트를 건드리지 않는다. 새 상수/헬퍼는 schema_diff_models.py 내부에 두며(재수출 추가는 선택적 additive, 기본은 루트 미변경). 위 5개 파일 밖 수정이 필요해지면 즉시 중단하고 재스케줄을 요청한다.

**검증:**
- `python -m py_compile src/core/schema_comparator.py src/core/schema_diff_models.py src/core/schema_extractor.py src/core/schema_severity_classifier.py src/core/schema_sync_script_generator.py`
- `python -m pytest tests/test_schema_diff.py tests/test_severity_classifier.py tests/test_diff_dialog.py -q`
- `python -m pytest -q`

**리스크:**
- CC-107 심층 수정(re-raise / error vs empty 구분)과 CC-108(근사 TABLE_ROWS 교체, N+1 배치화, _get_table_options/_get_row_count 시그니처 변경)은 관찰 가능한 동작을 바꾸는 기능 변경으로, test_get_table_options_defaults_on_failure·test_get_row_count_exception_returns_zero를 깨고 behavior-preserving 제약을 위반한다. 본 WP는 안전한 부분집합만 수행(CC-107=DRY 정리, CC-108=이월)하며 심층 fix는 별도 functional-change 작업 필요.
- byte-identical 출력 요구: test_generate_modify_column_strips_default_generated는 정확한 'MODIFY COLUMN ...' SQL과 섹션 문자열을, test_generate_create_table_uses_primary_index_order는 정확한 'PRIMARY KEY (...)'를 assert한다. generate_sync_script 분해(CC-097)+_alter_table(CC-105)+_quote_ident(CC-109) 적용 시 공백/개행/세미콜론/섹션 헤더를 동일하게 재현하지 못하면 즉시 실패.
- CC-106은 exact-case(sync:111, models:115)를 case-insensitive is_primary_key_index로 통일한다 — 이론상 소문자 'primary' 입력에 대한 잠재적 동작 변화이나 MySQL이 항상 'PRIMARY' 대문자를 반환하므로 런타임 영향 0, 기존 테스트도 대문자 사용.
- CC-098은 finding 권장(구조화 enum) 대신 값이 동일한 공유 상수 방식을 채택했다. 상수 값을 현재 리터럴('타입:' 등)과 다르게 정의하면 diff_dialog 표시 문자열과 다수 테스트(differences=[...] assert)가 깨진다 — 값 정확 일치가 필수.
- 소비자 src/ui/dialogs/diff_dialog.py, diff_workers.py 및 tests(test_schema_diff/test_severity_classifier/test_diff_dialog)는 모두 재수출 루트 src.core.schema_diff 경유로 import한다. public 클래스명/시그니처나 재수출 목록을 바꾸면 이 파일들과 그 테스트가 깨지므로 schema_diff.py는 미변경 유지.
- _quote_ident/is_primary_key_index/DIFF_PREFIX_* 헬퍼·상수는 schema_diff_models.py의 dataclass들보다 위(모듈 상단, _normalize_column_extra 인접)에 정의해야 IndexInfo/ColumnInfo/ForeignKeyInfo 메서드에서 NameError가 나지 않는다.
- same-round 충돌 방지: 이 5개 schema_* 파일은 다른 Round-2 WP가 동시에 건드리면 안 된다(파일 disjoint 가정). 만약 스케줄 상 겹치면 순차화 필요 — 현재 스캔상 겹침 없음. 참고로 schema_diff god-file 분리는 이미 main(commits e3ee3b4/a7625a0)에 병합되어 있어 Round-1 WP 의존성은 없음.

### WP-2.6 — dump-exporter-split
**Branch:** `refactor/cc-r2-rust-dump-exporter-split` · **Size:** L · **발견:** 6건 (H1/M4/L1)

**Findings covered:** CC-099, CC-100, CC-101, CC-102, CC-103, CC-104

**수정 파일:** `src/exporters/rust_dump_exporter.py`, `CLAUDE.md`
**신규 파일:** `src/core/foreign_key_resolver.py`, `src/exporters/dump_progress.py`
**테스트:** `tests/test_rust_dump_exporter.py`, `tests/test_db_orphan_dialog.py`, `tests/test_db_export_dialog.py`, `tests/test_db_import_dialog.py`, `tests/test_db_dialogs.py`, `tests/test_current_status_docs.py`, `tests/test_foreign_key_resolver.py (new)`, `tests/test_dump_progress.py (new)`

**가이드:**
- [CC-099] god-file 3분할: `src/core/foreign_key_resolver.py` 신규 생성 → `OrphanRecordInfo`(dataclass)와 `ForeignKeyResolver`(MySQLConnector 직접 사용) 클래스를 원문 그대로 이동. `src/exporters/dump_progress.py` 신규 생성 → `TableProgressTracker`, 모듈 함수 `emit_core_event`, 그리고 emit_core_event 전용 private 헬퍼 `_format_import_phase_message`(line 52)를 이동. `rust_dump_exporter.py`에는 `RustDumpConfig/RustDumpChecker/RustDumpExporter/RustDumpImporter`와 이들이 직접 쓰는 경로안전 헬퍼 `_safe_dump_child_dir`/`_safe_dump_child_file`(_analyze_dump_metadata에서 사용)만 남긴다.
- [CC-099] 하위호환 re-export 필수: `rust_dump_exporter.py` 상단에 `from src.core.foreign_key_resolver import ForeignKeyResolver, OrphanRecordInfo`, `from src.exporters.dump_progress import TableProgressTracker, emit_core_event`를 추가해 기존 이름을 그대로 노출한다. 소비자(src/core/scheduler.py, src/ui/dialogs/db_export_dialog.py, db_import_dialog.py, db_orphan_dialog.py, src/ui/workers/rust_dump_worker.py)와 `src/exporters/__init__.py`, 5개 테스트가 전부 `from src.exporters.rust_dump_exporter import ...`로 접근하므로, RustDumpChecker/RustDumpConfig/RustDumpExporter/RustDumpImporter/ForeignKeyResolver/OrphanRecordInfo/check_rust_dump/export_schema/export_tables/import_dump/emit_core_event/TableProgressTracker/DEFAULT_DUMP_COMPRESSION 전부가 rust_dump_exporter에서 계속 import 가능해야 이 파일들을 건드리지 않는다.
- [CC-099] 순환참조 방지: `foreign_key_resolver.py`는 `src.core.db_connector.MySQLConnector` + stdlib(datetime/typing)만 의존하고 rust_dump_exporter를 절대 역참조하지 않는다. `dump_progress.py`도 json/typing만 의존하는 자기완결 모듈로 만든다. import는 rust_dump_exporter → 두 신규 모듈 단방향만 허용(양방향 금지).
- [CC-104] `ForeignKeyResolver`에 private 헬퍼 `_orphan_join_where(schema, table, column, ref_table, ref_column) -> str` 추가(``FROM `{schema}`.`{table}` c LEFT JOIN `{schema}`.`{ref_table}` p ON c.`{column}` = p.`{ref_column}` WHERE c.`{column}` IS NOT NULL AND p.`{ref_column}` IS NULL`` 조각만 반환). `generate_orphan_query`(197-201), `find_orphan_records`의 count_query(225-231)/sample_query(237-243), `get_all_orphan_queries`(318-321)가 각자 SELECT 절/LIMIT만 덧붙여 이 조각을 공유하도록 리팩터. 생성되는 최종 SQL 문자열은 공백·개행·백틱까지 기존과 동등해야 함(실 DB 동작 및 orphan 리포트 보존).
- [CC-100] `_RustDumpClientBase` 도입: `__init__(self, config, facade=None)`(self.config=config; self.facade=facade if facade is not None else DbCoreFacade(); self._owns_facade=facade is None)와 공유 `_endpoint(self, schema)`를 정의하고 `RustDumpExporter`/`RustDumpImporter`가 상속하도록 변경. `RustDumpExporter(config)`/`RustDumpImporter(config)` 위치인자 호출(worker/scheduler/테스트)과 `facade=` 키워드 호출이 그대로 동작해야 하며, `test_exporter_initialization`의 `exporter.config == config` 및 `not hasattr(exporter, "_connector")` 단언이 통과해야 함.
- [CC-101] 콜백 수프 축소는 내부에만 적용(공개 시그니처 불변): `dump_progress.py`에 `@dataclass class DumpEventCallbacks`(progress/table_progress/detail/table_status/raw_output/metadata/table_chunk_progress = None) 정의. private `_run_rust_dump`의 시그니처를 `callbacks: Optional[DumpEventCallbacks]=None`로 축소하고 내부 forwarding을 번들로 통일. HARD CONSTRAINT: 공개 메서드 `export_full_schema`/`export_tables`/`import_dump`의 파라미터 개수·순서를 절대 변경 금지 — rust_dump_worker.py(out-of-scope, Round 3)가 콜백들을 위치인자로 넘긴다. 공개 메서드는 기존 개별 콜백을 받아 내부에서 `DumpEventCallbacks(...)`로 묶어 `_run_rust_dump`/`emit_core_event`에 전달만 한다.
- [CC-102] `rust_dump_exporter.py` line 20 `DEFAULT_DUMP_COMPRESSION` 바로 옆에 `DEFAULT_DUMP_THREADS = 8` 추가. `_run_rust_dump`(406)/`export_full_schema`(453)/`RustDumpExporter.export_tables`(489)/`RustDumpImporter.import_dump`(633)/모듈함수 `export_schema`(865)/`export_tables`(882)/`import_dump`(906)의 `threads: int = 8` 기본값 7곳을 `threads: int = DEFAULT_DUMP_THREADS`로 교체(값 8 불변 → 동작 보존). 상수는 신규 모듈이 아닌 rust_dump_exporter.py에 두어야 module-level 함수/클래스가 참조 가능.
- [CC-103] `emit_core_event`를 `dump_progress.py`에서 dispatch table로 재구성: 이벤트타입별 private 핸들러 `_handle_dump_plan_event`/`_handle_dump_schedule_event`/`_handle_phase_event`/`_handle_table_progress_event`/`_handle_row_progress_event`로 분리하고 row_progress의 status remap(807)·rows_sec(821)·percent(822-825) 산술을 해당 핸들러로 이동. HARD CONSTRAINT: 공개 `emit_core_event(event, progress_callback=None, table_progress_callback=None, detail_callback=None, table_status_callback=None, raw_output_callback=None, import_results=None, table_chunk_progress_callback=None)`의 이름·순서·기본값 유지 — RustDumpImporter.import_dump(689)가 8개 위치인자로, 테스트가 키워드로 호출. raw_output_callback 선처리 및 각 핸들러가 만드는 detail dict의 키·값이 기존과 동등해야 함.
- [문서] `CLAUDE.md`의 ForeignKeyResolver 서술(line 90 부근 "in the same module") 갱신: `ForeignKeyResolver`/`OrphanRecordInfo`가 이제 `src/core/foreign_key_resolver.py`에 위치하고 rust_dump_exporter에서 re-export됨을 명시. "RustDumpExporter._resolve_required_tables_from_rust_schema", "RustDumpConfig", "RustDumpExporter.export_tables" 등 다른 문장은 유지. (doc 테스트는 docs/current_status.md만 읽으므로 CLAUDE.md 편집이 테스트를 깨지 않음.)
- [CC-099][sweep] 신규 경로 import 커버리지 추가(선택): tests/test_foreign_key_resolver.py, tests/test_dump_progress.py를 만들어 `from src.core.foreign_key_resolver import ForeignKeyResolver, OrphanRecordInfo` 및 `from src.exporters.dump_progress import emit_core_event, TableProgressTracker` 경로를 고정. 기존 test_rust_dump_exporter.py의 import는 re-export 덕에 수정 불필요하나, 어떤 테스트가 private 심볼을 직접 참조하면 rust_dump_exporter에서 동일 심볼을 re-export해 깨지지 않게 한다.

**검증:**
- `python -m py_compile src/exporters/rust_dump_exporter.py src/core/foreign_key_resolver.py src/exporters/dump_progress.py`
- `python -c "from src.exporters import rust_dump_exporter as m; [getattr(m, n) for n in ('RustDumpChecker','RustDumpConfig','RustDumpExporter','RustDumpImporter','ForeignKeyResolver','OrphanRecordInfo','check_rust_dump','export_schema','export_tables','import_dump','emit_core_event','TableProgressTracker','DEFAULT_DUMP_COMPRESSION','DEFAULT_DUMP_THREADS')]"`
- `python -c "import src.exporters"`
- `python -m pytest tests/test_rust_dump_exporter.py tests/test_db_orphan_dialog.py tests/test_db_export_dialog.py tests/test_db_import_dialog.py tests/test_db_dialogs.py -q`
- `python -m pytest`

**리스크:**
- rust_dump_worker.py(Round 3, out-of-scope)가 export_full_schema/export_tables/import_dump를 위치인자(콜백 포함)로 호출한다(worker 80-90/108-120/144-158 확인). 따라서 CC-101의 문언 그대로 공개 메서드 시그니처를 DumpEventCallbacks 번들로 바꾸면 worker가 깨지고 out-of-scope 파일 수정이 필요해진다 → 공개 시그니처는 반드시 불변, 번들화는 private _run_rust_dump에만 적용. 위반 시 실행 에이전트는 중단·재스케줄 요청해야 함.
- import fan-out가 큼: rust_dump_exporter는 src/core/scheduler.py, src/ui/dialogs/{db_export_dialog,db_import_dialog,db_orphan_dialog}.py, src/ui/workers/rust_dump_worker.py, src/exporters/__init__.py, 그리고 test_rust_dump_exporter/test_db_dialogs/test_db_export_dialog/test_db_import_dialog/test_db_orphan_dialog 5개 테스트가 참조한다. 이동한 심볼(ForeignKeyResolver/OrphanRecordInfo/TableProgressTracker/emit_core_event)을 rust_dump_exporter에서 re-export하지 않으면 이 소비자들과 src/exporters/__init__.py(1-5행이 rust_dump_exporter에서 ForeignKeyResolver 등 import)가 전부 깨진다.
- test_db_orphan_dialog.py:32가 `src.ui.dialogs.db_orphan_dialog.ForeignKeyResolver`를 monkeypatch한다. 이는 db_orphan_dialog의 `from src.exporters.rust_dump_exporter import ForeignKeyResolver`(17행) 바인딩을 통해 해결되므로 rust_dump_exporter가 ForeignKeyResolver를 계속 re-export하는 한 유지된다. db_orphan_dialog의 import 라인은 out-of-scope이므로 수정 금지.
- emit_core_event의 전체 위치 시그니처가 RustDumpImporter.import_dump(689)에서 8개 위치인자(import_results, table_chunk_progress_callback 포함)로 호출된다. CC-103 dispatch 리팩터 시 파라미터 순서/이름/기본값을 100% 보존해야 하며, 생성되는 detail dict의 키·값도 테스트(TestCoreEventForwarding)와 동등해야 한다.
- 동일 파일에 대한 별개(오래된) 감사 계획 audit-master-plan-2026-07-08.md의 WP-2.8이 rust_dump_exporter.py + test_rust_dump_exporter.py를 수정하지만, 이는 branch prefix fix/audit-r2- 의 다른 계획(이미 머지된 것으로 추정)이며 본 clean-code 플랜의 Round 2가 아니다. 그래도 파일-분리 원칙상 본 clean-code Round 2의 다른 WP가 rust_dump_exporter.py를 건드리지 않는지 확인 필요 — 겹치면 same-round 충돌로 재스케줄 대상.
- DB 소유권 보존: ForeignKeyResolver는 MySQLConnector를 직접 사용하는 기존 코드로, tunnelforge-core 우회가 아니라 orphan 분석 전용 유틸이다(파셜 export FK 포함은 RustDumpExporter._resolve_required_tables_from_rust_schema가 담당). 이동만 하고 로직 변경 없이 그대로 유지하여 Python DB 드라이버 hot path를 새로 도입하지 않는다.

### WP-2.7 — engine-core-cleanup
**Branch:** `refactor/cc-r2-cross-engine-core-cleanup` · **Size:** M · **발견:** 4건 (H1/M1/L2)

**Findings covered:** CC-076, CC-077, CC-078, CC-079

**수정 파일:** `src/core/cross_engine_migration.py`
**테스트:** `tests/test_cross_engine_migration_protocol.py`

**가이드:**
- [CC-076] render_result_report(269-375)의 5개 섹션 인라인 렌더링을 파일 내부 private 헬퍼로 분해한다. 각 헬퍼는 List[str]을 반환하고 render_result_report는 lines.extend(...)로 이어붙인다. 제안 시그니처: _render_issues(issues)->List[str], _render_mismatches(mismatches)->List[str], _render_plan(plan)->List[str], _render_directions(directions)->List[str]. render_result_report의 public 시그니처 render_result_report(payload: Dict[str, Any]) -> str는 절대 변경하지 않는다(import 경로/호출부 dialog line1189, test line106 보존).
- [CC-076] 가장 깊은 directions 분기(318-373, 최대 9단)는 3개 헬퍼로 계층 분리한다: (1) _render_directions는 상단 readiness 루프만 담당하며 각 direction에 대해 status/table_count/issue 줄을 만들고 guide가 dict이면 _render_direction_guide로 위임, (2) _render_direction_guide(guide)는 create_table_sql / sequence_reset_sql+post_data_sql(follow-up) 렌더링과 tables 루프를 담당하되 각 table을 _render_table_guide로 위임, (3) _render_table_guide(table)은 columns/row_samples/insert_example_sql를 렌더링한다.
- [CC-076] 각 헬퍼는 guard clause(if not isinstance(x, dict): return []  또는 루프 내 continue)를 사용해 중첩을 2-3단으로 낮춘다. 예: _render_table_guide 진입 시 'if not isinstance(table, dict): return []', columns 루프에서 'if not isinstance(column, dict): continue'. 기존 isinstance 검사 로직과 동일한 분기 결과를 유지해야 한다.
- [CC-078] _render_directions 추출 과정에서 inner 루프의 'issues = direction.get("issues")'(324)를 'direction_issues'로 리네임하고 'issue_count = len(direction_issues) if isinstance(direction_issues, list) else 0'으로 바꾼다. 이렇게 하면 상단 payload의 issues(285)와의 shadowing 자체가 구조적으로 사라진다(헬퍼로 분리되어 스코프가 겹치지 않음).
- [CC-079] 모듈 상단(예: FULL_MIGRATION_WORKFLOW 근처)에 상수 MAX_MISMATCHES_DISPLAYED = 50을 정의하고, _render_mismatches 내부에서 mismatches[:MAX_MISMATCHES_DISPLAYED](기존 300)와 len(mismatches) > MAX_MISMATCHES_DISPLAYED(기존 306) 두 곳에 모두 사용한다. 리터럴 50을 남기지 않는다.
- [CC-077] 책임 불일치는 물리적 파일 분리가 아니라 모듈 docstring 정정으로 해소한다. lines 1-5의 docstring을 이 모듈이 실제로 소유하는 책임 전체(1) JSONL wire-format 모델/파싱/빌드 2) tunnelforge-core 실행파일 탐색 3) resume-state 디스크 영속화 4) 텍스트 결과 리포트 렌더링)을 정확히 기술하도록 다시 쓴다. 코드/함수 위치는 그대로 두는 internal-only 변경이다.
- [CC-077 HARD CONSTRAINT] cross_engine_executable.py / cross_engine_state.py 신규 생성이나 render_result_report를 migration_report_renderer.py로 이동하는 물리적 split은 이 WP에서 하지 않는다(risks 참조). migration_report_renderer.py는 MigrationReport dataclass를 HTML/JSON으로 내보내는 별개 렌더러이므로 payload dict를 렌더하는 render_result_report와 합치지 않는다.
- [출력 보존/behavior-preserving] render_result_report의 반환 문자열은 리팩터 전후 완전히 동일해야 한다. 섹션 사이 blank line('lines.append("")'), 들여쓰기 prefix('  ', '    '), json.dumps(row, ensure_ascii=False, sort_keys=True) 포맷, insert_example 조건부 출력, '... N more' 라인 순서를 헬퍼로 옮길 때 append 순서 그대로 재현한다. 테스트는 substring 검사이지만 정확 재현을 기준으로 작업한다.
- [스코프 잠금] 수정은 src/core/cross_engine_migration.py 내부로만 한정한다. DB 조작은 tunnelforge-core 소유이므로 어떤 Python DB 드라이버 hot path도 도입하지 않는다. 버전 bump 없음. 만약 어떤 수정이 dialog/worker/main.py/db_core_service.py/테스트 등 files_touched 밖 파일 편집을 요구하면 즉시 중단하고 rescheduling을 요청한다 — 본 가이드는 public 이름/시그니처를 모두 유지해 그 상황이 발생하지 않도록 설계되어 있다.

**검증:**
- `python -m py_compile src/core/cross_engine_migration.py`
- `python -m pytest tests/test_cross_engine_migration_protocol.py -q`
- `python -m pytest -q`

**리스크:**
- CC-077의 verified 권고안 전문은 물리적 모듈 split(executable discovery -> cross_engine_executable.py, resume-state -> cross_engine_state.py, render_result_report -> migration_report_renderer.py)이다. 본 WP는 이를 docstring 정정으로만 축소 이행한다. 이유: db_core_executable/save_resume_state/load_resume_state/state_key_from_payload/render_result_report는 최소 5개 소비처(src/core/db_core_service.py:13, src/ui/dialogs/cross_engine_migration_dialog.py:30-36/1189, src/ui/workers/cross_engine_migration_worker.py:13, main.py:94, tests/test_cross_engine_migration_protocol.py:12-22)에서 직접 import되며, 물리 분리는 re-export를 강제하고 round-3 UI WP들과 동일 파일 소비 위험을 키운다. 실제 물리 split이 필요하면 전용 WP로 재편성 권장.
- render_result_report는 substring 기반 3개 테스트(test_render_result_report_includes_counts_and_mismatches / _direction_readiness / _detailed_guide_rows_and_sql, 105-182)로만 검증된다. 헬퍼 추출 시 append 순서/blank line/들여쓰기를 어긋나게 하면 테스트는 통과해도 실제 리포트 포맷이 미세하게 달라질 수 있으므로 정확 재현이 필수.
- 소비처(cross_engine_migration_dialog.py, cross_engine_migration_worker.py)는 round-3 UI WP 대상이지만 이번 WP는 public 이름/시그니처와 import 경로를 전부 보존하므로 동일 라운드 파일 충돌은 없음(round-3는 후행 라운드로 file-disjoint 유지).
- migration_report_renderer.py가 이미 존재하나 MigrationReport dataclass용 HTML/JSON 익스포터로, payload dict를 렌더하는 render_result_report와 도메인이 다르다. 실수로 통합하면 동작 변경 위험 — 통합 금지.

### WP-2.8 — tunnel-platform-cleanup
**Branch:** `refactor/cc-r2-infra-tunnel-platform-cleanup` · **Size:** L · **발견:** 15건 (H0/M6/L9)

**Findings covered:** CC-023, CC-024, CC-025, CC-026, CC-027, CC-028, CC-029, CC-030, CC-031, CC-032, CC-033, CC-034, CC-035, CC-047, CC-049

**수정 파일:** `src/core/tunnel_engine.py`, `src/core/tunnel_monitor.py`, `src/core/single_instance.py`, `src/core/platform_paths.py`, `src/core/platform_integration.py`, `src/core/production_guard.py`, `src/core/mysql_login_path.py`, `src/core/logger.py`, `src/core/oneclick_log.py`, `tests/test_tunnel_monitor.py`
**신규 파일:** `src/core/tunnel_health_checker.py`
**테스트:** `tests/test_tunnel_engine.py`, `tests/test_tunnel_monitor.py`, `tests/test_single_instance.py`, `tests/test_platform_paths.py`, `tests/test_platform_integration.py`, `tests/test_production_guard.py`, `tests/test_mysql_login_path.py`, `tests/test_logger.py`

**가이드:**
- [CC-023] tunnel_engine.py에 private helper `_build_forwarder(self, config, local_bind_address, set_keepalive=None) -> SSHTunnelForwarder`를 추가한다. 공통 kwargs(bastion_host/int(bastion_port), ssh_username=config['bastion_user'], ssh_pkey=이미 로드한 pkey_obj, remote_bind_address=(remote_host,int(remote_port)))를 config에서 한 번 해석하고, set_keepalive는 None이면 kwarg 자체를 생략(SSHTunnelForwarder 기본값 유지), 값이 있으면 포함한다. _start_ssh_tunnel(local_bind_address=('0.0.0.0',int(local_port)), set_keepalive=30.0)/create_temp_tunnel/_test_ssh_tunnel_connection(local_bind_address=(DEFAULT_LOCAL_HOST,0), keepalive 없음) 3곳에서 재사용. 반드시 모듈 전역 `SSHTunnelForwarder`를 참조해야 한다(test_tunnel_engine이 `src.core.tunnel_engine.SSHTunnelForwarder`를 패치). pkey 로드 시점과 예외 흐름은 그대로 둔다.
- [CC-023] connection_logs 중복은 부차 항목이다. _start_ssh_tunnel/_test_ssh_tunnel_connection의 실패 메시지 문자열(이모지/줄순서)은 변경 금지(문구 회귀 방지). append+logger.debug을 감싸는 소형 로컬 헬퍼 도입은 선택이며, 최종 조립되는 출력 문자열이 문자 단위로 동일할 때만 적용한다.
- [CC-024] TunnelEngine의 파라미터/지역변수 `tid`를 전부 `tunnel_id`로 개명한다(start_tunnel/_start_ssh_tunnel/stop_tunnel/is_running/get_connection_info/get_active_tunnels/stop_all). 호출자는 전부 positional이고 `tid=` 키워드나 `'tid'` dict 키 사용처가 없음을 grep으로 확인했으므로 내부 개명만으로 안전. 단 get_active_tunnels가 반환하는 dict의 'id'/'tunnel_id' 키 문자열은 그대로 유지한다(외부 소비 키).
- [CC-025] health-check 책임을 새 모듈 `src/core/tunnel_health_checker.py`의 `TunnelHealthChecker`로 추출한다: _health_connections 캐시, _get_health_credentials, _create_health_connection, _measure_latency, _cleanup_health_connection, _cleanup_all_health_connections를 이관하고 tunnel_engine/config_manager/lock을 생성자로 주입. TunnelMonitor는 self._health_checker로 조합하되, 기존 테스트 표면 보존을 위해 같은 이름의 위임 메서드를 남기고 `_health_connections`는 checker의 동일 dict 객체를 돌려주는 property로 노출한다(test의 `monitor._health_connections['x']=...` 항목대입이 그대로 반영되어야 함).
- [CC-025/CC-026] 재연결 상태머신은 status/_lock/_statuses/_add_event/_notify_callbacks/_running/_auto_reconnect에 깊게 결합되어 있으므로 별도 파일로 분리하지 말고 tunnel_monitor.py에 유지한다. 대신 [CC-026] 중첩 `reconnect()` 클로저(549-604)를 명명 메서드 `_reconnect_after_delay(self, tunnel_id, delay, status)`로 승격하고 `threading.Thread(target=self._reconnect_after_delay, args=(tunnel_id, delay, status), daemon=True).start()`로 기동한다. 반드시 모듈 전역 `threading.Thread` 호출을 유지해야 한다(test가 `patch('threading.Thread')`로 검증). _attempt_reconnect의 직접 호출 가능성과 시그니처는 그대로 둔다.
- [CC-027] tunnel_monitor.py 상단에 모듈 상수 `RECONNECT_BACKOFF_SECONDS = (1, 2, 5, 10, 30, 60)`(증가 정책 주석 포함)를 추가하고 _attempt_reconnect의 인라인 `backoff = [1,2,5,10,30,60]`를 이 상수 참조로 교체한다. min(reconnect_count, len-1) 인덱싱과 값은 동일 유지.
- [CC-028] single_instance.py에 모듈 상수 `_CONNECT_ATTEMPT_TIMEOUT_MS = 100`, `_POLL_INTERVAL_SECONDS = 0.05`를 추가하고 notify_existing_instance의 waitForConnected(100)/waitForBytesWritten(100)/time.sleep(0.05) 리터럴을 대체한다. 반환값과 루프 동작은 동일 유지(test_single_instance가 True 반환을 검증).
- [CC-029] platform_paths.py에 private helper `_platform_base_dir(system, home_path, env, xdg_env_var, xdg_default_subdir) -> Path`를 추가해 Windows(LOCALAPPDATA fallback)와 Linux(XDG env fallback) 공통 로직을 캡슐화한다. 주의: macOS(Darwin)는 log_dir만 Library/Logs, app_support_dir/data_dir는 Library/Application Support로 서로 달라 helper로 뭉개면 안 된다 — Darwin 분기는 각 함수에 남기거나 파라미터로 정확히 반영. 세 함수의 trailing segment(APP_NAME, APP_NAME, APP_NAME/'logs')와 반환 경로는 문자 단위로 동일해야 한다(test가 정확한 경로를 assert).
- [CC-030] platform_integration.py 안에 `WindowsStartupRegistrar`/`MacOSStartupRegistrar` 전략 클래스를 같은 파일에 추가하고(새 import 경로 생성 금지) `StartupRegistrar`는 platform_name으로 전략을 고르는 얇은 facade로 남긴다. 공개 생성자 kwargs(platform_name, home, executable)와 is_supported/is_registered/set_registered 시그니처·반환 튜플을 그대로 유지해야 한다(test_platform_integration이 kwargs 생성 및 정확한 (True,"") 튜플을 assert). winreg/plist 구현 이동만 하고 로직은 동일.
- [CC-031] production_guard.py의 SchemaConfirmDialog._init_ui(76-207)를 `_build_header_frame(text_color,bg_color)`, `_build_details_section(details)`, `_build_schema_input_section(text_color,bg_color)`, `_build_button_row(text_color)` 등 포커스된 빌더로 분해하고 _init_ui는 조립만 담당한다. self.input_schema/self.btn_execute/self.btn_cancel 속성, 시그널 연결(textChanged->_on_text_changed, accept/reject), 위젯 트리, QSS 문자열은 동일 유지(내부 변경만, 공개 시그니처 불변).
- [CC-032/CC-033] production_guard sweep: [CC-032] 미사용 `from PyQt6.QtGui import QFont`(line 20) 제거. [CC-033] details_label(#f8f9fa/#dee2e6)와 QLineEdit(#bdc3c7/#3498db) 하드코딩 색을 ENV_COLORS 옆 모듈 상수(예: _NEUTRAL_BG,_NEUTRAL_BORDER,_INPUT_BORDER,_INPUT_FOCUS_BORDER)로 승격해 f-string으로 참조한다. 최종 렌더링 QSS 문자열은 동일해야 한다.
- [CC-034/CC-035/CC-047/CC-049] low sweep: [CC-034] mysql_login_path의 is_available()는 제거하지 말 것(main_window.py:763,789가 호출 — scope 밖 파일 수정 유발). 본문을 `return True`로 단순화하고 오해 소지 docstring을 정정하며 try/except를 제거한다. [CC-035] 모듈 상수 `_AES_BLOCK_SIZE_BITS = 128` 추가 후 PKCS7(128) 2곳(104,148)을 상수 참조로 교체. [CC-047] logger.py filter_log_by_level의 도달 불가 elif(173-174) 삭제(선행 `if f'] {level_upper} [' in line`이 ERROR도 처리). [CC-049] oneclick_log.py의 미사용 `from typing import Optional`(13) 제거.

**검증:**
- `python -m py_compile src/core/tunnel_engine.py src/core/tunnel_monitor.py src/core/tunnel_health_checker.py src/core/single_instance.py src/core/platform_paths.py src/core/platform_integration.py src/core/production_guard.py src/core/mysql_login_path.py src/core/logger.py src/core/oneclick_log.py`
- `python -m pytest tests/test_tunnel_engine.py tests/test_tunnel_monitor.py tests/test_single_instance.py tests/test_platform_paths.py tests/test_platform_integration.py tests/test_production_guard.py tests/test_mysql_login_path.py tests/test_logger.py -q`
- `python -m pytest -q`

**리스크:**
- test_tunnel_monitor.py가 TunnelMonitor 내부에 강하게 결합되어 있다: `_health_connections` dict 항목대입/in/len, `_measure_latency`/`_create_health_connection`/`_cleanup_health_connection`/`_cleanup_all_health_connections` 직접 호출, `_attempt_reconnect` 직접 호출 + `patch('threading.Thread')` + `_lock`/`_statuses`/`_max_reconnect_attempts` 조작. CC-025 추출은 반드시 위임 shim + `_health_connections` property(동일 dict 객체)로 이 표면을 보존해 테스트 무수정으로 통과시켜야 한다. 위임이 불가하면 files_touched에 포함한 test_tunnel_monitor.py를 최소 수정(어서션 의도 불변) 허용.
- test_tunnel_engine.py는 `src.core.tunnel_engine.SSHTunnelForwarder`를 모듈 스코프로 패치한다. CC-023의 `_build_forwarder`는 반드시 모듈 전역 `SSHTunnelForwarder`를 호출해야 하며 함수 내부 로컬 import 금지(패치 무력화 방지).
- CC-034: is_available()는 src/ui/main_window.py:763,789에서 호출된다. 메서드를 삭제하면 scope 밖 파일 수정이 강제되어 실행 에이전트가 중단해야 한다 — 반드시 메서드는 유지하고 본문만 `return True`로 단순화한다.
- platform_integration.StartupRegistrar의 공개 생성자 kwargs(platform_name/home/executable)와 is_registered/set_registered/(True,"") 반환 튜플을 test_platform_integration이 정확히 assert한다. 전략 분리 후에도 facade 공개 시그니처와 반환 형태를 문자 단위로 보존해야 한다. 전략 클래스는 같은 파일 내부에 두어 새 import 경로 생성을 피한다.
- platform_paths CC-029: macOS log_dir(Library/Logs)와 app_support_dir/data_dir(Library/Application Support)의 Darwin 경로가 서로 달라 공통 helper 파라미터화가 미묘하다. test가 macOS/Windows/XDG 경로를 정확히 assert하므로 helper가 세 함수의 반환 경로를 그대로 재현하는지 특히 주의.
- logger.py는 get_logger로 프로젝트 전역에서 import되는 모듈이나, CC-047은 순수 함수 filter_log_by_level 본문만 2줄 삭제(시그니처/import 불변)라 blast radius는 낮다.
- Round 2 file-disjoint 제약: 이 WP가 src/core의 tunnel_engine/tunnel_monitor/single_instance/platform_paths/platform_integration/production_guard/mysql_login_path/logger/oneclick_log 9개 파일과 신규 tunnel_health_checker.py를 소유한다. 동일 라운드의 다른 WP가 이 파일들을 건드리면 충돌이므로 같은 라운드 중복 배정 금지.

### WP-2.9 — net-sql-misc-cleanup
**Branch:** `refactor/cc-r2-infra-net-sql-misc-cleanup` · **Size:** L · **발견:** 12건 (H0/M8/L4)

**Findings covered:** CC-004, CC-005, CC-011, CC-012, CC-013, CC-015, CC-036, CC-037, CC-038, CC-039, CC-040, CC-048

**수정 파일:** `src/core/db_connector.py`, `src/core/postgres_connector.py`, `src/core/sql_history.py`, `src/core/sql_statement_parser.py`, `src/core/github_app_auth.py`, `src/core/github_issue_reporter.py`, `src/core/update_downloader.py`
**신규 파일:** `scripts/github_app_secret_codec.py`, `src/core/error_summary_builder.py`
**테스트:** `tests/test_db_connector.py`, `tests/test_sql_history.py`, `tests/test_github_app_auth.py`, `tests/test_github_issue_reporter.py`, `tests/test_update_downloader.py`, `tests/test_sql_execution_worker.py`, `tests/test_github_app_secret_codec.py (new)`, `tests/test_error_summary_builder.py (new)`

**가이드:**
- [CC-004] MySQLConnector.get_schemas/schema_exists 의 `SHOW DATABASES`/`SHOW DATABASES LIKE %s` 직접 실행을 제거하고 위임한다. `if not self.connection: return []`(schema_exists 는 `return False`) 가드는 유지하고, 위임 직전 `self._delegate.connection = self.connection` 로 라이브 커넥션을 공유시킨 뒤 `self._delegate.get_schemas()` / `self._delegate.schema_exists(schema_name)` 호출. TTL 캐시(`{prefix}:schemas`)는 MySQLConnector wrapper 에서만 감싸고 delegate 는 순수 조회만 하게 둔다. connect() 는 이미 self.connection 과 delegate.connection 을 동일 객체로 맞추므로 프로덕션 영향 없음. sync 라인 덕분에 self.connection 만 세팅하는 test_db_connector.py 도 수정 없이 통과.
- [CC-004] PostgresConnector.schema_exists 는 `if not schema_name or not self.connection: return True` 가드를 유지하고, information_schema.schemata 쿼리 직접 실행 대신 `RustDbConnector("postgresql", self.host, self.port, self.user, self.password, database=self.database, facade=self.facade)` 를 만들어 `delegate.connection = self.connection` 로 기존 커넥션을 재사용해 `delegate.schema_exists(schema_name)` 위임(connect() 구조·인스턴스 속성은 건드리지 않음). postgres_connector.py 에 `RustDbConnector` import 추가.
- [CC-005] MySQLConnector.get_db_version 의 수기 `split('-')/split('.')/int()` 파싱을 제거하고 `return parse_db_version_tuple(self.get_db_version_string())` 로 교체(`from src.core.db_core_service import parse_db_version_tuple`). get_db_version_string 은 self.connection 기반 그대로 유지하므로 기존 test_get_db_version_returns_tuple/no_connection 이 그대로 통과.
- [CC-011] sql_history.py 상단에 `from src.core.logger import get_logger` 추가 후 `logger = get_logger('sql_history')` 정의. _load_history 의 except `(json.JSONDecodeError, IOError)` 브랜치에서 `[]` 반환 전에 `logger.warning(...)` 로 손상/읽기실패를 기록하고, _save_history 의 `print(f"히스토리 저장 오류: {e}")` 를 `logger.error(...)` 로 교체. 동작(반환값)은 불변. 선택적으로 test_sql_history.py 에 손상 JSON 로깅 회귀 테스트 1개 추가.
- [CC-012] sql_history.py 에 `_matches_history_id(entry, history_id) -> bool`(=`entry.get('id') == history_id or entry.get('timestamp') == history_id`) 헬퍼를 추가해 update_status(L109)·toggle_favorite(L281) 에서 사용. update_status_batch 는 중복 이중검사(`entry_id in ids_set or entry.get('timestamp') in ids_set`, L132)를 헬퍼 기반 `any(_matches_history_id(entry, hid) for hid in history_ids)` 로 정리(동작 동일 유지).
- [CC-015] search_advanced 필터를 `@dataclass HistorySearchFilter(keyword, date_from, date_to, success_only, favorites_only)` 로 묶고 필터링 로직을 `_apply_filters(history, filt)` 헬퍼로 이동. 하드 제약: search_advanced 의 기존 keyword 시그니처(keyword=None,...,limit=50, offset=0)는 절대 변경 금지 — src/ui/dialogs/sql_editor_history_dialog.py(L282, 이 WP 범위 밖)와 다수 테스트가 keyword 인자로 호출하므로, 메서드 내부에서 dataclass 를 구성해 헬퍼에 전달하는 back-compat 방식으로만 정리(외부 시그니처 유지).
- [CC-013] parse_sql_statement_ranges 의 while 루프에서 독립 스캔 규칙을 `_consume_line_comment`/`_consume_block_comment`/`_consume_dollar_quote`/`_consume_quoted_string` 등 헬퍼로 추출(커서 i·상태를 인자로 받아 갱신값 반환), 최상위 루프는 dispatcher 로 축소. parse_sql_statements/find_sql_statement_at_position/parse_sql_statement_ranges/read_dollar_quote 의 시그니처·동작은 100% 보존. LOW 이며 파서가 미묘하므로 정확한 동작 보존이 불확실하면 이 항목만 스킵 가능. test_sql_execution_worker.py 로 회귀 확인.
- [CC-036] 새 모듈 scripts/github_app_secret_codec.py 에 `OBFUSCATION_KEY`, `obfuscate()`, `deobfuscate()`, `generate_embedded_code()` 를 두어 빌드타임 도구를 분리. github_app_auth.py 의 generate_embedded_code/_obfuscate 는 `from scripts.github_app_secret_codec import ...` 를 함수 내부에서 lazy import 해 위임하는 얇은 classmethod wrapper 로 남겨 test_github_app_auth.py 와 GITHUB_APP_SETUP.md 호출 경로를 유지. 하드 제약: _deobfuscate 는 런타임 경로(_get_private_key 가 _EMBEDDED_PRIVATE_KEY 복호화에 사용)이므로 scripts import 없이 github_app_auth.py 내부에 그대로 구현 유지(패키징 exe 에 scripts/ 미포함). scripts/embed_github_credentials.py 는 이번 WP 에서 미변경.
- [CC-037] github_app_auth.py 에 `from src.core.logger import get_logger` + `logger = get_logger(__name__)` 추가하고 get_installation_token 의 `print(f"Installation Token 발급 실패: {e}")`(L248)를 `logger.error(...)` 로 교체(다른 core 모듈과 일관).
- [CC-038] 새 모듈 src/core/error_summary_builder.py 에 네트워크 의존 없는 순수 로직(sanitize_error_message/extract_core_error/generate_fingerprint/generate_issue_body/summarize_error)을 담는 `ErrorSummaryBuilder` 생성. GitHubIssueReporter 는 __init__ 에서 `self._summary = ErrorSummaryBuilder()` 를 보유하고 기존 _sanitize_error_message/_extract_core_error/_generate_fingerprint/_generate_issue_body/summarize_error 를 얇은 위임 메서드로 유지(테스트가 이 private 메서드들을 직접 호출함). 하드 제약: GitHubIssueReporter 클래스명과 public API(from_github_app/report_error/find_similar_issue/create_issue/add_comment)는 그대로 — src/core/__init__.py·src/ui/workers/github_worker.py·기존 테스트가 참조하므로 GitHubIssueClient 로의 rename/완전분리는 범위 밖.
- [CC-039][CC-040] github_issue_reporter.py 내부 정리: summarize_error 반환 dict 에 `summary['full_message'] = sanitized_message` 추가, add_comment 의 취약한 `summary.get('body','').split('## 상세 오류 메시지')[1].split('```')[1][:1000]`(L388)를 `(summary.get('full_message') or summary.get('core_error',''))[:COMMENT_PREVIEW_LEN]` 로 교체해 마크다운 포맷 결합 제거. 흩어진 truncation 리터럴을 클래스 상수 `TITLE_PREVIEW_LEN=80`(L101), `CORE_ERROR_MAX_LEN=100`(L178·L190), `BODY_PREVIEW_LEN=2000`(L204), `COMMENT_PREVIEW_LEN=1000`(L388)로 명명·참조. full_message 없는 summary 도 fallback 으로 기존 add_comment 테스트 통과.
- [CC-048] update_downloader.py download_installer 의 `requests.get(self.download_url, stream=True, timeout=30)`(L204)에서 `timeout=30` 하드코딩을 `timeout=self.timeout` 으로 교체해 설정 가능한 다운로드 타임아웃(get_network_timeout_download)을 적용, get_installer_info(L131)와 일관성 확보. 스트리밍 전용 타임아웃 근거가 없으므로 self.timeout 재사용이 기본. 선택적으로 test_update_downloader.py 에 download_installer 가 self.timeout 을 전달하는지 검증하는 테스트 추가.

**검증:**
- `python -m pytest tests/test_db_connector.py tests/test_sql_history.py tests/test_github_app_auth.py tests/test_github_issue_reporter.py tests/test_update_downloader.py tests/test_sql_execution_worker.py -q`
- `python -m pytest tests/test_github_app_secret_codec.py tests/test_error_summary_builder.py -q`
- `python -m pytest -q`
- `python -m py_compile src/core/db_connector.py src/core/postgres_connector.py src/core/sql_history.py src/core/sql_statement_parser.py src/core/github_app_auth.py src/core/github_issue_reporter.py src/core/update_downloader.py src/core/error_summary_builder.py scripts/github_app_secret_codec.py`

**리스크:**
- [CC-004] MySQLConnector 유닛테스트는 connect() 를 우회해 self.connector.connection 에 mock 을 직접 주입한다. 위임 방식은 delegate.connection 이 라이브여야 동작하므로, wrapper 안에서 `self._delegate.connection = self.connection` sync 라인을 반드시 넣어야 test_db_connector.py 수정 없이 통과한다. sync 라인을 생략하면 delegate 가 실제 facade 로 재연결을 시도해 get_schemas/schema_exists 테스트가 깨진다. `if not self.connection` 가드도 유지해야 test_get_schemas_no_connection 이 [] 를 반환.
- [CC-004] postgres 부분은 src/core/postgres_connector.py 를 수정한다. 이 파일에 대한 전용 유닛테스트(test_postgres_connector.py)가 없어 유닛 커버리지가 얇다 — 회귀는 미검증 상태이므로 최소한 수동 검토(가드+위임 동치) 필요. 다른 라운드/WP 가 postgres_connector.py 를 동시 수정하면 same-round 충돌 — 병렬 스케줄러가 이 파일을 이 WP 에만 배정하도록 보장할 것.
- [CC-015] search_advanced 는 src/ui/dialogs/sql_editor_history_dialog.py(L282, 범위 밖)와 test_sql_history.py 의 8개 테스트가 모두 keyword 인자로 호출한다. 시그니처를 `search_advanced(filter, limit, offset)` 로 바꾸면 다이얼로그가 깨지고 실행 에이전트가 범위 밖 파일을 고쳐야 해 중단해야 한다 — 반드시 기존 시그니처를 유지하고 dataclass 는 내부에서만 사용.
- [CC-036] _deobfuscate 는 런타임에 _EMBEDDED_PRIVATE_KEY 를 복호화하는 경로다. 이를 scripts/ 모듈로 옮기거나 scripts import 에 의존시키면, 패키징된 exe 에 scripts/ 가 포함되지 않아 임베디드 자격증명 빌드가 런타임에 깨진다(behavior 회귀). _deobfuscate 는 github_app_auth.py 내부 구현으로 남기고, 빌드타임 전용 _obfuscate/generate_embedded_code 만 lazy-import wrapper 로 분리. scripts/ 에는 __init__.py 가 없으나 repo 루트 기준 namespace package 로 pytest 에서는 import 가능(테스트는 repo 루트에서 실행됨).
- [CC-036] scripts/embed_github_credentials.py 가 이미 동일 난독화 로직 사본을 갖고 있다(중복). 이 WP 에서는 건드리지 않기로 함(범위·회귀 제한) — 신규 codec 모듈로의 통합은 후속 작업으로 남긴다.
- [CC-038] GitHubIssueReporter 를 ErrorSummaryBuilder + GitHubIssueClient 로 완전 분리(클래스 rename/치환)하면 src/core/__init__.py, src/ui/workers/github_worker.py, test_github_issue_reporter.py 가 모두 깨진다 — 범위 밖. GitHubIssueReporter 를 public facade 로 유지하고 순수 로직만 위임 추출하는 보수적 스코프로 한정.
- [CC-013] parse_sql_statement_ranges 는 DELIMITER/주석/dollar-quote/escape 를 공유 가변 상태로 처리하는 미묘한 state machine 이다. 헬퍼 추출 시 커서/상태 전달을 잘못하면 조용한 파싱 회귀가 발생. LOW 이므로 정확한 동치가 불확실하면 스킵 허용. test_sql_execution_worker.py + scheduler/sql_editor 경로로 회귀 확인.
- 프로젝트 메모리상 macOS validation/docs 관련 테스트(tests/test_macos_support_docs.py 등)는 로컬 pytest 에서 CI 의존으로 항상 실패하는 flaky 항목 — 전체 pytest 회귀 판정 시 무시. PyQt UI 테스트는 hang 위험이 있으므로 이 WP 의 core 파일 검증은 위 targeted pytest 로 우선 수행.
- github_issue_reporter.find_similar_issue(L307)도 print() 를 쓰지만 이번 findings 에 포함되지 않았다 — 범위 규율상 이번 WP 에서는 손대지 않음(scope creep 방지).

### WP-2.10 — dump-import-cleanup
**Branch:** `refactor/cc-r2-rust-dump-import-cleanup` · **Size:** L · **발견:** 10건 (H5/M2/L3) · **의존:** WP-1.8

**Findings covered:** CC-221, CC-222, CC-223, CC-224, CC-225, CC-226, CC-227, CC-228, CC-230, CC-236

**수정 파일:** `migration_core/src/dump.rs`, `migration_core/src/import.rs`
**테스트:** `migration_core/src/dump.rs`, `migration_core/src/import.rs`, `migration_core/tests/jsonl_cli.rs`

**가이드:**
- [CC-221] adaptive_dump_parallel_limits_with_avg(2019-2061)과 #[cfg(test)] 래퍼 adaptive_dump_parallel_limits(2004)를 삭제하고, dump_run의 호출(1706)을 dump_parallel_limits(threads, table_total) 직접 호출로 교체한다. heavy_tables/max_estimated_chunks 죽은 계산을 제거. row_counts/avg_row_lengths는 dump_schedule_order/dump_schedule_event에서 계속 쓰이므로 유지. 항상 baseline을 반환하던 동작이라 동작 보존 리팩터임.
- [CC-221] adaptive_* 를 호출하던 5개 테스트(dump_schedule_event_reports_adaptive_workers_and_top_tables 13388, adaptive_dump_limits_prioritize_range_workers_for_heavy_chunked_tables 13443, adaptive_dump_limits_use_byte_chunks_for_wide_tables 13456, adaptive_dump_limits_keep_table_parallelism_for_pathological_wide_table 13471, adaptive_dump_limits_keep_multiple_heavy_tables_in_parallel 13489)를 dump_parallel_limits(threads, table_total) 호출로 재지정(chunk_size/row_counts/avg 인자 제거). 단정값이 baseline과 동일하므로 그대로 통과. 이 테스트들은 dump.rs의 #[cfg(test)] mod tests 안에서만 수정해야 하며, WP-1.8이 dump_format.rs에 배치했다면 중단·재스케줄.
- [CC-222] dump.rs 내 dump_tables_parallel(디스패치 루프 2107-2198)·dump_tables_global_mysql(2330-2456)·dump_mysql_table_parallel_ranges(2598-2670)에 복제된 bounded worker-pool 루프를 제네릭 헬퍼 run_bounded_pool<E>(work: VecDeque, max_workers, spawn, on_event: FnMut(E)->PoolAction)->Result<(),String>로 통합하고 PoolAction{Continue, Fatal(String)} enum 도입. 각 함수는 이벤트 타입과 on_event 클로저만 공급. first_error 최초 캡처 순서·active 재충전 순서를 정확히 보존하고, global_mysql 아암의 state.chunks_done/rows_dumped 인라인 갱신을 클로저로 그대로 옮긴다(순수 기계적 통합).
- [CC-223] dump_run(1620-1859) 6단계를 헬퍼로 분리: parse_dump_run_options(request)->DumpRunOptions(옵션 파싱 1621-1665), select_dump_strategy(engine, threads, table_total)->DumpStrategy enum(1733-1800의 4-way if/else-if를 단일 match로 소비), finalize_dump_manifest(endpoint, schema, table_manifests, ..)(view 수집 + DumpManifest 조립/쓰기 1802-1839). dump_run은 순차 오케스트레이터(~40-60줄)로 축소하고 최종 result JSON만 조립. dump.rs 내부 변경만, dump_run의 pub 여부/JSONL 진입점 시그니처 불변.
- [CC-224] dump_import(3234-3533)를 분리: prepare_import_target(mode, tables, adapter, target_schema)(MySQL FK preflight + child-first drop-all 3335-3347), import_table_rows(adapter, table, table_manifest, data_format, compression, threads, ..)(테이블별 MySQL TSV fast-path vs generic chunk 분기 3369-3445), finalize_dump_import(..)(post-load DDL + row-count 검증 + view import + report 3458-3533). dump_import은 top validation 블록 + 위 헬퍼 호출의 얇은 시퀀스로. import.rs 내부 변경만, dump_import 시그니처 불변.
- [CC-225] dump_one_table(2945-3064)과 dump_one_mysql_table(3066-3214)의 공통 스캐폴딩(table_progress dumping/completed 이벤트, format!("{:04}_{}", index+1, safe_dump_component(&table.name)) 디렉토리 create_dir_all, DumpTableManifest 조립)을 run_table_dump_loop(table, index, table_total, output_path, request_id, emit, fetch_next_chunk: FnMut(u64)->Result<Option<ChunkOutcome>,String>)->Result<(DumpTableManifest,u64,u64),String>로 추출. ChunkOutcome{rows, chunk_name, checksum}. 두 함수는 chunk-fetch 클로저(generic write_dump_rows vs raw MySQL query_iter TSV)만 공급. dump.rs 내부 한정.
- [CC-226] dump.rs에 DumpJobContext 구조체(endpoint: Endpoint, output_path: PathBuf, chunk_size: usize, data_format: String, compression: String, request_id: Option<String>)를 DumpParallelLimits 정의(1975) 근처에 두고 &DumpJobContext로 전달. spawn_dump_table_worker(11 params)·dump_one_table(11)·dump_mysql_table_parallel_ranges(11)·dump_tables_sequential(9)를 컨텍스트+테이블별 인자(table, index, table_total, threads, emit)로 축소. dump_tables_parallel의 3개 호출부(2109/2143/2164) 및 1942/2510/2791/2894/2913 호출부 전부 갱신 — 모두 dump.rs 내부라 외부 파일 불필요.
- [CC-228] dump_import_row_progress_event의 뒤쪽 4개 Option<u64>(chunks_done, chunks_total, chunk_index, load_ms)를 ChunkProgress 구조체(named fields)로 대체. 4개 호출부(3419/3803/4166/4251)와 테스트 dump_import_row_progress_event_reports_table_and_overall_rows(13354)를 named-field 생성으로 갱신해 chunk_index의 이중 사용을 자기문서화. 주의: 이 함수 정의는 라인 1568(dump.rs 범위)이나 호출부는 전부 import.rs이므로 ChunkProgress를 pub(crate)로 정의하고 import.rs에서 use. WP-1.8이 함수를 실제로 배치한 모듈을 먼저 확인하고, dump.rs/import.rs 두 파일 밖이면 중단.
- [CC-230] import.rs에 MysqlImportChunkContext<'a>{table: &NormalizedTable, table_manifest: &DumpTableManifest, compression: &str, request_id: Option<String>, overall_rows_before: u64, overall_rows_total: u64}(Clone)를 도입해 import_mysql_tsv_table·import_mysql_tsv_table_insert_fallback·import_mysql_tsv_table_parallel 세 함수(공통 6-필드 클러스터)에 관통하고 3377/3691/3705/3728/3782 호출부 갱신. spawn_mysql_import_chunk_worker(7 params, 2필드만 공유)는 강제 편입하지 말고 좁은 시그니처 유지(필요 시 별도 소형 struct). import.rs 내부 한정.
- [CC-227 + CC-236 상수화 sweep] DEFAULT_DUMP_THREADS: usize = 8을 dump.rs 상단에 pub(crate)로 정의하고 dump_run(1640)·dump_import(3273)의 .unwrap_or(8)을 .unwrap_or(DEFAULT_DUMP_THREADS)로(import.rs는 use crate::dump::DEFAULT_DUMP_THREADS). import.rs 상단에 MYSQL_ERR_LOCAL_INFILE_DISABLED: &str = "3948", MYSQL_IMPORT_NET_TIMEOUT_SECS: u32 = 600, MYSQL_IMPORT_WAIT_TIMEOUT_SECS: u32 = 28800을 정의하고 is_mysql_local_infile_disabled_error(4003 contains)·mysql_import_session_tuning_sql(4084-4086)에 format!으로 주입. 골든 테스트 mysql_dump_import_uses_fast_session_tuning_statements(15791)가 SQL 문자열을 그대로 검증하므로 출력이 반드시 byte-identical("= 600", "= 28800")이어야 한다. 상수는 공용 상단 const 블록(15-27, WP-1.8이 adapters.rs로 이동)이 아니라 소유 모듈 상단에 둘 것.
- HARD CONSTRAINTS: 전부 동작 보존 리팩터(기능 변경 금지), 기존 import 경로·공개 시그니처 유지(lib.rs 재-export 루트 손대지 않음), 버전 bump 없음, DB 조작은 tunnelforge-core 소유 유지(Python DB 드라이버 핫패스 재도입 금지 — 본 WP는 Rust 전용). files_touched(dump.rs/import.rs) 밖 편집이 필요해지면(테스트가 dump_format.rs에 있음, 함수가 다른 모듈로 배치됨, 상수 블록이 adapters.rs에 있음 등) 즉시 중단하고 재스케줄 요청. LOW 발견(CC-227/CC-236)은 하나의 상수화 커밋으로 묶어도 됨.

**검증:**
- `cargo build --manifest-path migration_core/Cargo.toml --release`
- `cargo test --manifest-path migration_core/Cargo.toml`
- `cargo clippy --manifest-path migration_core/Cargo.toml --all-targets (죽은 코드 제거 후 미사용 import/warning 확인)`

**리스크:**
- dump_import_row_progress_event는 정의가 라인 1568(WP-1.8 기계적 분할 기준 dump.rs 범위 1344-3215)이지만 4개 비-테스트 호출부(3419/3803/4166/4251)와 테스트(13354)가 전부 import.rs 소속이다. WP-1.8이 이 함수를 dump.rs에 남기면 CC-228의 ChunkProgress 구조체를 pub(crate)로 두고 import.rs에서 use해야 한다. 두 파일 모두 scope 내라 처리 가능하나, 편집 전 WP-1.8의 실제 배치 모듈을 반드시 확인.
- CC-221 테스트(13388-13489)와 CC-228 테스트(13354)는 WP-1.8 계획이 'dump_format.rs/dump.rs'로 모호하게 배정한 12598-13905 범위에 있다. WP-1.8이 이 테스트를 dump.rs가 아닌 dump_format.rs에 co-locate하면 테스트 수정이 files_touched 밖을 건드리게 되어, 실행 에이전트는 중단하고 dump_format.rs를 scope에 추가하도록 재스케줄해야 한다(기대: 테스트는 대상 함수와 같은 dump.rs에 배치).
- 공용 최상단 MYSQL_* const 블록(라인 15-27)은 WP-1.8 기계적 분할 시 adapters.rs(1-728)로 이동한다. 따라서 CC-227/CC-236 신규 상수를 이 공용 블록에 추가하면 adapters.rs(scope 밖)를 건드린다 — 반드시 소유 모듈(dump.rs/import.rs) 상단에 정의할 것.
- CC-236 골든 테스트 mysql_dump_import_uses_fast_session_tuning_statements(15791)는 SQL 문자열 리터럴을 정확히 대조한다. 상수 interpolation(format!)이 'SET SESSION net_read_timeout = 600' 등과 byte-identical하지 않으면 테스트가 깨진다.
- CC-222 제네릭 pool-helper 통합: dump_tables_global_mysql의 이벤트 아암이 이미 drift됨(state.chunks_done/rows_dumped를 테이블별 인라인 갱신). run_bounded_pool의 on_event 클로저가 각 호출자의 정확한 bookkeeping(first_error 캡처 순서, active 재충전 타이밍, 인라인 상태 갱신)을 노출·보존해야 함 — 이벤트 순서 변화 시 진행률 리포팅이 달라질 수 있으므로 순수 기계적 통합 유지.
- 동일 라운드 파일 분리 가정: 다른 round-2 Rust WP(WP-2.11/2.12/2.13)가 dump.rs나 import.rs를 건드리지 않는다고 가정한다. 만약 겹치면 금지된 same-round overlap이므로 머지 전에 반드시 플래그.
- round-1 WP-1.8이 아직 dump.rs/import.rs를 생성하지 않았다(현재 lib.rs는 17006줄 단일 파일). 본 WP는 WP-1.8 완료를 강하게 전제하며, 미완료 시 실행 불가.

### WP-2.11 — query-schema-oneclick-cleanup
**Branch:** `refactor/cc-r2-rust-query-schema-oneclick-cleanup` · **Size:** M · **발견:** 7건 (H1/M4/L2) · **의존:** WP-1.8

**Findings covered:** CC-229, CC-231, CC-232, CC-233, CC-234, CC-235, CC-242

**수정 파일:** `migration_core/src/oneclick.rs`, `migration_core/src/schema.rs`, `migration_core/src/query.rs`
**테스트:** `migration_core/src/oneclick.rs`, `migration_core/src/schema.rs`, `migration_core/tests/jsonl_cli.rs`, `migration_core/tests/live_roundtrip.rs`

**가이드:**
- [전제·범위] WP-1.8 분할 완료를 가정한다. 모든 편집은 query.rs / schema.rs / oneclick.rs 세 모듈 파일 내부에서만 한다. lib.rs(재수출 루트)와 migrate.rs/protocol.rs/dump.rs/import.rs/compare.rs/ddl.rs 는 절대 수정하지 않는다. 공개 함수 시그니처는 pub 그대로 유지(공개 API 불변), 새로 뽑는 내부 헬퍼/구조체만 추가하며 크로스모듈 헬퍼는 pub(crate)로 두고 use 로 임포트한다. WP-1.8 이 함수를 다른 라인/파일에 배치했으면 라인번호 대신 함수명으로 재확인해 매핑한다.
- [CC-231] (HIGH) oneclick.rs 의 oneclick_run_streaming(약 214줄, 원본 5480-5693)을 단계 함수로 분해한다: preflight/analysis, execution(4분기 익명 6-tuple → 타입드 구조체), validation, final-report. execution 반환은 익명 6-tuple 대신 기존 OneClickApplyOutcome(oneclick.rs, 원본 6783; success_count/fail_count/log/applied_fixes)에 skip_count·disallowed_fix_attempts 필드를 추가해 재사용한다. oneclick_run_streaming 은 단계 호출 시퀀싱과 emit 만 담당하고, emit 이벤트의 순서·개수·필드는 바이트 단위로 동일하게 유지한다(동작 보존). OneClickApplyOutcome 를 리터럴로 생성하는 다른 위치/테스트가 있으면 새 필드에 맞춰 함께 갱신한다.
- [CC-229] (MEDIUM) oneclick_run_streaming(원본 5656-5668)과 oneclick_validate(원본 5978-5987)에 중복된 10줄 fallback MigrationIssue 생성 블록을 oneclick.rs 내부 헬퍼 issues_from_inspect_result(result: Result<InspectionResult, String>) -> Vec<MigrationIssue> 로 추출한다(Ok → oneclick_issues_from_inspection, Err → 단일 validation-error MigrationIssue). suggestion 문자열과 모든 필드값을 그대로 보존하고 두 호출부를 헬퍼 호출로 교체한다. CC-231 의 validation 단계 함수 안에서 이 헬퍼를 부르도록 함께 진행한다.
- [CC-232] (MEDIUM, 동작보존 주의) oneclick_apply_actions(원본 6797-6908)와 oneclick_dry_run_preview_fixes(원본 6910-6983)의 per-step 분류 로직(manual/skip 스킵, charset_issue+charset_collation_fk_safe, deprecated_engine+engine_innodb, 그 외 disallowed)을 공유 분류기 classify_oneclick_step(step, schema) -> OneClickStepClassification(enum: Skip / Disallowed(String) / Charset(Value) / Engine{table, sql})로 추출한다. 단 real-apply 전용인 sql_template 불일치 검사(원본 6878-6887)를 preview 경로에 그대로 추가하면 dry-run preview 출력이 바뀌는 '기능 변경'이 되므로, sql_template 검사는 oneclick_apply_actions(real) 경로에만 남긴다(분류기에 enforce_sql_template: bool 파라미터를 주거나 real 경로에서 분류 후 후처리). 즉 공통 분류만 공유하고 divergence 자체는 이 WP 에서 고치지 않는다. oneclick_apply_fixes dry-run 테스트(원본 12277-12341)로 preview 출력 불변을 확인한다.
- [CC-242] (LOW) oneclick.rs 의 oneclick_applied_fix_payload(원본 7026-7063)의 issue_type == "charset_issue" && strategy == "charset_collation_fk_safe" 문자열 비교 dispatch 를 enum OneClickPayloadShape { CharsetCollationFkSafe, SingleTable } + classify_oneclick_payload_shape(action: &OneClickApplyAction) -> OneClickPayloadShape 로 대체하고 match 로 분기한다. 출력 payload 의 구조·필드값은 완전히 동일하게 유지한다.
- [CC-235] (MEDIUM, 최고위험·독립커밋) schema.rs 의 inspect_mysql(원본 4828-4921)과 inspect_postgresql(원본 4967-5080)의 per-table 5단계 시퀀스(table_names → columns/keys/foreign_keys/indexes → apply_key_flags/group_indexes/group_foreign_keys → NormalizedTable push)를 trait InspectAdapter + MySQL/Postgres 래퍼 impl + 제네릭 inspect_generic<A: InspectAdapter>(adapter, schema) 로 통합한다. 모든 코드는 schema.rs 내부에 둔다. 드라이버 API 차이(mysql exec_map 클로저 vs postgres client.query + row.get 인덱스)는 각 impl 안에 캡슐화하고, DB 연결/쿼리는 tunnelforge-core(Rust) 소유 그대로 유지한다(Python DB 드라이버 hot path 재도입 금지). inspect 관련 단위 테스트(원본 16050-16474)로 SQL/정규화 회귀를 확인하고, 실 DB 왕복은 live_roundtrip.rs(env-gated)로만 잡히므로 머지 전 라이브 DB 실행을 권장한다. 다른 항목과 별도 커밋으로 분리한다.
- [CC-234] (MEDIUM, 동작보존 필수) 세 개의 수제 SQL 주석 스캐너를 query.rs 의 pub(crate) fn skip_sql_comment(bytes: &[u8], i: usize, allow_hash: bool) -> Option<usize> 하나로 통합한다(주석 시작이면 그 끝 다음 인덱스 반환, 아니면 None). 반드시 방언 커버리지를 보존한다: strip_leading_comments_and_parens(query.rs, 원본 4612)는 allow_hash=true, mysql_definition_has_residual_definer(schema.rs, 원본 5271)와 validate_single_view_statement(schema.rs, 원본 5315)는 allow_hash=false 로 호출해 두 검증기가 지금처럼 '#' 을 리터럴로 취급(더 보수적)하는 동작을 그대로 유지한다 — '#' 인식을 조용히 넓히면 보안 관련 View 정의 검증의 기능 변경이 된다. schema.rs 는 use crate::query::skip_sql_comment; 로 임포트한다. '(' 처리와 각 스캐너의 주변 로직(주석을 공백으로 치환 vs 스킵)은 그대로 둔다. validate_single_view_statement 테스트(원본 16930-17009, 특히 comment/string-literal 케이스)로 회귀를 검증하고, &str↔bytes 구조 차이로 바이트 단위 동작보존이 어려우면 통합을 강행하지 말고 이 항목을 보류한다.
- [CC-233] (LOW, sweep) 6개 함수에 반복되는 json!({"event":"error","request_id":request.request_id,"message":err}) 리터럴을 헬퍼로 통합한다. 주의: 실제 phase_event 는 migrate.rs(원본 7967, WP-2.12 도메인)에 있으므로 그 옆에 두면 안 된다. 대신 schema.rs 에 pub(crate) fn error_event(request: &Request, message: impl Into<String>) -> Value 를 정의한다(inspect 호출부가 schema.rs 이므로). schema.rs 의 inspect(원본 4784)와 oneclick.rs 의 5개 호출부(oneclick_preflight 5715 / oneclick_analyze 5743 / oneclick_derive_charset_contracts 5823 / oneclick_apply_fixes 5925 / oneclick_validate 6000)에서 이 헬퍼를 쓰며 oneclick.rs 는 use crate::schema::error_event; 로 임포트한다. 이 WP 범위 밖(dump.rs/import.rs/migrate.rs/protocol.rs)의 동일 리터럴은 건드리지 않는다.
- [검증·규율] 위험 항목(CC-235 trait, CC-234 스캐너)마다 개별 커밋 후 cargo build --release 와 cargo test 를 돌려 통과를 확인하고 다음 항목으로 넘어간다. 전부 동작보존 리팩터로 기능/출력/이벤트/payload 변화가 없어야 하며, 버전 bump 없음, 공개 시그니처 불변(내부 헬퍼·구조체만 추가)이다. 만약 어떤 수정이 세 모듈 파일(query.rs/schema.rs/oneclick.rs) 밖의 파일 편집을 요구하면 즉시 작업을 멈추고 리스케줄을 요청한다.

**검증:**
- `cargo build --manifest-path migration_core/Cargo.toml`
- `cargo build --manifest-path migration_core/Cargo.toml --release`
- `cargo test --manifest-path migration_core/Cargo.toml`

**리스크:**
- WP-1.8(모듈 분할) 완료가 하드 의존이다. 이 스펙은 WP-1.8 경계(query.rs 4454-4657 / schema.rs 4658-5441 / oneclick.rs 5442-7137)를 그대로 가정한다. WP-1.8 이 함수를 다른 파일/라인에 배치하면 함수명 기준으로 재매핑해야 한다.
- CC-233 크로스WP 함정: 실제 phase_event 는 migrate.rs(원본 7967, WP-2.12 소유)에 있다. 권고문의 '기존 phase_event 옆에 두라'를 그대로 따르면 migrate.rs 를 건드려 같은 라운드 WP-2.12 와 파일 충돌이 난다. 그래서 error_event 를 schema.rs 에 배치하도록 설계했다. 또한 error-이벤트 동일 리터럴은 dump.rs/import.rs/migrate.rs/protocol.rs 등 다른 WP 소유 파일에도 다수 존재(4783 외 760~7608 여러 위치)하나, 이 WP 는 명시된 6개 호출부(schema.rs inspect + oneclick.rs 5개)만 손대고 나머지는 각 WP 에 남긴다.
- CC-234 동작 변경 위험: 두 schema.rs 검증기는 현재 '#' 을 주석으로 취급하지 않고(더 보수적) query.rs 스캐너만 취급한다. 순진하게 하나로 합치면 보안 관련 View 정의 검증(residual DEFINER 탐지·멀티스테이트먼트 거부)의 동작이 바뀐다. allow_hash 파라미터로 사이트별 동작을 반드시 보존해야 하며, &str vs bytes 구조 차이로 바이트 단위 보존이 어려우면 이 항목은 보류가 안전하다. mysql_definition_has_residual_definer 에 직접 단위 테스트가 없어(sanitize_view_definition 테스트 경유 커버) 회귀 신호가 약할 수 있다.
- CC-232 동작 변경 위험: 권고문의 '두 경로에 sql_template 검사 통합'은 dry-run preview 출력을 바꾸는 기능 변경(현재 plannable → disallowed). 동작보존을 위해 sql_template 검사는 real-apply 경로에만 유지하도록 스펙을 좁혔다. divergence 자체를 실제로 고치려면 별도 사인오프가 필요한 별개 작업이다.
- CC-235 최고위험: 서로 다른 DB 드라이버 API(mysql::PooledConn exec_map vs postgres::Client row.get 인덱스) 위에 trait 추상화를 도입하는 비트리비얼 리팩터다. 동작이 완전 동일해야 하며 실 DB 왕복 회귀는 live_roundtrip.rs 로만 잡히는데, 이 테스트는 *_HOST/*_USER/*_DATABASE env 로 게이팅되어 로컬 cargo test 에서는 스킵된다(프로젝트 MEMORY: live 검증은 GitHub CI 의존/로컬 flaky). 로컬 단위 테스트(inspect_* 16050-16474)는 실 DB 를 치지 않으므로 커버리지가 제한적 — 머지 전 라이브 DB 실행 권장.
- CC-231 의 OneClickApplyOutcome 필드 확장(skip_count, disallowed_fix_attempts): 이 구조체는 oneclick.rs-로컬로 확인됨. 다만 co-located 테스트가 이 구조체를 리터럴로 생성하면 새 필드 추가로 컴파일 깨짐 → 실행 시 해당 테스트 생성부도 함께 갱신 필요. 마찬가지로 tuple → 구조체 전환이 emit 이벤트 계산에 영향 주지 않도록 순서/개수 검증 필수.
- 같은 라운드 파일 분리 확인: WP-2.10=dump.rs/import.rs, WP-2.12=migrate.rs/compare.rs/dump_format.rs/ddl.rs, WP-2.13=adapters.rs/protocol.rs 로 내 query.rs/schema.rs/oneclick.rs 와 겹치지 않는다. 단 WP-2.13 의 발견(CC-252/253)은 원본 14112-15997 의 테스트 블록인데 WP-1.8 은 테스트를 각 소유 모듈로 co-locate 한다 — 그 테스트가 어느 모듈로 가느냐에 따라 WP 간 test-block 경합 소지가 있으나 내 세 파일과는 직접 겹치지 않는 것으로 판단.

### WP-2.12 — migrate-ddl-compare-cleanup
**Branch:** `refactor/cc-r2-rust-migrate-ddl-compare-cleanup` · **Size:** L · **발견:** 16건 (H2/M10/L4) · **의존:** WP-1.8

**Findings covered:** CC-237, CC-238, CC-239, CC-240, CC-241, CC-243, CC-244, CC-245, CC-246, CC-247, CC-248, CC-249, CC-250, CC-251, CC-252, CC-253

**수정 파일:** `migration_core/src/migrate.rs`, `migration_core/src/dump_format.rs`, `migration_core/src/ddl.rs`
**테스트:** `migration_core/src/ddl.rs`, `migration_core/src/migrate.rs`, `migration_core/src/dump_format.rs`, `migration_core/tests/stress_rss.rs`, `migration_core/tests/live_roundtrip.rs`

**가이드:**
- [전제/모듈 매핑] 이 WP는 WP-1.8이 lib.rs(17,006줄)를 모듈로 분할한 뒤 실행된다. 대상 함수 위치: migrate.rs(구 7138-8790: migrate_streaming/verify/migrate_with_adapters_reporting/migration_error_result/verify_with_adapters_reporting), dump_format.rs(구 9147-10098: learned_mysql_range_chunk_size), ddl.rs(구 10099-11612: select_chunk_text_*/insert_rows_literal_*/sql_literal*/generate_table_ddl/is_safe_column_type/map_default_literal/is_valid_mysql_collation_ident). 라인번호는 분할 전 기준이므로 반드시 함수명 grep로 재확인. lib.rs(재수출 루트)와 compare.rs(이 WP 발견 없음)는 절대 수정 금지 — 모든 변경은 위 세 모듈 내부에서만.
- [CC-237] ddl.rs: select_chunk_text_sql/select_chunk_text_after_key_sql/select_chunk_text_range_sql에 3회 복붙된 28줄 컬럼 프로젝션 클로저(binary hex-encode / postgres ::text / mysql passthrough / CAST AS CHAR)를 `fn projected_text_columns_sql(engine: &str, table: &NormalizedTable) -> String`(private)로 추출. 기존 is_binary_type, quote_ident 재사용. 세 함수는 이 헬퍼로 SELECT 프로젝션을 만든 뒤 각자의 WHERE/ORDER BY/LIMIT만 덧붙인다. 세 함수의 pub 시그니처 불변. 기존 테스트(select_chunk_text_sql 15762/15886, range 15776)로 출력 문자열 동등성 확인.
- [CC-244][CC-245] ddl.rs: 보안 fail-closed 가드인 is_safe_column_type(~198줄 바이트 파서)를 작은 파서 헬퍼로 분해 — parse_base_ident / parse_quoted_string_list / parse_numeric_list / parse_modifier_word 각각 진행된 index를 Option<usize>로 반환하고, is_safe_column_type은 짧은 오케스트레이션으로. 분해 과정에서 CC-245의 암호 같은 지역변수(ds/ws/ws2/is2/ss)를 의미 이름(numeric_list_start, varying_length_start, modifier_word_start, time_zone_word_start, charset_or_collate_value_start, set_keyword_start, character_set_name_start)으로 교체(대부분 헬퍼 내부 지역변수로 자연 흡수). 동작 절대 불변: is_safe_column_type_accepts_normal_types(16158)/rejects_injection(16185)이 그대로 통과해야 하며, 분해 전후 동일 입력집합에 대한 동등성 단위테스트 추가 권장.
- [CC-249] ddl.rs: generate_table_ddl(77줄, 3책임)을 `fn column_ddl_lines(table, source, target) -> Option<(Vec<String>, Vec<String>)>`(컬럼 DDL 라인 + PK 컬럼 목록, job1+2)과 `fn mysql_table_collation_suffix(source, target, table) -> Option<String>`(job3)로 분리. is_safe_column_type / is_valid_mysql_collation_ident의 fail-closed `return None` 경로를 각 헬퍼 안에 그대로 보존하고 generate_table_ddl은 둘을 호출해 최종 CREATE TABLE 조립. 보안 테스트 generate_table_ddl_rejects_injection_*(16115/16211/16228) 및 collation 테스트(16087~16329)가 그대로 green이어야 함.
- [CC-246] ddl.rs: insert_rows_literal_sql와 insert_rows_literal_sql_for_table의 INSERT 조립 로직을 `fn insert_values_sql(rows: &[Value], column_names: &[&str], literal_for: impl Fn(&str,&Value)->String) -> String`(private)로 통합. 두 public 함수는 컬럼명 목록(&[String] vs NormalizedTable.columns)과 리터럴 클로저(sql_literal / sql_literal_for_column)만 준비해 위임하고, `INSERT INTO {} ({}) VALUES {}` 최종 조립은 헬퍼가 담당. NULL/Value::Object 처리 규칙 단일화. 시그니처/공개성 불변. 테스트 15894/15927/15958.
- [CC-247][CC-250] ddl.rs: sql_literal_for_column의 String 경로 말미(10804-10809)와 비-String 경로(10811-10817)에 중복된 mysql-json/mysql/fallback 3-way 분기를 `fn mysql_or_generic_literal(target_engine: &str, source_type: &str, value: &Value) -> String`로 추출해 두 곳에서 각각 1회 호출(postgres 특수 케이스는 String 경로에 그대로 유지). 또한 sql_literal과 mysql_sql_literal의 동일한 Null/Bool/Number arm을 `fn generic_sql_literal(value: &Value, escape: impl Fn(&str)->String) -> String`로 통합(sql_literal은 작은따옴표 doubling 클로저, mysql_sql_literal은 mysql_string_literal을 escape로 전달). 동작 완전 동일.
- [CC-248] ddl.rs (최우선 리스크 / optional-medium): copy_csv_field_for_column / sql_literal_for_column / map_default_literal에 흩어진 반복 `if target_engine == "mysql"|"postgresql"` 엔진 분기를 정리. 완전한 SqlDialect trait(+Mysql/Postgresql impl) 도입이 부담되면 범위를 축소해 CC-247의 mysql_or_generic_literal 등 공유 per-engine 헬퍼 재사용만으로 중복 제거하고 동작을 100% 보존. 반드시 CC-246/247/250 헬퍼 추출을 먼저 완료한 뒤 마지막에 얹어 같은 함수 내 편집 충돌 회피. 조금이라도 엔진별 캐스팅 동작이 바뀔 위험이 보이면 헬퍼-추출 형태로 남기고 trait 미도입.
- [CC-238] migrate.rs: migrate_with_adapters_reporting(~168줄)의 테이블별 복사 본문(create_table + keyset/offset 페이지네이션 결정 + 청크 read/insert 루프 + progress 이벤트 emit, 구 8217-8310)을 `fn copy_table_rows<S: MigrationAdapter, T: MigrationAdapter, F: FnMut(Value)>(...) -> Result<TableCopyOutcome, MigrationResult>`(private, migrate.rs 내부에 작은 TableCopyOutcome 구조체 신설)로 추출. 상위 함수는 validate -> build ddl -> for each table copy_table_rows -> apply_post_load_ddl 흐름으로 축약. cancel_after_chunks 검사, progress 이벤트의 순서·payload·카운트를 동일하게 유지.
- [CC-241] migrate.rs: migration_error_result 시그니처를 `table: &NormalizedTable` -> `location: &str`로 변경(내부 유일 사용 `location: table.name.clone()` -> `location.to_string()`). 가짜 NormalizedTable을 조립하던 2곳(구 8201-8212 schema_ddl, 8314-8325 post_data_ddl)은 문자열 리터럴 `"schema_ddl"`/`"post_data_ddl"`를, 나머지 3 호출부(구 8231/8254/8276)는 `&table.name`을 전달. migration_error_result와 그 5개 호출부가 모두 migrate.rs 내부(private)임을 grep로 재확인했으므로 외부 파일 영향 없음 — 진행 전 재확인.
- [CC-240] migrate.rs: migrate_streaming과 verify에 4회 복제된 endpoint 해석 보일러플레이트(+`unreachable!()` 랜드마인)를 `fn required_endpoint(payload: &Value, key: &str) -> Result<Endpoint, String>`로 추출 — 내부에서 `.get(key)` 부재를 `unreachable!()` 대신 실제 `Err(...)`로 처리해 패닉 경로 완전 제거. 호출부는 `let ep = match required_endpoint(&request.payload, "source") { Ok(e)=>e, Err(err)=>{ /*기존 그대로*/ emit(...) 또는 events.push(...); return; } };` 형태로, emit vs events.push 차이는 각 호출부 error arm에 그대로 둔다. endpoint_from_value/Endpoint는 migrate_streaming이 이미 사용 중이라 migrate.rs 스코프에 존재(WP-1.8이 use 구성).
- [CC-239] migrate.rs: verify_with_adapters_reporting(~168줄)을 `fn verify_table_by_digest(source, target, table, chunk_size, emit) -> Vec<Value>`와 `fn verify_table_by_keyset(source, target, table, key_columns, chunk_size, total_rows, emit) -> Vec<Value>`로 분리(각각 mismatch 반환). 외곽 루프는 양측 count 체크 후 `key_columns.is_empty()`로 두 전략을 디스패치. table_progress/row_progress bookkeeping을 각 전략 헬퍼로 이동하되 emit 순서/카운트/payload 불변. 회귀 게이트: verify_with_adapters 관련 테스트(15303/15319/15391) + 통합테스트 stress_rss.rs(verify_with_adapters 호출).
- [CC-243][CC-251] + HARD CONSTRAINTS sweep: 매직값을 사용처 모듈 내부 상수로 승격 — dump_format.rs 최상단에 `const LEARNED_PROFILE_LARGE_ROW_BYTES: u64 = 4_096;`(대형행 테이블은 학습된 chunk_rows 프로파일을 신뢰하는 이유 1줄 주석, learned_mysql_range_chunk_size에서 참조), ddl.rs 최상단에 `const MYSQL_IDENTIFIER_MAX_LEN: usize = 64;`와 `const MAX_COLUMN_TYPE_LEN: usize = 512;`(각 1줄 근거 주석, is_valid_mysql_collation_ident/is_safe_column_type에서 참조). 경고: lib.rs 17-27줄 상수 블록에 넣지 말 것(재수출 루트 불변) — 반드시 각 모듈 내부 정의. 전역 원칙: 동작 보존 리팩터만(기능 변화·버전 bump 금지), 추출 헬퍼는 전부 module-private `fn`으로 두고 기존 pub 함수의 시그니처/공개성 불변 유지(→ 소비자·테스트 파일 미변경), DB 조작 소유권은 tunnelforge-core 그대로(Python DB 드라이버 재도입 금지 — 해당 없음, Rust 내부 SQL 문자열 생성 정리만). files_touched/new_files 밖 파일 편집이 필요해지면 즉시 중단하고 리스케줄 요청.
- [리뷰반영/구WP-2.13 흡수] CC-252(insert_rows_literal_sql_for_table 리터럴 중복 테스트 → ddl.rs)와 CC-253(bigint-PK range-dump 리터럴 → dump_format.rs, auto_increment/기본값 리터럴 → ddl.rs)을 본 WP로 이관. 원래 WP-2.13(adapters.rs 전용)은 대상 함수가 ddl/dump_format 모듈에 있어 실질 no-op이 되므로 폐지하고 본 WP가 흡수.

**검증:**
- `cargo build --manifest-path migration_core/Cargo.toml --release`
- `cargo test --manifest-path migration_core/Cargo.toml`
- `cargo test --manifest-path migration_core/Cargo.toml --test stress_rss`
- `cargo test --manifest-path migration_core/Cargo.toml --test live_roundtrip`
- `python -m pytest -q`

**리스크:**
- WP-1.8 의존: 분할 시 migrate.rs가 endpoint_from_value/Endpoint(구 4805/126, schema·adapters 클러스터)와 NormalizedTable/MigrationResult/MigrationAdapter(adapters 클러스터)를, ddl.rs가 is_binary_type/quote_ident/is_json_type/mysql_string_literal 등을 올바른 `use crate::...`로 import했어야 컴파일됨. 추출 착수 전 각 모듈에 필요한 심볼이 스코프에 있는지 먼저 확인. import 누락이면 WP-1.8 결함으로 보고.
- CC-248(엔진 dispatch/dialect trait)이 최고 위험: copy_csv_field_for_column/sql_literal_for_column/map_default_literal의 tinyint(1)<->boolean 캐스팅, JSON/BIT 리터럴, 문자열 이스케이프 규칙을 미묘하게 바꾸기 쉬움. 최소 공유 헬퍼 방식 권장, 의심 시 미도입. CC-246/247/250과 같은 함수를 건드리므로 반드시 그 헬퍼 추출을 먼저 끝낸 뒤 착수.
- 보안 크리티컬 fail-closed 가드(is_safe_column_type, generate_table_ddl, is_valid_mysql_collation_ident)의 파서 분해(CC-244/245/249)는 accept/reject 의미를 정확히 보존해야 함. 주입 테스트(16115/16145/16158/16185/16211/16228/16251/16327)가 하드 게이트 — 분해 전후 동등성 테스트 추가로 안전망 확보.
- 새 상수(CC-243/CC-251)는 반드시 dump_format.rs/ddl.rs 내부에 정의. 원문 권고의 'lib.rs 17-27줄 옆에 배치'를 그대로 따르면 재수출 루트 lib.rs를 편집하게 되어 round-2 규칙 위반 — 절대 금지.
- pub 함수(select_chunk_text_*, insert_rows_literal_*, sql_literal, is_binary_type, migrate_with_adapters, verify_with_adapters)는 통합테스트 stress_rss.rs가 소비 → 시그니처/공개성 반드시 불변, 내부만 리팩터. migration_error_result는 private+migrate.rs 내부 5개 호출부뿐임을 확인했으나 WP-1.8이 실수로 pub(crate)로 노출하고 타 모듈에서 호출하면 시그니처 변경이 files_touched 밖에 영향 → 그 경우 중단·리스케줄.
- 동일 라운드 파일 분리 확인됨(충돌 없음): WP-2.10=dump.rs/import.rs, WP-2.11=query/schema/oneclick.rs, WP-2.13=adapters/protocol.rs. 이 WP는 migrate/dump_format/ddl.rs만 수정. compare.rs는 theme에 있으나 발견이 없어 미수정(중복 위험 회피). live_roundtrip.rs는 실 DB 연결을 요구할 수 있어 로컬에서 스킵/무시될 수 있음(회귀 판정 시 CI 결과 우선).

---

## Round 3 — UI 다이얼로그 / 메인윈도우 / 워커

### WP-3.1 — editor-decomposition
**Branch:** `refactor/cc-r3-sql-editor-decomposition` · **Size:** L · **발견:** 12건 (H1/M4/L7)

**Findings covered:** CC-112, CC-113, CC-114, CC-115, CC-116, CC-117, CC-118, CC-119, CC-120, CC-121, CC-122, CC-123

**수정 파일:** `src/ui/dialogs/sql_editor_dialog.py`, `src/ui/dialogs/sql_editor_workers.py`, `src/ui/dialogs/sql_editor_code_editor.py`, `src/ui/dialogs/sql_editor_highlighters.py`, `src/ui/dialogs/sql_editor_history_dialog.py`
**신규 파일:** `src/ui/dialogs/sql_editor_editability.py`
**테스트:** `tests/test_sql_editor_dialog.py`, `tests/test_sql_editor_editability.py (new)`

**가이드:**
- [CC-112] god-class 분해 원칙 = 동작 보존 + 위임 유지. 순수(UI 비의존) SQL 로직만 새 모듈 src/ui/dialogs/sql_editor_editability.py로 추출한다: analyze_query_editability(query) -> dict|None (현재 1833-1888의 정규식 분석 로직 그대로 이동), quote_editor_identifier(engine, name) -> str (2452-2455 로직을 engine 파라미터화), PK 조회 SQL은 엔진별 상수 또는 build_primary_key_query(engine, has_schema)로. 다이얼로그의 _analyze_query_editability / _quote_editor_identifier 는 동일 시그니처의 얇은 위임 메서드로 남겨 기존 호출부·테스트가 그대로 통과하게 한다.
- [CC-112] _fetch_primary_keys(self, schema, table)(1890-1941)와 _execute_cell_edits_in_txn(self, cursor, table_edits)(2072-2121)는 self.db_connection.cursor()(Rust 커넥션)와 Qt 테이블 아이템에 강결합되어 있으므로 다이얼로그에 그대로 두되 내부의 SQL 문자열 생성·식별자 인용만 위 모듈 함수를 호출하도록 바꾼다. 경고: DB 실행 경로(self.db_connection = RustDbConnection의 cursor)는 절대 바꾸지 말 것 — 스키마 RPC 등으로 재작성하면 동작 변경이며 tunnelforge-core 소유 원칙 위반이자 회귀 위험이다.
- [CC-112] 연결/트랜잭션 생명주기를 통째로 SqlEditorTransactionSession 별도 클래스로 이관하지 않는다(하려면 위임 계층만). tests/test_sql_editor_dialog.py가 dialog.db_connection, dialog.pending_queries, dialog._connected_target, dialog._persistent_temp_server, dialog._autocommit_temp_server, dialog._do_commit(), dialog._ensure_connection(), dialog._close_db_connection(), dialog._on_postgres_transaction_rolled_back() 를 직접 호출·검사한다. 이 속성/메서드는 반드시 다이얼로그에 유지하며, 분해가 이 계약을 깨면 중단하고 위임 방식으로 되돌린다(files_touched 밖 수정 금지).
- [CC-113] init_ui(233-588)를 영역별 빌더 _build_connection_bar / _build_toolbar / _build_editor_panel / _build_result_panel / _build_transaction_panel / _build_status_bar 로 분할하고 init_ui는 조립만 담당하게 한다. 반복 인라인 QSS는 모듈 상수(PRIMARY_BUTTON_QSS, TX_PANEL_QSS 등)로 추출. 위젯 속성명(message_text, message_summary, btn_toggle_message, schema_tree, db_combo, db_selector_label, result_tabs, editor_tabs, editor, btn_commit, btn_rollback, auto_commit_check, validation_label 등)은 테스트가 참조하므로 동일하게 유지한다.
- [CC-114][CC-115] _do_commit(1508-1609)에서 스키마 그룹핑+ProductionGuard 확인 루프를 _confirm_cell_edit_commit(table_edits) -> bool 로, 커밋 실행 try/except 본문을 _apply_commit(table_edits) 로 추출해 _do_commit을 가드→확인→적용→보고 흐름으로 축소한다. 동시에 _do_commit(1588-1592)과 _do_rollback(1626-1631)에 중복된 '쿼리 N건, 셀 편집 N건' 요약 생성을 _describe_pending_changes(pending_count, cell_edit_count) -> str 헬퍼로 통일. 가드 실패 시 QMessageBox.warning·커밋 차단 등 public 동작은 그대로 유지.
- [CC-116] SQL 미리보기 절단 헬퍼 truncate_sql_preview(text, length=60) -> str 를 sql_editor_workers.py에 module-level로 추가하고 워커 run()의 query[:100](183행)에 적용. 다이얼로그(992/1065/1383행)는 이 헬퍼를 import해 사용하되 서로 다른 길이를 의도적 상수로 보존한다: DANGER_CONFIRM_PREVIEW_LEN=200, TX_QUERY_PREVIEW_LEN=60, PENDING_PREVIEW_LEN=50, 워커는 WORKER_PROGRESS_PREVIEW_LEN=100. 길이값을 그대로 유지해야 동작 보존.
- [CC-117] 다이얼로그 module-level 상수 도입 후 호출부 교체: MAX_AUTO_COLUMN_WIDTH_PX=400 (_add_result_table 1213-1214), RESULT_ROW_HEIGHT_PX=28 (1220), ELAPSED_TIMER_INTERVAL_MS=100 (_set_executing_state 1716). 값 변경 없이 리터럴만 상수 참조로 치환.
- [CC-118] 죽은 섹션 구분 주석(72-124행, 이미 다른 파일로 이관된 클래스 자리)을 삭제한다. 단 상단 import 블록(38-52행)의 재노출 import는 절대 제거 금지 — 테스트가 sql_editor_dialog.SQLQueryWorker / SQLTransactionExecutionWorker 를 monkeypatch하고 SQLEditorTab / LARGE_SQL_RENDER_LIMIT_BYTES / format_metadata_db_version 를 이 모듈에서 import하므로 재노출 경로를 유지해야 한다.
- [CC-121] sql_editor_workers.py에 _rows_from_cursor(cursor) -> tuple[list, list] 를 module-level로 추출(현재 108-116 / 192-199의 columns=[desc[0]...]; rows=cursor.fetchall(); dict/tuple→row_list 변환 블록)하고 SQLQueryWorker.run 과 SQLTransactionExecutionWorker.run 양쪽에서 호출한다. 반환 형태·순서 동일 유지.
- [CC-123] @dataclass ConnectionParams(engine, host, port, user, password, database=None, schema=None) 를 sql_editor_workers.py에 도입. 하위호환 필수: SQLQueryWorker.__init__(self, host, port, user, password, database, queries, engine='mysql', schema=None) positional 시그니처를 유지하고(테스트가 positional로 생성: SQLQueryWorker('127.0.0.1', 3306, 'user', 'pass', 'db', [...])) 내부에서 ConnectionParams를 구성해 흐르게 한다. create_sql_editor_connector 는 connector_from_params(params) 동반 함수를 추가하되 기존 primitive 시그니처도 보존, dialog._create_db_connector 도 시그니처 유지. 시그니처를 정말 바꾸려면 tests/test_sql_editor_dialog.py의 해당 positional 호출 1곳(약 638행)만 함께 갱신(파일이 WP 범위 내).
- [CC-119] SQLEditorTab(code_editor.py)에 _apply_text(self, text) private 메서드를 추출해 set_content(403-410)와 load_file(416-430)의 공통 블록(_set_large_document_mode_for_text · editor.blockSignals · setPlainText · is_modified=False · title_changed.emit)을 공유한다. load_file은 추가로 file 읽기 try/except와 self.file_path 설정만 유지하고, 실패 시 False 반환 동작 보존.
- [CC-120][CC-122] (sweep) CC-120: sql_editor_highlighters.py의 from src.core.sql_validator import IssueSeverity 를 _build_issue_map 루프 내부(152행)에서 파일 상단 import(4행 근처)로 이동. CC-122: sql_editor_history_dialog.py에 _reset_list_state(is_searching: bool) 헬퍼(list_widget.clear + _history_items.clear + current_offset=0 + _is_searching 설정)를 추출해 load_history(218-223, is_searching=False 이후 _update_fav_count 호출)와 _do_search(336-341, is_searching=True)에서 재사용.

**검증:**
- `python -m py_compile src/ui/dialogs/sql_editor_dialog.py src/ui/dialogs/sql_editor_workers.py src/ui/dialogs/sql_editor_code_editor.py src/ui/dialogs/sql_editor_highlighters.py src/ui/dialogs/sql_editor_history_dialog.py src/ui/dialogs/sql_editor_editability.py`
- `python -m pytest tests/test_sql_editor_dialog.py -q`
- `python -m pytest tests/test_sql_editor_editability.py -q`
- `python -m pytest -q`

**리스크:**
- 높은 테스트 강결합: tests/test_sql_editor_dialog.py(약 30개 테스트)가 다이얼로그의 private 메서드(_do_commit, _fetch_primary_keys, _ensure_connection, _close_db_connection, _on_postgres_transaction_rolled_back, _execute_with_autocommit, _on_query_result, _on_transaction_query_result, _add_result_table, _clear_result_tabs, closeEvent, _retire_worker, _load_metadata, _apply_limit, _get_query_at_cursor, _split_queries, _on_metadata_loaded)와 인스턴스 속성(db_connection, pending_queries, _connected_target, _persistent_temp_server, _autocommit_temp_server, 각종 위젯)을 직접 참조한다. 이들을 다이얼로그 밖으로 옮기면 회귀 발생 — 반드시 다이얼로그에 유지하거나 위임한다.
- CC-123 시그니처 변경 위험: SQLQueryWorker는 테스트에서 정규명(src.ui.dialogs.sql_editor_dialog.SQLQueryWorker)으로 monkeypatch되고 약 638행에서 positional primitive로 인스턴스화된다. __init__ positional 시그니처를 바꾸면 이 테스트가 깨지므로 하위호환(내부 dataclass 구성) 방식 권장.
- test_sql_edit_dialog의 inspect.getsource(SQLEditorDialog) 검사(약 869행)가 'def _execute_query_in_thread' 문자열 부재를 단언한다. 메서드 추출은 무방하나 해당 이름을 재도입하지 말 것.
- 재노출(re-export) 유지 필수: dialog 38-52행 import가 SQLEditorTab/LARGE_SQL_RENDER_LIMIT_BYTES/format_metadata_db_version/SQLQueryWorker/SQLTransactionExecutionWorker/create_sql_editor_connector 를 모듈 네임스페이스에 노출한다. CC-118 주석 삭제 시 이 import를 함께 지우면 테스트 import·monkeypatch가 실패한다.
- DB 실행 경로 보존: _fetch_primary_keys / _execute_cell_edits_in_txn 는 이미 Rust 커넥션 cursor로 information_schema/UPDATE를 실행한다(파이썬 DB 드라이버 아님). 실행 방식을 다른 경로로 재작성하면 동작 변경이자 CLAUDE.md의 tunnelforge-core 소유 원칙 위반 — 문자열 빌드만 모듈화하고 실행부는 그대로 둘 것.
- 코드 소비자 팬아웃은 작음: sql_editor_* 위성 파일과 워커의 실제 코드 소비자는 sql_editor_dialog.py와 tests/test_sql_editor_dialog.py뿐이며(그 외 매치는 .claude/investigation-* 및 docs/current_status.md 감사 문서), 재노출 유지 시 소비자 파일을 건드릴 필요가 없다.
- 동일 라운드 파일 중복 없음 예상: 본 WP는 src/ui/dialogs/sql_editor_* 5개 파일 + 신규 editability 모듈 + 자체 테스트에만 국한된다. 다른 round-3 UI WP가 이 5개 파일을 건드리면 안 되며, 만약 겹치면 머지 충돌 위험이므로 사전 조율 필요.

### WP-3.2 — export-import-dialogs
**Branch:** `refactor/cc-r3-db-export-import-dialogs` · **Size:** L · **발견:** 17건 (H2/M12/L3)

**Findings covered:** CC-126, CC-127, CC-128, CC-129, CC-130, CC-131, CC-132, CC-133, CC-134, CC-135, CC-136, CC-137, CC-138, CC-139, CC-140, CC-141, CC-142

**수정 파일:** `src/ui/dialogs/db_export_dialog.py`, `src/ui/dialogs/db_import_dialog.py`, `src/core/constants.py`, `src/ui/workers/github_worker.py`, `src/exporters/rust_dump_exporter.py`, `tests/test_db_export_dialog.py`, `tests/test_db_import_dialog.py`
**신규 파일:** `src/core/path_safety.py`, `src/ui/dialogs/collapsible_config_dialog.py`, `tests/test_path_safety.py`
**테스트:** `tests/test_db_export_dialog.py`, `tests/test_db_import_dialog.py`, `tests/test_db_dialogs.py`, `tests/test_rust_dump_exporter.py`, `tests/test_path_safety.py (new)`

**가이드:**
- [CC-129] src/core/path_safety.py 신설: safe_component/safe_join 중첩 클로저(db_export_dialog.py 656-673)를 순수 함수 safe_output_dir(base_dir: str, folder_name: str) -> str 로 추출한다. 문자 제거 규칙(: / \ * ? " < > | strip '.'), Path.expanduser().resolve() + is_relative_to() 탈출 방지, fallback 'export_%Y%m%d_%H%M%S' 로직을 문자열 입출력만으로 재현한다. _generate_output_dir 는 self.radio_manual_naming/self.chk_* Qt 위젯 상태를 읽어 folder_name 문자열만 조립한 뒤 safe_output_dir 를 호출하도록 바꾼다. 반드시 test_rust_dump_export_dialog_rejects_parent_manual_folder(입력 '..' → tmp_path 내부 유지)와 동일 결과를 내야 한다. tests/test_path_safety.py 신설로 순수 함수를 단위 테스트한다.
- [CC-126][CC-127][CC-128] db_export_dialog.py 내부 분해(공개 API 불변): init_ui(212-602)를 _build_status_group()/_build_export_type_group()/_build_schema_section()/_build_output_folder_group()/_build_progress_section()/_build_button_row() private 빌더로 쪼개고, 각 빌더는 기존과 동일한 self.<위젯명>(예: self.combo_compression, self.radio_full, self.txt_log, self.btn_save_log, self.spin_threads)에 그대로 대입한 뒤 groupbox/widget 을 반환해 init_ui 가 splitter/layout 으로 조립만 하게 한다. do_export(877-1008)는 _reset_export_state()/_resolve_output_dir(schema)/_build_worker(schema, output_dir) 로 추출한다. RustDumpConfig/worker kwargs/6개 시그널 연결 순서는 그대로 유지 — do_export/_generate_output_dir 는 계속 인스턴스 메서드로 남겨 테스트가 직접 호출 가능해야 한다.
- [CC-136][CC-137][CC-138] db_import_dialog.py 내부 분해: init_ui(267-671)를 _build_status_group()/_build_input_dir_group()/_build_upgrade_check_group()/_build_schema_group()/_build_timezone_group()/_build_import_mode_group()/_build_progress_section() 로 쪼갠다. do_import(946-1128)에서 타임존 SQL 결정부(1058-1092)를 모듈 레벨 순수 함수 resolve_timezone_sql(engine: str, tz_mode: str) -> Optional[str] 로 추출(auto→mysql 은 check_timezone_support 결과로 분기, postgresql→None; kst/utc 는 engine 별 SET TIME ZONE/SET SESSION time_zone 문자열)하고, ProductionGuard 호출부(968-983)는 _confirm_production_guard(input_dir, target_schema)->bool 로 감싼다. test_postgresql_import_auto_timezone_skips_mysql_detection / _forced_kst_uses_postgresql_timezone_sql 결과(auto+pg→None, kst+pg→"SET TIME ZONE '+09:00'")를 정확히 재현해야 한다.
- [CC-131] src/ui/dialogs/collapsible_config_dialog.py 신설: toggle_config_section/collapse_config_section/expand_config_section(db_export_dialog.py 604-634 == db_import_dialog.py 673-703, '🔼 설정 펼치기'/'🔽 설정 접기' 라벨, 60:40/10:90 splitter 비율)을 CollapsibleConfigDialog 믹스인으로 이관한다. 믹스인은 self.splitter/self.config_container/self.btn_collapse 속성 계약만 요구하고 QDialog 를 상속하지 않는다. 두 다이얼로그 선언을 class RustDumpExportDialog(CollapsibleConfigDialog, QDialog) 형태로 바꿔 MRO 상 믹스인이 우선하게 한다. db_dialogs.py 는 클래스만 re-export 하므로 수정 불필요.
- [CC-132] src/ui/workers/github_worker.py 에 GithubReportingMixin 추가: 워커 spawn tail(GitHubReportWorker 생성→self._github_workers.append→finished lambda 연결→start)과 _on_github_report_finished 를 믹스인 메서드 _start_github_report_worker(error_type, message, context) / _on_github_report_finished 로 통합한다. 믹스인은 github_worker.py 모듈에 두어 monkeypatch(src.ui.workers.github_worker.GitHubReportWorker)가 호출 시점에 반영되도록 모듈 전역 이름을 참조한다. 각 다이얼로그의 _report_error_to_github 는 서로 다른 context(export: schema/tables/mode; import: 실패 테이블 집계 + error_count 기본 인자 + combined_error)를 그대로 만든 뒤 _start_github_report_worker 만 호출하도록 남긴다 — 시그니처 _report_error_to_github(self, error_type, error_message, [error_count=0]) 와 self._github_workers 리스트는 test_*_github_workers_are_retained_until_finished 가 검증하므로 반드시 보존.
- [CC-133] src/exporters/rust_dump_exporter.py 에 build_rust_dump_config(connector) -> RustDumpConfig 팩토리를 추가한다. host=getattr(connector,'host',DEFAULT_LOCAL_HOST), port=connector.port if hasattr else DEFAULT_MYSQL_PORT, user=connector.user if hasattr else DEFAULT_DB_USER, password=... if hasattr else '', engine=getattr(connector,'engine',DEFAULT_DB_ENGINE) 로 기존 getattr/hasattr 폴백 의미를 100% 동일하게 재현한다. src/core/constants.py 에 새 상수 DEFAULT_DB_USER='root', DEFAULT_DB_ENGINE='mysql' 를 추가하고 기존 DEFAULT_MYSQL_PORT/DEFAULT_LOCAL_HOST 를 재사용한다. 두 다이얼로그의 do_export(972-979)/do_import(1050-1056) 인라인 RustDumpConfig(...) 를 build_rust_dump_config(self.connector) 호출로 교체 — config.host/port/user/password/engine 검증 테스트가 그대로 통과해야 한다.
- [CC-135] src/core/constants.py 에 TABLE_STATUS_ICONS = {'pending':'⏳','loading':'🔄','done':'✅','error':'❌'} 를 모듈 상수로 추가하고, db_export_dialog.py on_table_status(1187-1192), db_import_dialog.py on_table_status(1193-1198)/on_table_chunk_progress(1273-1278) 세 곳의 인라인 dict 를 이 상수 참조로 교체(.get(status,'❓') fallback 동작 유지).
- [CC-139] db_import_dialog.py 에 private _format_bytes(self, size_bytes: int) -> str 헬퍼를 추가하고(size_mb=size_bytes/(1024*1024); <1024면 f'{:.1f} MB' else f'{/1024:.2f} GB'), on_table_status(1217-1222)/on_table_chunk_progress(1286-1290)/on_metadata_analyzed(1366-1370) 세 복사본을 이 호출로 대체한다. 포맷 문자열(소수 자릿수 .1f/.2f)을 정확히 보존한다.
- [CC-140] db_import_dialog.py 에 _table_results(self) -> dict(import_results 에서 'fk_restore' 제외 + isinstance(dict) 필터)와 _count_by_status(results, status) 헬퍼를 추가하고, 발산하던 5개 지점(on_import_finished 1383-1386, on_finished 1400-1403, _report_error_to_github 1480-1481, select_failed_tables 1519-1522, save_log 1590-1592)을 동일 필터로 통일한다. 통일 기준은 이미 올바른 on_import_finished/on_finished 의 'fk_restore 제외 + isinstance' 의미다. 사용자 노출 성공/실패 카운트가 이 정본값으로 수렴함을 리스크에 명시.
- [CC-142] db_import_dialog.py 상단에 from src.core.logger import get_logger 및 logger = get_logger('db_dialogs') 를 추가하고, check_timezone_support(912-913)와 _get_dump_schema_name(935-936)의 bare except 폴백 반환 직전에 logger.debug(..., exc_info=True) 로 삼킨 예외를 기록한다. 반환값(False / '')과 제어 흐름은 그대로 유지 — 진단 로깅만 추가하는 순수 관측성 개선.
- [CC-130][CC-134][CC-141] LOW 스윕(동작 불변): (1) db_export_dialog.py 642/650-652/792 의 중복 지역 import(os/datetime/Path)를 삭제하고 상단 모듈 import 사용. (2) constants.py 에 MAX_VISIBLE_LOG_LINES=500 / MAX_LOG_ENTRIES=500 추가 후 db_export_dialog.py on_raw_output(1239,1246) 2곳과 db_import_dialog.py _add_log(920)/on_raw_output(1319) 2곳의 하드코딩 500 을 상수로 교체(cap 의미·경계 유지 — test_import_log_entries_are_capped 600→500 통과). (3) db_import_dialog.py status_colors dict(1199-1204)와 _color 대입(1208, noqa F841) 미사용 dead-code 삭제.

**검증:**
- `python -m py_compile src/ui/dialogs/db_export_dialog.py src/ui/dialogs/db_import_dialog.py src/core/constants.py src/core/path_safety.py src/ui/dialogs/collapsible_config_dialog.py src/ui/workers/github_worker.py src/exporters/rust_dump_exporter.py`
- `python -m pytest tests/test_db_export_dialog.py tests/test_db_import_dialog.py tests/test_db_dialogs.py tests/test_rust_dump_exporter.py tests/test_path_safety.py -q`
- `python -m pytest -q`

**리스크:**
- db_dialogs.py 는 두 다이얼로그 클래스의 유일한 외부 소비자이자 순수 re-export 셰임이다. 공개 이름(RustDumpExportDialog/RustDumpImportDialog + 모듈 레벨 RustDumpWorker/check_rust_dump/_sanitized_rust_event 등)을 반드시 유지해야 하며, 만약 db_dialogs.py 나 이 파일 목록 밖 파일을 수정해야 할 상황이 오면 실행 에이전트는 멈추고 재스케줄을 요청해야 한다(내부 변경·시그니처 보존으로 설계상 발생하지 않게 함).
- 테스트가 module 경로에 monkeypatch 를 건다: src.ui.dialogs.db_export_dialog.RustDumpWorker / .check_rust_dump, src.ui.dialogs.db_import_dialog.RustDumpWorker / .check_rust_dump, src.ui.workers.github_worker.GitHubReportWorker. 이 이름들이 호출 시점에 패치되도록 top-level import(캡처)로 고정하지 말고 기존처럼 모듈 전역/함수 내 지역 import 참조를 유지할 것.
- CC-140 통일은 순수 리팩터가 아니라 발산 카운트를 정본값으로 수렴시킨다: GitHub 이슈 본문의 실패 테이블 수(현재 fk_restore 미제외)와 retry 선택/save_log 카운트가 미세하게 달라질 수 있다. 다만 fk_restore 는 의사(pseudo) 엔트리이고 이를 assert 하는 기존 테스트는 없어 회귀는 없음. 리뷰 시 의도된 정합성 개선으로 문서화 필요.
- 공유 파일 4개(src/core/constants.py, src/ui/workers/github_worker.py, src/exporters/rust_dump_exporter.py + 신설 path_safety/collapsible)를 건드린다. 이는 이 WP 테마가 명시적으로 지시한 범위지만, 같은 round-3 의 다른 WP 가 constants.py/github_worker.py/rust_dump_exporter.py 를 동시 수정하면 same-round 충돌이 된다 — 매니저는 이 WP 를 이 파일들의 단독 소유자로 배정하거나 마지막 순서로 머지해야 한다.
- constants.py 에는 이미 DEFAULT_MYSQL_PORT/DEFAULT_LOCAL_HOST 가 존재한다(round-1 선행 산출물 불필요, depends_on 없음). 다만 round-1 의 공유상수 WP 가 동일 파일에 추가 상수를 넣었다면 머지 시 인접 라인 충돌 가능 — 새 상수는 파일 하단에 append 하여 충돌면을 최소화한다.
- init_ui 를 빌더 메서드로 쪼갤 때 self.<위젯> 속성명이 하나라도 바뀌면 on_progress/on_raw_output/do_export 등 다른 메서드와 db_dialogs 경유 접근이 조용히 깨진다. 빌더는 반드시 기존과 동일한 self.<이름> 에 대입하고 위젯 부모/레이아웃 부착 순서를 보존할 것(PyQt 위젯 소유권 이슈 방지).
- tests/test_db_import_dialog.py 는 RustDumpImportDialog 를 db_dialogs 경유로 import 하면서 monkeypatch 는 db_import_dialog 경유로 건다. 재-export 체인(db_import_dialog→db_dialogs)이 유지되는지 py_compile + 세 테스트 파일 동시 실행으로 반드시 확인.
- 로컬 pytest 에는 GitHub CI 의존으로 항상 실패하는 macOS validation 테스트가 존재(MEMORY 기록). 전체 pytest 실행 시 해당 flaky 실패는 이 WP 회귀로 오판하지 말 것.

### WP-3.3 — misc-dialogs-cleanup
**Branch:** `refactor/cc-r3-db-misc-dialogs-cleanup` · **Size:** M · **발견:** 9건 (H1/M7/L1)

**Findings covered:** CC-124, CC-125, CC-143, CC-144, CC-188, CC-189, CC-195, CC-196, CC-197

**수정 파일:** `src/ui/dialogs/db_dialogs.py`, `src/ui/dialogs/db_connection_dialog.py`, `src/ui/dialogs/tunnel_status_dialog.py`, `src/ui/dialogs/test_dialogs.py`
**테스트:** `tests/test_db_dialogs.py`, `tests/test_db_import_dialog.py`, `tests/test_db_orphan_dialog.py`, `tests/test_db_export_dialog.py`

**가이드:**
- [CC-124] RustDumpWizard에 `_resolve_connector(self, need_connection_info: bool = False) -> tuple` 헬퍼를 추출한다. preselected_tunnel이 있으면 self._connect_preselected_tunnel() 결과를 그대로 반환하고, 없으면 DBConnectionDialog 생성 -> exec() -> get_connector() 분기를 담는다. exec()!=Accepted 또는 커넥터 falsy면 (None, None) 반환. 다이얼로그 경로에서 need_connection_info=True일 때만 conn_dialog.get_connection_identifier()로 connection_info를 채운다. start_export은 `need_connection_info=True`로 호출해 (connector, connection_info)를 사용, start_import/start_orphan_check은 `connector, _ = self._resolve_connector()`로 사용. preselected 분기는 반드시 self._connect_preselected_tunnel()을 호출해야 함 (tests/test_db_dialogs.py의 test_start_orphan_check_disconnects... 가 이 메서드를 monkeypatch함). _connect_preselected_tunnel/get_connector/get_connection_identifier의 공개 시그니처는 유지(내부 리팩터만).
- [CC-125] db_dialogs.py의 완전 죽은 import 7개만 삭제한다: cap_incomplete_export_percent, next_export_percent, export_overall_percent, format_export_row_labels, format_export_table_status, format_export_visible_telemetry (db_export_dialog에서), _build_orphan_queries_sql (db_orphan_dialog에서). 삭제 전 grep으로 db_dialogs.py 내부 참조가 0인지 재확인. tests/test_db_export_dialog.py는 이 6개 export helper를 db_export_dialog에서 직접 import하므로 영향 없음.
- [CC-125] 절대 삭제 금지: (a) RustDumpExportDialog/RustDumpImportDialog/OrphanRecordDialog는 start_export(127)/start_import(162)/start_orphan_check(197)에서 직접 사용됨. (b) 테스트 재노출 8개(format_import_row_labels, import_overall_percent, displayed_import_percent, format_import_visible_telemetry, _sanitized_rust_event, _sanitize_plain_rust_line, OrphanAnalysisWorker, OrphanReportWorker)는 tests/test_db_import_dialog.py:11-18, tests/test_db_orphan_dialog.py:11-14가 db_dialogs에서 import함. (c) line 8의 DBConnectionDialog import는 main_window.py:397/migration_dialogs.py:1411이 db_dialogs에서 재import하므로 손대지 말 것. 남는 재노출 import 블록 위에 `# 테스트 하위호환 재노출` 주석만 추가하고 __all__은 넣지 말 것(불완전 __all__이 explicit 재노출 소비자를 깨뜨릴 위험).
- [CC-143] DBConnectionDialog에 `_read_connection_fields(self) -> tuple`(host, port, user, password, database 반환)와 `_build_connector_or_raise(self, host, port, user, password, database)`(self._current_engine + self._create_connector로 커넥터 생성)를 추출한다. test_connection/do_connect는 이 두 헬퍼를 호출하되 setOverrideCursor/try-except/restoreOverrideCursor 골격과 `if not user` 검증 위치, 성공 경로(정보박스+disconnect vs self.connector 저장+accept)는 각 메서드에 그대로 남긴다. QApplication import 유지.
- [CC-144] `_apply_tunnel_data(self, tunnel_data: dict) -> None`를 추출한다: 가드된 host/port 존재 확인(`if 'host' in tunnel_data and 'port' in tunnel_data`) 후 input_host/input_port 세팅, `if 'tunnel_id' in tunnel_data` 분기로 _fill_saved_credentials + tunnel_configs 조회 + _apply_engine_from_config + default_database/default_schema로 input_database 채움. _on_tunnel_selected(currentData가 truthy일 때)과 on_mode_changed(use_tunnel and currentData일 때) 모두 이 메서드를 호출. on_mode_changed의 기존 무가드 접근이 가드 버전으로 통일됨 — 실데이터는 항상 host/port를 포함(load_active_tunnels가 t['host']/t['port'] 사용)하므로 관측 동작 불변.
- [CC-188] tunnel_status_dialog.py의 죽은 type_colors dict(218-224), 주석(225), `_color = type_colors.get(...) # noqa: F841`(226)을 삭제한다. line 227의 setForeground 삼항(connected/reconnected면 darkGreen, 아니면 black)은 현재 렌더링을 그대로 보존하기 위해 유지. 색상 dict를 실제 setForeground에 배선하면 시각 동작이 바뀌므로 금지(behavior-preserving).
- [CC-189] tunnel_status_dialog.py max_attempts_spin 블록(111-117) 위에 1줄 주석을 추가해 이 값이 settings.py의 전역 기본값(config_mgr.get_app_setting('max_reconnect_attempts', 5))과 달리 monitor 기반 per-tunnel 라이브 오버라이드임을 명시한다. 공유 build_max_reconnect_spinbox 헬퍼는 settings.py 편집이 필요하므로 이 WP 범위 밖 → 만들지 말 것(risks 참조). LOW 항목이므로 주석만.
- [CC-195] test_dialogs.py에 `_resolve_connection(self) -> Optional[tuple[str, int, Optional[Any]]]`를 추출한다: is_direct면 config remote_host/remote_port; elif self.engine.is_running(tid)면 get_connection_info; else create_temp_tunnel(실패 시 '❌ 터널 생성 실패: {error}' append 후 None 반환) -> '127.0.0.1' + get_temp_tunnel_port. refresh_databases와 execute_sql 모두 `resolved = self._resolve_connection(); if resolved is None: return; host, port, temp_server = resolved` 패턴 사용. refresh_databases는 temp_server를 기존 finally의 close_temp_tunnel(temp_server)에 그대로 넘기고, execute_sql은 `self.temp_server = temp_server`로 저장해 _cleanup 경로를 보존. 진행 메시지는 execute_sql 스타일('🔗 임시 터널 생성 중...'/'✅ 임시 터널 생성됨: localhost:{port}')로 통일(refresh_databases에 이 로그가 추가됨 — risks 참조).
- [CC-196] refresh_databases의 커넥터 비즈니스 로직(create_rust_db_connector + connect + get_schemas + disconnect)을 `_fetch_schemas(self, host, port, db_user, db_password) -> list[str]` 동기 헬퍼로 추출해 다이얼로그 메서드에서 DB 접근 로직을 분리한다. DB 연산은 계속 create_rust_db_connector(=tunnelforge-core)를 경유하며 Python DB 드라이버 직접 사용 금지. 완전한 비동기 워커 이관(SQLExecutionWorker식)은 blocking->non-blocking 동작 변경이라 behavior-preserving 범위 밖 → deferred(risks).
- [CC-197] test_dialogs.py 모듈 최상단에 `from src.core.logger import get_logger`와 `logger = get_logger(__name__)`를 추가(tunnel_status_dialog.py 등 다른 다이얼로그와 일관). refresh_databases(211-212)와 execute_sql(281-282)의 `except Exception as e:` 블록에서 기존 UI 메시지 self.output_text.append(...)는 유지하면서 `logger.exception(...)` 호출을 추가한다.
- [제약/sweep] 모든 변경은 동작 보존 리팩터로 한정한다: 공개 메서드 시그니처(get_connector, get_connection_identifier, start_export/import/orphan_check, refresh_databases, execute_sql, _connect_preselected_tunnel 등)와 기존 import 경로/재노출을 유지, 버전 bump 금지, tunnelforge-core 소유의 DB 연산 경로 재도입 금지. 위 4개 파일 외 파일(특히 settings.py, main_window.py, migration_dialogs.py, 테스트 파일) 편집이 필요해지면 즉시 중단하고 리스케줄을 요청할 것.

**검증:**
- `python -m py_compile src/ui/dialogs/db_dialogs.py src/ui/dialogs/db_connection_dialog.py src/ui/dialogs/test_dialogs.py src/ui/dialogs/tunnel_status_dialog.py`
- `python -m pytest tests/test_db_dialogs.py tests/test_db_import_dialog.py tests/test_db_orphan_dialog.py tests/test_db_export_dialog.py -q`
- `python -c "from src.ui.dialogs import SQLExecutionDialog, TestProgressDialog, RustDumpWizard; from src.ui.dialogs.db_dialogs import DBConnectionDialog; from src.ui.dialogs.tunnel_status_dialog import TunnelStatusDialog"`
- `python -m pytest -q`

**리스크:**
- [CC-125] db_dialogs.py는 3종류의 재노출 성격을 동시에 가짐: (1) 직접 사용(RustDumpExportDialog/RustDumpImportDialog/OrphanRecordDialog/DBConnectionDialog/MySQLConnector/PostgresConnector), (2) 테스트 하위호환 재노출 8개(test_db_import_dialog.py/test_db_orphan_dialog.py가 소비), (3) 완전 사장 7개(삭제 대상). 그룹을 혼동해 (1)(2)를 지우면 tests/test_db_import_dialog.py·test_db_orphan_dialog.py·main_window.py·migration_dialogs.py가 import 에러로 깨진다. 삭제 전 대상 7개 각각 grep 재확인 필수.
- [CC-125] test_db_export_dialog.py:237이 `src.ui.dialogs.db_dialogs.QMessageBox.question`을 monkeypatch함 — QMessageBox는 _connect_preselected_tunnel에서 실제 사용되므로 계속 import 상태여야 하며 이 patch 타깃이 유효하게 유지됨. QMessageBox/QDialog import를 제거하지 말 것.
- [CC-195] _resolve_connection이 진행 메시지 side-effect를 execute_sql 스타일로 통일하면 refresh_databases의 로그 텍스트에 임시터널 생성 로그 2줄이 새로 나타남(관측 가능한 로그 텍스트 변화). 제어 흐름/host/port/temp_server 결과와 finally·_cleanup 정리 경로는 동일. 리뷰어에게 의도된 cosmetic 통합임을 명시.
- [CC-196] 완전한 async 워커 이관은 blocking->non-blocking 런타임 동작 변경이라 behavior-preserving 제약에 위배 → 동기 _fetch_schemas 추출로만 범위 축소. src/core로의 완전 이전이나 신규 SchemaListWorker 도입은 신규 파일/비동기 배선/테스트 부담이 커 이번 WP에서 deferred.
- [CC-189] settings.py(318-324)와 tunnel_status_dialog.py(111-117)의 공유 spinbox 헬퍼는 settings.py 편집이 필요해 이 WP의 파일 집합 밖 → 주석만으로 처리. settings.py는 최근(041f7dd)에 리팩터되었고 다른 라운드의 settings 전용 WP가 이후 라운드에서 만질 수 있으나(후행 라운드 중복은 허용), 만약 동일 라운드에서 settings.py를 건드리는 WP가 존재하면 충돌 위험 — 이 WP는 settings.py를 전혀 수정하지 않으므로 파일 단위로는 disjoint 유지.
- test_dialogs.py / tunnel_status_dialog.py / db_connection_dialog.py에는 전용 단위 테스트가 없음(워커 테스트 test_sql_execution_worker.py·test_connection_test_worker.py는 워커만 검증). 따라서 CC-143/144/188/189/195/196/197은 py_compile + 전체 pytest의 import-level 게이트 + dialogs/__init__.py 재노출(SQLExecutionDialog/TestProgressDialog/RustDumpWizard) 및 main_window 재import로만 자동 검증됨. UI 실동작(수동 스모크)은 자동화 불가하므로 리뷰 시 유의.
- [CC-144] on_mode_changed의 무가드 host/port 접근을 가드 버전으로 통일하면 이론상 host/port 키 누락 시의 KeyError 경로가 사라진다(크래시->무동작). load_active_tunnels가 항상 host/port를 채우므로 실사용상 관측 동작 변화는 없으나, 외부에서 host/port 없는 tunnel dict를 주입하던 숨은 호출자가 있으면 동작이 달라짐(현재 코드베이스엔 없음).

### WP-3.4 — dialogs-ui
**Branch:** `refactor/cc-r3-migration-dialogs-ui` · **Size:** L · **발견:** 12건 (H2/M6/L4)

**Findings covered:** CC-145, CC-148, CC-149, CC-150, CC-152, CC-153, CC-154, CC-155, CC-156, CC-209, CC-210, CC-211

**수정 파일:** `src/ui/dialogs/migration_dialogs.py`, `src/ui/dialogs/oneclick_migration_dialog.py`, `src/ui/workers/migration_worker.py`, `tests/test_migration_worker.py`
**신규 파일:** `src/ui/dialogs/migration_result_store.py`
**테스트:** `tests/test_migration_worker.py`, `tests/test_oneclick_rust_core_gate.py`, `tests/test_migration_result_store.py (new)`

**가이드:**
- [CC-209][CC-155] src/ui/workers/migration_worker.py 에 `@dataclass(frozen=True) class MigrationCheckOptions` 정의 — 필드는 워커가 현재 forward 하는 14개 check_* 불리언(check_orphans, check_charset, check_keywords, check_routines, check_sql_mode, check_auth_plugins, check_zerofill, check_float_precision, check_fk_name_length, check_invalid_dates, check_year2, check_deprecated_engines, check_enum_empty, check_timestamp_range) 만, 각 기본값 True. MigrationAnalyzerWorker.__init__ 을 `(connector, schema, options: MigrationCheckOptions = MigrationCheckOptions())` 로 축소하고 `self.options=options` 만 저장. run() 은 `analyzer.analyze_schema(self.schema, **dataclasses.asdict(self.options))` 로 forward. 경고: analyze_schema(core) 의 16번째 파라미터 check_int_display_width 는 dataclass 에 넣지 말 것 — 현재도 워커가 전달하지 않아 analyzer 기본값(True)으로 동작하므로 그대로 두어야 동작 보존. analyze_schema(=src/core/migration_analyzer.py) 시그니처는 절대 수정 금지(WP 범위 밖).
- [CC-155] 호출부 MigrationAnalyzerDialog.start_analysis (migration_dialogs.py:714-733) 를 `options=MigrationCheckOptions(check_orphans=self.chk_orphans.isChecked(), ... , check_fk_name_length=self.chk_fk_name_length.isChecked())` 로 교체. 체크박스 없는 5개 always-on 검사(invalid_dates/year2/deprecated_engines/enum_empty/timestamp_range)는 dataclass 기본값 True 에 맡기고 명시 인자 생략 — 전송 값은 현재와 동일(동작 보존). `from src.ui.workers.migration_worker import MigrationCheckOptions` 추가. MigrationCheckOptions 를 src/ui/workers/__init__.py 에 등록하지 말 것(그 파일은 WP 범위 밖 — 모듈에서 직접 import).
- [CC-211] CleanupWorker.__init__ 에서 dry_run 파라미터와 RuntimeError 가드(103-107)를 완전히 제거 — run() 이 `execute_cleanup(action, dry_run=True)` 하드코딩이라 클래스가 구조적으로 preview 전용, 값만 검증하던 dead 파라미터. docstring 에 'preview/dry-run 전용' 명시. 호출부 migration_dialogs.py:1111-1116 의 `dry_run=dry_run` 인자 삭제(execute_cleanup 은 dry_run=False 면 1081 에서 조기 반환하므로 이 지점은 항상 dry_run=True — 동작 보존).
- [CC-211] gating 테스트 lockstep 갱신(tests/test_migration_worker.py): test_cleanup_worker_rejects_legacy_actual_cleanup_mode 를 `CleanupWorker(..., dry_run=False)` 가 이제 TypeError(예상치 못한 키워드)를 내도록 수정, test_cleanup_worker_allows_dry_run_mode 를 dry_run 인자 없이 생성하도록 수정하고 `not hasattr(worker,'dry_run')` 단언 유지. FixWizardWorker 테스트(57-77)와 fix_wizard_worker.py 는 건드리지 말 것.
- [CC-210] migration_worker.py 상단 import 를 `from src.core.migration_analyzer import MigrationAnalyzer` 로 축소 — AnalysisResult/CleanupAction/ActionType 는 live 참조 없음(주석 타입힌트만). 삭제 전 grep 으로 사용 0건 재확인. test_migration_worker.py 는 이 심볼들을 analyzer 모듈에서 직접 import 하므로 영향 없음.
- [CC-149] migration_dialogs.py 미사용 import 제거: `import html`(10), `import re`(11), `QFont`(24), QtWidgets 블록의 QLineEdit/QSpinBox/QListWidget/QListWidgetItem/QMenu/QSplitter. save_log()(658) 내부 지역 `from PyQt6.QtWidgets import QFileDialog` 삭제(모듈 스코프 21 에 이미 존재). 삭제 전 각 심볼 grep 으로 0건 재확인(특히 `re.`/`html.`).
- [CC-150] oneclick_migration_dialog.py 에서 미사용 QScrollArea(QtWidgets 블록 15), QColor(QtGui 블록 18) import 제거. 이 파일에서 이 WP 의 변경은 dead import 제거뿐 — 버튼 QSS 리팩터는 범위 밖(동일 라운드 타 UI WP 와 겹칠 수 있어 제외).
- [CC-153] migration_dialogs.py 모듈 스코프에 `ORPHAN_COUNT_CRITICAL_THRESHOLD = 1000`, `ORPHAN_COUNT_WARNING_THRESHOLD = 100` 상수 추가, update_orphans_table 의 823/825 매직넘버(>1000, >100)를 이 상수로 교체(색상 #e74c3c/#f39c12 는 그대로 — 동작 보존).
- [CC-152] migration_dialogs.py 에 순수 모듈 함수 `iter_fk_tree(fk_tree) -> Iterator[tuple[table, depth, is_cycle, is_last]]` 하나 추가해 root-finding/cycle-detection/rendered-set/미방문 사이클 재진입 로직을 단일화. _format_fk_tree_text(107)와 update_fk_tree 의 nested add_tree_items(924-936)를 이 제너레이터 소비로 재작성. 경고: _format_fk_tree_text 의 이름/시그니처/정확한 출력(📁, ├──, └──, '🔄 {child} (순환 참조)', 정렬 순서)은 반드시 보존 — test_migration_worker.py:261/247 이 문자열을 직접 단언. QTreeWidgetItem 라벨도 기존과 동일해야 함.
- [CC-154] init_ui(224-401)를 기존 탭-init 패턴에 맞춰 private 헬퍼로 분할: _build_schema_row(), _build_basic_check_options(), _build_upgrade_checker_options(), _build_action_buttons() 가 각각 layout/widget 반환. 모든 체크박스/버튼(self.chk_*, self.btn_analyze/btn_oneclick/btn_save/btn_close 등)은 반드시 인스턴스 속성으로 유지 — start_analysis, closeEvent, test_oneclick_rust_core_gate.py(btn_oneclick 단언)가 의존. 순수 내부 리팩터, 공개 시그니처/속성 불변.
- [CC-148] migration_dialogs.py 모듈 스코프에 `_make_action_button(text, bg, hover, disabled='#bdc3c7') -> QPushButton`(bold 포함) 헬퍼 추가, 7개 인라인 QSS 블록(303/317/388/483/496/558/573) 중 btn_close(388, bold/:disabled 없음) 제외한 6개를 이 헬퍼 호출로 교체하되 각 버튼의 현재 색상값 그대로 전달(외형 보존). btn_close 는 bold/disabled 없는 별도 `_make_secondary_button(text)` 변형으로 처리. 공유 src/ui/styles.py 로의 통합은 동일 라운드 UI WP 와의 파일 충돌 회피 위해 의도적으로 보류(risks 참조).
- [CC-145][CC-156] 갓클래스 분해: 새 모듈 src/ui/dialogs/migration_result_store.py 에 MigrationResultStore 추가 — 순수 파일 I/O/직렬화만 소유(analysis_dir() 확보, default_name(schema), auto_save(result)->path[to_dict+json.dump], write(result,path), read(path)->AnalysisResult[from_dict], export .sql/.txt 파일쓰기 코어). 다이얼로그의 _auto_save_result/save_analysis_result/_save_result_directly/load_analysis_result/export_orphan_queries 는 QFileDialog/QMessageBox/add_log/UI 갱신만 남기고 I/O 는 store 에 위임(공개 메서드명/시그니처 유지, thin wrapper). 또한 _generate_orphan_select_query(981)를 순수 모듈 함수 build_orphan_select_sql(orphan, schema) 로 추출해 다이얼로그 메서드에서 분리(1009/1054 호출부 갱신). 경고: AUTO_FIXABLE_TYPES 와 orphan-select SQL 을 core(migration_analyzer.py)로 이관하는 것은 round-2 core 파일이라 범위 밖 — UI 측 순수 함수/상수로만 정리하고 완전한 SSOT 통합은 deferred.

**검증:**
- `python -m py_compile src/ui/dialogs/migration_dialogs.py src/ui/dialogs/oneclick_migration_dialog.py src/ui/workers/migration_worker.py src/ui/dialogs/migration_result_store.py`
- `python -m pytest tests/test_migration_worker.py tests/test_oneclick_rust_core_gate.py tests/test_migration_result_store.py -q`
- `python -m pytest -q`

**리스크:**
- CC-211 은 CleanupWorker 의 dry_run 파라미터를 제거하므로 tests/test_migration_worker.py 의 두 CleanupWorker 테스트(test_cleanup_worker_rejects_legacy_actual_cleanup_mode, test_cleanup_worker_allows_dry_run_mode)를 반드시 같은 커밋에서 갱신해야 함(미갱신 시 TypeError 로 실패). RuntimeError 안전 가드는 사라지지만 run() 이 dry_run=True 하드코딩이라 실제 DB 변경은 여전히 구조적으로 불가 — Rust Core 소유 원칙 유지.
- CC-148 의 공유 헬퍼를 src/ui/styles.py 에 추가하는 것은 동일 라운드(round-3) 형제 UI WP(fix_wizard_*/oneclick — 동일 QSS 중복 존재)와 styles.py 를 동시 편집할 same-round 충돌 위험이 있어 의도적으로 보류. 대신 migration_dialogs.py 모듈-로컬 _make_action_button 사용 → 파일 간 중복은 잔존(deferred).
- CC-156 및 CC-145 item-3(orphan-select SQL + AUTO_FIXABLE_TYPES 를 src/core/migration_analyzer.py 로 SSOT 통합)은 round-2 core 파일 편집이 필요해 이 WP 파일셋 밖 — UI 측 순수 함수/모듈 상수로만 정리. 리뷰어가 core 배치를 요구하면 reschedule 필요.
- MigrationAnalyzerDialog 공개 표면(생성자 (parent, connector, config_manager); 속성 btn_oneclick/btn_save/chk_*/worker/cleanup_worker/disconnect_deferred_to_worker_completion; 메서드 closeEvent/execute_cleanup/update_fk_tree/load_schemas/_format_fk_tree_text)이 test_migration_worker.py, test_oneclick_rust_core_gate.py, main_window.MigrationWizard 에서 사용됨 — init_ui 분할 및 store 추출 후에도 전부 그대로 유지해야 함.
- analyze_schema(core) 는 워커가 전달하지 않는 16번째 인자 check_int_display_width 를 가짐. MigrationCheckOptions 에는 정확히 14개 forward 플래그만 넣어 **asdict(options) 가 kwargs 를 추가/누락하지 않도록 할 것(check_int_display_width 는 analyzer 기본값 True 유지 — 동작 보존).
- 현재 persistence(save/load/auto-save/export) 를 직접 검증하는 테스트가 없음 → 추출 가드용으로 tests/test_migration_result_store.py(new) 에 write->read 라운드트립을 잠글 것. QFileDialog/QMessageBox 강결합이 커서 순수 I/O 코어만 store 로 이동하고 UI 상호작용은 다이얼로그에 잔류.
- import re / import html / Qt 위젯 import 삭제 전 반드시 재-grep 으로 사용 0건 재확인(검증됐지만 회귀 방지).

### WP-3.5 — wizard-pages-cleanup
**Branch:** `refactor/cc-r3-fix-wizard-pages-cleanup` · **Size:** L · **발견:** 10건 (H0/M6/L4) · **의존:** WP-2.2

**Findings covered:** CC-151, CC-157, CC-158, CC-159, CC-160, CC-161, CC-162, CC-163, CC-164, CC-212

**수정 파일:** `src/ui/dialogs/fix_wizard_dialog.py`, `src/ui/dialogs/fix_wizard_preview_page.py`, `src/ui/dialogs/fix_wizard_execution_page.py`, `src/ui/dialogs/fix_wizard_option_page.py`, `src/ui/dialogs/fix_wizard_charset_page.py`, `src/ui/workers/fix_wizard_worker.py`, `src/core/migration_fix_models.py`, `src/core/migration_fix_wizard.py`, `tests/test_fix_wizard_dialog.py`
**신규 파일:** `tests/test_fix_wizard_sql_helpers.py`
**테스트:** `tests/test_fix_wizard_dialog.py`, `tests/test_migration_worker.py`, `tests/test_migration_fix_wizard.py`, `tests/test_fix_wizard_sql_helpers.py (new)`

**가이드:**
- [CC-151] fix_wizard_dialog.py 미사용 import 3개 제거: L20 migration_fix_wizard import에서 `CharsetTableInfo` 제거(→ `FixWizardStep, CharsetFixPlanBuilder`만 남김), L21 `from src.ui.workers.fix_wizard_worker import FixWizardWorker` 줄 통째 삭제, L24 `from ...fix_wizard_option_page import FixOptionPage, BatchOptionDialog`에서 `BatchOptionDialog` 제거(→ `FixOptionPage`만). 소스 내 실제 미참조는 grep 0건 확인됨. 단 test_fix_wizard_dialog.py가 이 이름들을 모듈 네임스페이스로 참조하므로 함께 수정: L95 `fix_wizard_dialog.FixWizardWorker(` → `fix_wizard_worker.FixWizardWorker(`(fix_wizard_worker는 이미 top import됨), 테스트 상단에 `from src.core.migration_fix_models import CharsetTableInfo` 추가 후 L248 `fix_wizard_dialog.CharsetTableInfo(` → `CharsetTableInfo(`.
- [CC-157] preview 페이지의 SQL 템플레이팅/중복제거 로직을 core로 이관(3단계): (a) migration_fix_models.py의 FixWizardStep에 `rendered_sql(self) -> str` 메서드 추가 — selected_option 없으면 '' 반환, 있으면 `sql_template or ''`에 `requires_input and user_input`일 때 `{custom_date}`/`{precision}` 치환(SmartFixGenerator.generate_sql L431-436과 동일 로직). (b) migration_fix_wizard.py의 SmartFixGenerator.generate_sql(L426)을 `return step.rendered_sql()`로 위임(기존 test_generate_sql_* 그대로 gate). (c) migration_fix_wizard.py에 모듈함수 `render_all_steps_sql(steps) -> List[Tuple[FixWizardStep, str]]` 추가: SKIP 전략 제외, 각 step을 rendered_sql()로 렌더, 렌더 문자열 값 기준 dedup(현행 `hash(sql)` set → 문자열 set으로 동등 처리). preview_page.generate_sql_preview(L92)는 이 헬퍼가 준 (step,sql) 리스트만 받아 `-- [n] {location}`/전략 라벨/개행 포맷팅만 수행하고, Part1의 FKSafeCharsetChanger 호출부(L108-131)는 그대로 둔다.
- [CC-158] 두 결과 클래스에 공통 summary() 접근자 도입: migration_fix_models.py에 `@dataclass ExecutionSummary(total:int, success:int, fail:int, skip:int, affected_rows:int)` 추가하고 `BatchExecutionResult.summary()` 구현(total=total_steps, success=success_count, fail=fail_count, skip=skip_count, affected_rows=total_affected_rows). fix_wizard_worker.py의 CombinedExecutionResult에 `summary()` 추가(total=charset_tables_count + (other_result.total_steps if other_result else 0), success=total_success_count, fail=total_fail_count, skip=(other_result.skip_count if other_result else 0), affected_rows=total_affected_rows); worker의 migration_fix_models import 목록에 `ExecutionSummary` 추가.
- [CC-158] execution_page.on_finished(L207-226)의 `hasattr(result,'charset_tables_count')` 분기 제거 → `s = result.summary()` 한 줄로 lbl_total/success/fail/affected 세팅, fail 스타일은 `s.fail>0`으로 판정. PreviewPage.on_dryrun_finished(L208-220)는 charset_tables_count/charset_fk_count 등 Combined 전용 상세 라인을 그대로 노출해야 하므로 완전 제거하지 말고 공통 수치(success/skip/affected)만 summary()로 치환, charset 상세 라인은 소량의 타입 체크 뒤 유지. 두 페이지 모두 렌더 텍스트가 이전과 1:1 동일한지 확인하고, PreviewPage에서 출력이 바뀌면 원상 복구 후 risks에 근거 기록.
- [CC-162] execution_page._save_and_show_rollback의 지역변수 `rollback_dir = self._get_rollback_dir()`(L260)를 `rollback_dir_path`로 개명하고 L261 `os.path.join(rollback_dir_path, filename)`도 함께 변경. 모듈 top의 import된 `rollback_dir` 함수(L14) 섀도잉 제거. 동작 불변.
- [CC-163] execution_page.__init__에 `self._rollback_sql_content: Optional[str] = None`을 L26(rollback_sql_path) 옆에 선언. copy_rollback_sql(L307)·save_rollback_as(L316)의 `hasattr(self,'_rollback_sql_content')` 가드를 `if self._rollback_sql_content:` (save_rollback_as는 `if not self._rollback_sql_content:`)로 단순화. _save_and_show_rollback의 두 대입(L268/L286)은 유지.
- [CC-159] option_page FixOptionPage.isComplete(L498-503)를 `def isComplete(self) -> bool: return True  # 옵션 검증은 validatePage에서 수행`로 축약, 두 분기 모두 True를 반환하는 무의미한 `if not self.wizard_dialog.wizard_steps` 제거. validatePage(L510-)는 손대지 않음.
- [CC-160] charset_page.nextId(L378-395)를 `return self.wizard_dialog.option_page_id if self.wizard_dialog.has_other_issues() else self.wizard_dialog.preview_page_id`로 collapse. 4개 조합 모두 has_other_issues()에만 의존(has_charset_issues() 외곽 분기는 dead) 확인됨. isComplete(L370-376)는 변경하지 않음.
- [CC-161] charset_page의 '원본 이슈'/'FK 연관' 태그 생성(L187-210) 중복 QSS를 `_make_tag_label(self, text: str, color: str) -> QLabel` 헬퍼로 추출 — 공통 QSS(`padding:2px 6px; border-radius:3px; font-size:10px; color:white;`)에서 background-color만 파라미터화. 호출부는 `tag = self._make_tag_label('원본 이슈', '#e74c3c') if info.is_original_issue else self._make_tag_label('FK 연관', '#3498db')`.
- [CC-164] option_page FixOptionPage 클래스 docstring(L191-200)에서 제거된 기능 3개 불릿('FK 연관 테이블 Tree 시각화', 'FK 연관테이블 일괄 변경 시 자동 포함', '자동 포함된 테이블 건너뛰기 네비게이션') 삭제(init_ui L270-273 주석이 제거 사실 명시). 현재 실제 기능(이슈별 옵션 선택 + 전체 일괄 적용 다이얼로그)만 기술. 코드 변경 없음(docstring만).
- [CC-212] ⚠️ finding의 'dry_run 파라미터 완전 제거' 권고는 채택 금지. 근거: (1) test_fix_wizard_dialog.py L91-104 및 test_migration_worker.py L57-64가 `FixWizardWorker(dry_run=False)` RuntimeError('Rust Core')를 명시 검증, (2) 쌍둥이 CleanupWorker는 out-of-scope인 migration_worker.py 소유라 '두 파일 동시 수정' 불가, (3) 현 FixWizardWorker는 이미 CleanupWorker와 동일 shape(param 수신·self.dry_run 미저장·False면 raise)이며 `not hasattr(worker,'dry_run')` 테스트도 이미 통과. in-scope 작업은 L74-92 가드에 '이 파라미터는 Rust Core mutation-ownership을 강제하는 의도적 방지 가드'라는 주석 1줄만 추가해 vestigial 오해 제거. 파라미터/가드/run()/테스트는 전부 불변.
- (HARD CONSTRAINTS sweep) 전 변경은 동작 보존 리팩터 — 공개 시그니처 유지, 기존 import 경로는 re-export로 계속 동작, 버전 bump 금지. DB 작업은 tunnelforge-core 소유 유지(Python DB 드라이버 hot path 재도입 금지; FixWizardWorker의 dry-run 하드코딩·가드 구조 불변). files_touched/new_files 밖(특히 migration_worker.py, test_migration_worker.py) 수정이 필요해지면 즉시 중단하고 리스케줄 요청. 새 core 헬퍼(rendered_sql/render_all_steps_sql/summary/ExecutionSummary) 단위 테스트는 tests/test_fix_wizard_sql_helpers.py(신규)에 작성하고, 공유 core 테스트 test_migration_fix_wizard.py는 편집하지 말고 gate로만 실행.

**검증:**
- `python -m py_compile src/ui/dialogs/fix_wizard_dialog.py src/ui/dialogs/fix_wizard_preview_page.py src/ui/dialogs/fix_wizard_execution_page.py src/ui/dialogs/fix_wizard_option_page.py src/ui/dialogs/fix_wizard_charset_page.py src/ui/workers/fix_wizard_worker.py src/core/migration_fix_models.py src/core/migration_fix_wizard.py`
- `python -m pytest tests/test_fix_wizard_dialog.py tests/test_migration_worker.py tests/test_migration_fix_wizard.py tests/test_fix_wizard_sql_helpers.py -q`
- `python -m pytest -q`

**리스크:**
- CC-157/CC-158 core 이관을 위해 src/core/migration_fix_models.py와 src/core/migration_fix_wizard.py(둘 다 round-2 WP-2.2 소유)에 헬퍼(rendered_sql, render_all_steps_sql, generate_sql 위임, ExecutionSummary, BatchExecutionResult.summary())를 추가한다. earlier→later 라운드 교차라 허용되지만, WP-3.5는 반드시 WP-2.2 머지 이후에 머지해야 충돌이 없다(depends_on 반영). WP-2.2가 이 두 파일을 크게 재편하면 rebase 필요.
- test_migration_worker.py는 round-3 WP-3.6의 gate이기도 하다(파일 L80 주석 'WP-3.6'). WP-3.5는 이 파일을 절대 편집하지 않는다(same-round 충돌 금지) — CC-212를 param 유지로 설계해 편집 불필요. 만약 실행자가 finding대로 dry_run param을 제거하면 이 파일과 migration_worker.py 편집이 강제되어 즉시 STOP/리스케줄 대상이 된다.
- CC-151: fix_wizard_dialog.py가 FixWizardWorker/CharsetTableInfo를 모듈 네임스페이스로 re-export하고 test_fix_wizard_dialog.py(L95,L248)가 이를 소비한다. import 제거 시 이 두 테스트 참조를 함께 갱신해야 하며(in-scope), 누락하면 AttributeError로 회귀.
- CC-158: PreviewPage.on_dryrun_finished는 Combined 결과에서 summary()가 담지 않는 charset_tables_count/charset_fk_count 상세를 표시한다. summary()를 무리하게 전면 적용하면 출력 텍스트가 바뀔 수 있으므로, 공통 필드만 치환하고 charset 상세 라인은 유지한다. 출력이 변하면 PreviewPage는 원복하고 ExecutionPage만 적용(CC-158은 여전히 shared accessor 도입으로 충족).
- CC-157 dedup은 현행 `hash(sql)` 기반에서 렌더 문자열 값 기반 set으로 바뀐다. 실질 동등하나 hash 충돌(극히 드묾) 시 기존은 잘못 dedup, 신규는 정확히 둘 다 유지 — 미세한 behavior 개선. 회귀로 오인 말 것.
- CombinedExecutionResult는 fix_wizard_worker.py(UI worker)에 정의돼 있어 summary()/ExecutionSummary import를 이 파일에 추가한다. ExecutionSummary는 PyQt 비의존 core(migration_fix_models.py)에 두어 순환 import를 피한다.
- Qt 페이지의 on_finished/on_dryrun_finished 렌더는 오프스크린으로도 단위 테스트가 까다롭다(기존 테스트는 _FakeSignal로 emit 안 함). summary()/rendered_sql/render_all_steps_sql는 순수 함수 단위 테스트(신규 파일)로 검증하고, 페이지 포맷 변경은 py_compile + 코드 diff 리뷰로 behavior 보존을 확인한다.

### WP-3.6 — engine-diff-dialogs
**Branch:** `refactor/cc-r3-cross-engine-diff-dialogs` · **Size:** L · **발견:** 15건 (H1/M8/L6)

**Findings covered:** CC-165, CC-166, CC-167, CC-168, CC-169, CC-170, CC-171, CC-172, CC-173, CC-174, CC-175, CC-176, CC-177, CC-178, CC-213

**수정 파일:** `src/ui/dialogs/cross_engine_migration_dialog.py`, `src/ui/dialogs/cross_engine_migration_endpoint_form.py`, `src/ui/dialogs/diff_dialog.py`, `src/ui/workers/cross_engine_migration_worker.py`, `src/core/cross_engine_migration.py`, `tests/test_cross_engine_migration_dialog.py`
**테스트:** `tests/test_cross_engine_migration_dialog.py (update: CC-168 alias references)`, `tests/test_diff_dialog.py`, `tests/test_cross_engine_migration_worker.py`, `tests/test_cross_engine_migration_protocol.py`

**가이드:**
- [CC-165] 공개 표면 고정: CrossEngineMigrationDialog(43-1499)와 CrossEngineMigrationWizard(1504-)의 클래스 이름/위치/생성자 시그니처는 그대로 유지한다(main_window.py:34,708이 CrossEngineMigrationWizard.start를 import/호출 — main_window.py는 절대 수정 금지). 테스트가 dialog._show_step/_go_next_step/_go_previous_step/_on_result/_payload/_set_running/_reset_command_ui/_schema_summary_text/_plan_summary_text 등을 dialog 인스턴스에서 직접 호출하므로, 이 메서드들은 반드시 dialog에 남기고 내부 구현만 이동한다(behavior-preserving).
- [CC-165] 프레젠테이션 로직 이관: _schema_summary_text(491-516)/_plan_summary_text(544-575)/_verification_result_text(593-644)의 순수 문자열 포맷 로직을 src/core/cross_engine_migration.py의 render_result_report 옆에 모듈 함수(format_schema_summary(schema, unsupported), format_plan_summary(payload), format_verification_result(payload))로 옮기고, dialog 메서드는 이 함수를 호출하는 얇은 래퍼로 남긴다(시그니처 보존: dialog._schema_summary_text(schema, unsupported)는 test 700행, dialog._plan_summary_text(payload)는 test 1344/1375행에서 직접 호출). 아울러 96-178의 83줄 인라인 스타일시트를 모듈 상수 _WIZARD_STYLESHEET로 추출하고 _apply_wizard_style가 self.setStyleSheet(_WIZARD_STYLESHEET)만 호출하게 한다(문자열 'QPushButton:disabled','background-color: #e4e7ec' 그대로 보존).
- [CC-169] _on_result(990-1046)의 command 분기를 command->handler 매핑으로 교체한다. 공통 선처리(self.last_result 설정, btn_save_report 활성화, migrate state 저장, schema 반영, unsupported_objects 처리)는 _on_result 상단에 유지하고, command별 본문을 _handle_readiness_result/_handle_guide_result/_handle_plan_result/_handle_verify_result/_handle_preflight_result/_handle_migrate_result로 분리해 handlers.get(payload.get('command'))로 디스패치. inspect/preflight/plan이 공유하는 상태갱신 순서와 execution_unlocked 로직을 그대로 유지해야 test_wizard_next_requires_current_step_completion 등이 통과.
- [CC-166] 네비게이션 상태 계산 단일화: _show_step(844-849)의 인라인 btn_previous/btn_next 블록을 삭제한다(바로 뒤 850행 self._refresh_navigation_state()가 동일 작업 수행). _set_running(1217-1220)의 세 번째 복사본도 self._refresh_navigation_state() 호출로 교체. 유일 소스는 _refresh_navigation_state(805-813)로 통일해 피연산자 순서 drift를 없앤다. test_finished_waits_for_worker_to_stop_before_clearing_reference/test_dialog_initial_button_states_and_running_toggle의 버튼 상태 기대치는 동일 유지.
- [CC-167] thread-wait 타임아웃을 모듈 상수로 추출: WORKER_GRACEFUL_WAIT_MS=5000(_wait_for_worker_finish 1124행), WORKER_CLOSE_WAIT_MS=3000(closeEvent 1496행), WORKER_TERMINATE_WAIT_MS=1000(1498행). 값은 반드시 그대로 유지(test는 wait_timeout==5000 확인). 세 값의 차이 의도를 1줄 주석으로 남긴다.
- [CC-168] 별칭 위젯 제거: 363행 self.btn_run_plan=self.btn_plan, 437행 self.btn_run_verify=self.btn_verify 별칭 삭제하고 addWidget 호출부(401,439)를 canonical self.btn_plan/self.btn_verify로 교체(.clicked 연결은 382/385에서 이미 canonical 사용). ⚠️ tests/test_cross_engine_migration_dialog.py가 dialog.btn_run_plan/dialog.btn_run_verify를 참조하므로(540,591행) 이를 btn_plan/btn_verify로 갱신하고, 이제 항등식이 되는 assert(541: btn_run_verify is btn_verify, 592: btn_plan is btn_run_plan)는 삭제한다.
- [CC-170] 기본값 상수화: src/core/cross_engine_migration.py에 DEFAULT_MYSQL_PORT=3306, DEFAULT_POSTGRESQL_PORT=5432, DEFAULT_POSTGRESQL_SCHEMA='public', DEFAULT_POSTGRESQL_DATABASE='postgres'를 추가한다. endpoint_form의 하드코딩(43,167-170,174,201,289 및 payload 287의 'public')과 dialog._target_approval_schema(1253의 'public')를 상수 참조로 교체. 반환 dict/UI 기본값은 동일 유지(test_dialog_initial_button_states의 schema 'source_db'/database 'postgres', test_execute_approval_uses_public_when_postgresql_target_schema_blank의 'public').
- [CC-173][CC-171] dataclass 도입 + 조건체인 정리: src/core/cross_engine_migration.py에 @dataclass ConnectionEndpointInput(engine,host,port,user,password,database,schema)과 to_payload()(기존 make_connection_payload와 동일 dict 반환)를 추가하고, make_connection_payload는 하위호환 위해 유지(dialog:32,endpoint_form:7 import 보존). endpoint_form.payload()(275-298)의 7개 위치 인자 호출을 ConnectionEndpointInput 생성 후 .to_payload()로 교체. 더불어 _apply_tunnel_data(182-217)의 POSTGRESQL/default_schema 이중 조건체인(198,200,202,204,207,209)을 순수 헬퍼 _resolve_default_database_and_schema(engine, default_database, default_schema)->tuple[str,str]로 추출해 database/schema를 한 곳에서 계산 후 대입. test_tunnel_selection_fills_endpoint_fields_from_configured_list(database='postgres',schema='analytics')로 결과 검증.
- [CC-172] _detect_engine(219-223)의 미사용 host,port 파라미터를 제거하고 호출부(191)를 self._detect_engine(config)로 변경한다(내부 전용 메서드, 공개 시그니처 아님, 소비자 파일 영향 없음).
- [CC-174][CC-175] diff_dialog 커넥터 수명주기 정리: 3중 복붙된 disconnect/None-out 블록(_start_compare 사전정리 337-348, 예외처리 378-389, closeEvent 746-759)을 dialog 메서드 _disconnect_connectors(self)로 추출해 세 곳에서 호출한다. ⚠️ dialog._source_connector/_target_connector는 test_diff_dialog.py가 직접 set/read(예: 599-609,442-443)하므로 반드시 dialog 소유로 유지 — finding 원문의 별도 파일 diff_dialog_session.py로 상태를 이관하지 말 것(테스트 파괴). CC-174의 god-class 축소는 이 헬퍼 추출과 create/teardown 그룹핑 수준의 in-file 정리로 한정한다.
- [CC-176][CC-178][CC-177] diff 렌더/타입판별/데드코드 sweep: (176) _display_results의 column_diffs(536-549)/index_diffs(552-571)/fk_diffs(574-593) 3중 루프를 _add_diff_child_item(parent_item, kind_prefix, diff, name_attr) 하나로 통합하되 index/FK의 RENAMED old_name 포맷 분기는 옵션 처리로 보존. (178) _show_diff_detail(677-709)의 hasattr 체인을 isinstance(diff, ColumnDiff/IndexDiff/ForeignKeyDiff)로 교체 — 이 클래스들은 src.core.schema_diff에서 re-export되므로 diff_dialog import에만 추가하고 schema_diff_models.py는 건드리지 않는다. (177) 미사용 import 제거: import math(7), import random(8), QtWidgets 목록의 QApplication(16).
- [CC-213] worker run()(103-179)의 6분기 이벤트 디스패치(129-152: result/error/phase/table_progress/row_progress/issue + else log)를 _dispatch_event(self, event)->bool로 추출해 run()이 'spawn->write stdin->drain stderr->stream lines->dispatch each->finalize'로 읽히게 한다. checkpoint 부기(self._last_checkpoint=... + self.checkpoint.emit)와 시그널 emit 동작은 동일 유지, 취소 break 조건도 그대로. test_worker_run_emits_result/checkpoint/failure가 무변경 통과해야 한다.

**검증:**
- `python -m py_compile src/ui/dialogs/cross_engine_migration_dialog.py src/ui/dialogs/cross_engine_migration_endpoint_form.py src/ui/dialogs/diff_dialog.py src/ui/workers/cross_engine_migration_worker.py src/core/cross_engine_migration.py`
- `python -m pytest tests/test_cross_engine_migration_dialog.py tests/test_diff_dialog.py tests/test_cross_engine_migration_worker.py tests/test_cross_engine_migration_protocol.py -q`
- `python -m pytest -q`

**리스크:**
- src/core/cross_engine_migration.py는 공유 코어 모듈이다. 라운드2 코어 정리 WP가 같은 파일을 건드렸다면 이 라운드3 WP는 라운드2 반영 이후의 main에서 분기해야 충돌이 없다(라운드가 순차 실행이므로 cross-round 자체는 허용, rebase만 필요). 같은 라운드3에서 이 코어 파일을 만지는 WP가 없어야 함.
- CC-168 별칭 제거는 tests/test_cross_engine_migration_dialog.py 수정을 강제한다(540-541,591-592행의 btn_run_plan/btn_run_verify 참조 및 항등 assert). 이 때문에 test 파일을 files_touched에 포함했다. 별칭을 남기면 finding 미해결이므로 test 갱신이 정답 경로.
- CC-174(medium god-class)는 finding 원문 권고(신규 diff_dialog_session.py로 커넥터 이관)를 그대로 따르면 test_diff_dialog.py가 dialog._source_connector/_target_connector를 직접 조작(TestCloseEventCleanup, test_cleanup_on_source_connect_failure 등)하기 때문에 깨진다. 따라서 커넥터는 dialog 소유를 유지하고 _disconnect_connectors 추출 + 수명주기 그룹핑으로 범위를 의도적으로 축소했다(문서화된 편차).
- CC-165 갓클래스 분해도 동일 제약: 스텝 상태머신/워커 배선 메서드가 테스트에 직접 커플링되어 있어 WizardStepController를 별도 파일 클래스로 완전 분리하지 못한다. 실제 이관 대상은 순수 프레젠테이션 로직(core로) + 스타일시트 상수 + _on_result 디스패치 맵 + 네비게이션 dedup로 한정. 남은 스텝 메서드는 dialog에 유지.
- 프레젠테이션 함수는 다수 테스트가 한국어 요약 부분문자열을 하드코딩(예: '테이블 3개','예상 rows 3,500','int unsigned -> bigint','검증 실패: Rust Core가 비교 차이 상세를...')하므로 core 함수 출력이 바이트 단위로 동일해야 한다. _verification_result_text는 직접 호출 테스트는 없지만 _on_result 경유로 광범위하게 검증됨.
- CC-178 isinstance 전환은 ColumnDiff/IndexDiff/ForeignKeyDiff(정의: src/core/schema_diff_models.py, re-export: src/core/schema_diff)를 diff_dialog에서 import해야 한다. src.core.schema_diff 경유로 import해 schema_diff_models.py를 건드리지 않도록 유지(라운드1에서 분리된 파일).
- PyQt 테스트는 QThread/subprocess를 띄우면 hang 위험이 있다. diff/worker/dialog 테스트는 이미 FakeProcess/FakeWorker/스레드 wait로 격리되어 있으므로, 리팩터 시 popen_factory/CrossEngineMigrationWorker 주입 지점과 시그널 이름(phase_changed/table_progress/row_progress/checkpoint/issue/log_message/failed/result/finished)을 변경하지 말 것.

### WP-3.7 — schedule-tunnel-dialogs
**Branch:** `refactor/cc-r3-settings-schedule-tunnel-dialogs` · **Size:** L · **발견:** 13건 (H2/M8/L3) · **의존:** WP-1.6

**Findings covered:** CC-179, CC-180, CC-182, CC-183, CC-184, CC-185, CC-186, CC-187, CC-190, CC-191, CC-192, CC-193, CC-194

**수정 파일:** `src/ui/dialogs/settings.py`, `src/ui/dialogs/schedule_dialog.py`, `src/ui/dialogs/tunnel_config.py`, `src/ui/dialogs/group_dialog.py`
**신규 파일:** `src/core/sql_safety.py`, `src/ui/dialogs/settings_log_tab.py`
**테스트:** `tests/test_settings_update_actions.py`, `tests/test_settings_update_launch.py`, `tests/test_tunnel_config_dialog.py`, `tests/test_sql_safety.py (new)`

**가이드:**
- [CC-192] src/core/sql_safety.py 를 새로 생성해 DANGER_PATTERNS 리스트와 순수 함수 find_dangerous_sql_warnings(sql_text: str) -> list[str] 를 옮긴다. 내부 로직은 현재와 동일하게 parse_sql_statements(sql_text) or [sql_text] 로 문장 분리 후 문장별로 re.search(pattern, statement, re.IGNORECASE|re.DOTALL) 를 돌려 중복 없이 원본 경고 문자열(⚠️ 접두 없음)을 등장 순서대로 반환한다(다중 statement negative-lookahead 엣지케이스 보존). schedule_dialog.py 의 _check_dangerous_query 는 이 함수를 호출해 라벨 텍스트(⚠️ 접두 부착)와 show/hide 만 담당하도록 축소한다. 옮긴 뒤 schedule_dialog.py 의 `from src.core.sql_statement_parser import parse_sql_statements` 는 다른 참조가 없으면 제거(grep 확인)하되, `import re` 는 SQLSyntaxHighlighter(122-123행)에서 여전히 쓰이므로 절대 제거하지 말 것. DANGER_PATTERNS 클래스 속성은 src/ 전체 grep 으로 외부 참조 0건 확인 후 제거한다.
- [CC-180] settings.py 의 _create_general_tab(91-357) 을 QGroupBox 를 반환하는 7개 private 빌더(_build_language_group / _build_close_behavior_group / _build_theme_group / _build_github_group / _build_backup_group / _build_reconnect_group / _build_startup_group)로 분해하고, _create_general_tab 은 반환 위젯을 layout 에 addWidget 만 하도록 축소한다. self.* 위젯 속성명, 시그널 연결, _github_app_configured 조건 분기, _refresh_backup_list 호출 순서를 그대로 유지해 동작을 보존한다.
- [CC-179] 클래스 책임 축소: 로그 뷰어 서브시스템(_create_log_tab 및 레벨 필터/파일 비우기 관련 메서드)을 새 파일 src/ui/dialogs/settings_log_tab.py 의 LogViewerTab(QWidget) 으로 추출하고 SettingsDialog.init_ui 가 이를 조립하도록 바꾼다. 단 테스트가 언바운드로 호출/inspect 하는 메서드 init_ui / save_settings / _on_theme_changed / _restore_original_theme_if_unsaved / _launch_installer 와 모듈 레벨 함수 update_package_action_text 는 반드시 SettingsDialog(및 settings 모듈)에 그대로 남긴다. 로그 탭 배선이 files_touched/new_files 밖(예: main_window.py)을 건드려야 한다면 추출을 포기하고 CC-180 인-파일 빌더 분해만으로 축소한다(파일 세트 이탈 시 즉시 중단·재스케줄 요청).
- [CC-183] _launch_installer(948-965) 의 main_window.engine.active_tunnels / tunnel_configs 직접 접근을 settings.py 내부 private 헬퍼(예: _collect_active_tunnel_names(main_window) -> list[str])로 국소화하고 _launch_installer 는 그 결과로 경고 문구를 조립한다. MainWindow 에 accessor 를 추가하는 이상적 방식은 main_window.py(다른 라운드3 WP 소유)를 수정해야 하므로 채택하지 않는다. hasattr(main_window,'engine') 분기와 빈 목록(len==0) 경로를 유지해 test_settings_update_launch.py(MagicMock, len 기본 0)가 계속 통과하도록 한다.
- [CC-182] settings.py 미사용 import 제거: dataclass(5행), Optional(6행), QThread·pyqtSignal(13행), QCursor(14행), 그리고 settings_update_helpers import 목록의 UpdatePackageActionText(30행). 제거 전 각 심볼을 파일 전체 grep 으로 참조 0건 확인한다. 같은 줄에 있는 QFont(14행), Qt(13행)와 sys/subprocess/QMessageBox/QDesktopServices/QUrl/QApplication/update_package_action_text/UpdateCheckerThread 는 사용 중이므로 유지한다. 34-41행 빈 줄 블록을 1줄로 정리한다.
- [CC-184] tunnel_config.py 의 init_ui(81-290) 를 기존 '--- 섹션 ---' 헤더 라벨 경계에 맞춰 form_layout 을 받는 빌더(_build_bastion_section / _build_target_db_section / _build_auth_section 등)로 분해한다. self.* 위젯 속성명과 form_layout.addRow 순서를 그대로 유지해 렌더링·시그널을 보존한다.
- [CC-185] tunnel_config.py 환경 콤보를 combo_db_engine 패턴으로 통일: combo_environment.addItem(label, value) 로 구성하되 value 는 각각 None/'production'/'staging'/'development'(라벨 문자열 '(미설정)'/'🔴 Production'/'🟠 Staging'/'🟢 Development' 유지), 로드 시 findData(self.tunnel_data.get('environment')) 로 setCurrentIndex, get_data 는 currentData() 로 읽어 env_index_map(213행)·env_map(374행) 두 딕셔너리를 완전히 삭제한다. get_data()['environment'] 반환값(None/'production'/'staging'/'development')이 변하지 않아야 하며 test_tunnel_config_dialog.py 로 회귀 확인한다.
- [CC-186] tunnel_config.py 모듈 상단에 `from src.core.logger import get_logger` 와 `logger = get_logger(__name__)` 를 추가(현재 로거 import 없음)하고 _available_tunnels 의 except 블록(347-348행)에 logger.exception("failed to load tunnel list for bastion templates") 를 넣은 뒤 [] 를 반환한다. 반환 계약([])과 '다른 연결 복사' 버튼 비활성화 동작은 그대로 유지한다.
- [CC-187] tunnel_config.py 세 _test_* 메서드의 중복을 제거: hasattr(self.parent(),'config_mgr') 로 encryptor 를 얻는 2줄을 _get_parent_encryptor() 로, self._start_connection_test(...) 후 dialog.exec() 하는 꼬리 패턴을 _run_test(test_type, temp_config, config_mgr, title) 래퍼로 추출해 _test_db_only/_test_integrated/_test_tunnel_only 가 공유하도록 한다. TestType 선택과 필드별 검증 분기는 각 호출부에 남긴다.
- [CC-191, CC-194] schedule_dialog.py 대형 메서드 분해: _setup_ui(164-464) 를 위젯 반환 빌더(_build_task_type_group / _build_basic_info_group / _build_backup_page / _build_sql_page / _build_schedule_group)로 쪼개 _setup_ui 는 최상위 레이아웃 조립만 하게 하고, _save(610-719) 는 _validate_and_build_sql_task()/_validate_and_build_backup_task() 로 분리해 공통 ScheduleConfig 필드는 _save 에서 1회 조립한다. ScheduleConfig(src.core.scheduler 소유)의 시그니처는 변경 금지 — 20개 키워드 인자를 flat 하게 그대로 전달하고, 검증·위험쿼리 확인 다이얼로그 흐름을 보존한다(recommendation 의 nested sub-dataclass 안은 scheduler.py 를 건드리므로 채택하지 않는다).
- [CC-193, CC-190] LOW 스윕: (CC-193) schedule_dialog.py 의 _browse_result_output_dir/_browse_output_dir 을 _browse_dir(self, line_edit, title) 공통 헬퍼로 통합하고 두 호출부가 각자 line_edit/타이틀을 전달(사이의 무관 메서드 _on_cron_changed 520-532 는 손대지 않음). (CC-190) group_dialog.py 상단에 모듈 상수 DEFAULT_GROUP_COLOR = '#3498db' 를 추가하고 29행의 두 분기 모두 이 상수를 참조하게 한다.

**검증:**
- `python -m py_compile src/ui/dialogs/settings.py src/ui/dialogs/schedule_dialog.py src/ui/dialogs/tunnel_config.py src/ui/dialogs/group_dialog.py src/core/sql_safety.py src/ui/dialogs/settings_log_tab.py`
- `python -m pytest tests/test_settings_update_actions.py tests/test_settings_update_launch.py tests/test_tunnel_config_dialog.py tests/test_sql_safety.py -q`
- `python -m pytest -q`

**리스크:**
- CC-183 이상안(MainWindow.get_active_tunnel_names accessor)은 src/ui/main_window.py 를 수정해야 하는데 이 파일은 다른 라운드3 WP 소유다. 따라서 settings.py 내부 헬퍼로만 국소화하고 main_window.py 는 절대 건드리지 않는다(건드려야 하면 중단·재스케줄).
- settings.py 에 강한 테스트 계약이 있다: SettingsDialog._launch_installer/init_ui/save_settings/_on_theme_changed/_restore_original_theme_if_unsaved 는 SettingsDialog 메서드로, module-level update_package_action_text/sys/subprocess/QMessageBox/QDesktopServices/QUrl 은 settings 모듈 심볼로 반드시 남겨야 한다. 이들을 새 클래스/파일로 옮기면 test_settings_update_launch.py/test_settings_update_actions.py 가 깨진다.
- test_settings_update_launch.py 는 MagicMock main_window 로 _launch_installer 를 언바운드 호출하며 len(engine.active_tunnels)==0(MagicMock __len__ 기본 0) 경로에 의존한다. CC-183 리팩터가 hasattr/len 분기를 바꾸면 통과 조건이 깨질 수 있으니 동작을 보존할 것.
- CC-182 에서 실제 사용 중인 import 를 잘못 지우면 모듈 import 자체가 실패한다. 특히 QFont(QCursor 와 같은 줄), Qt(QThread/pyqtSignal 과 같은 줄)는 유지. 제거 대상 6개 심볼은 각각 grep 으로 참조 0건을 먼저 확인.
- CC-192 후 schedule_dialog.py 의 `import re` 는 SQLSyntaxHighlighter(122-123행)에서 계속 사용되므로 제거하면 안 된다. parse_sql_statements import 만 dead 가 된다(grep 확인). DANGER_PATTERNS 는 tests 에는 참조가 없으나 제거 전 src/ 전체 grep 필요.
- CC-191/CC-194: ScheduleConfig 는 src/core/scheduler.py(WP 범위 밖) 소유다. 파라미터 그룹핑을 위해 nested dataclass 를 만들려면 scheduler.py 를 수정해야 하므로 금지 — 20개 인자를 flat 하게 전달하는 방식만 유지한다.
- schedule_dialog 의 위험쿼리 로직에 대한 기존 단위 테스트가 없다(test_scheduler.py 는 core scheduler 만 검증). 추출된 순수 함수의 회귀 게이트로 tests/test_sql_safety.py 를 반드시 신규 작성해야 한다(DROP/TRUNCATE/WHERE 없는 DELETE·UPDATE, 다중 statement 케이스 포함).
- 라운드3 파일 분리: 이 WP 의 4개 다이얼로그 + 신규 2파일은 형제 라운드3 WP(main_window/workers/db_dialogs 등)와 겹치지 않는다. settings_log_tab.py 는 신규 파일이라 충돌 없음. 동일 라운드 파일 오버랩은 관측되지 않음.

### WP-3.8 — window-workers-cleanup
**Branch:** `refactor/cc-r3-main-window-workers-cleanup` · **Size:** L · **발견:** 15건 (H2/M7/L6)

**Findings covered:** CC-198, CC-199, CC-200, CC-201, CC-202, CC-203, CC-205, CC-206, CC-207, CC-208, CC-214, CC-215, CC-216, CC-217, CC-218

**수정 파일:** `main.py`, `src/ui/main_window.py`, `src/ui/widgets/tunnel_tree.py`, `src/ui/workers/rust_dump_worker.py`, `src/ui/workers/test_worker.py`, `src/ui/workers/validation_worker.py`, `tests/test_main_window_export_import_labels.py`, `tests/test_tunnel_tree.py`, `tests/test_sql_execution_worker.py`, `tests/test_app_self_check.py`, `tests/test_connection_test_worker.py`
**신규 파일:** `src/ui/controllers/__init__.py`, `src/ui/controllers/wizard_launcher.py`, `src/ui/controllers/tray_controller.py`, `src/ui/controllers/tunnel_actions_controller.py`, `src/ui/workers/sql_execution_worker.py`, `src/ui/workers/cancellable_worker.py`
**테스트:** `tests/test_main_window_export_import_labels.py`, `tests/test_tunnel_tree.py`, `tests/test_connection_test_worker.py`, `tests/test_sql_execution_worker.py`, `tests/test_app_self_check.py`, `tests/test_cancellable_worker.py (new)`

**가이드:**
- [CC-198] TunnelManagerUI를 얇은 facade로 유지하며 협력자 3개를 `src/ui/controllers/`에 신설한다: `WizardLauncher`(마법사 생성/실행), `TrayController`(`init_tray`/`_on_tray_activated`/`_update_schedule_run_menu` 등 트레이 구성), `TunnelActionsController`(터널 CRUD: `add_tunnel_dialog`/`edit_tunnel_dialog`/`duplicate_tunnel`/`delete_tunnel` + `_process_credentials` + `save_and_refresh`). 각 협력자는 window 역참조(`self._window`)로 statusBar/config_mgr/engine/QMessageBox parent에 접근한다. window의 기존 public 메서드 이름은 1줄 위임자로 남겨 `_connect_tree_signals` 연결과 외부 호출부(main.py의 `bring_to_front`/`show`/`dispose_for_smoke_check`) 시그니처를 그대로 보존한다. 동작 보존 리팩터만(기능 변경/버전 bump 금지). ⛔ 반드시 window에 물리적으로 남길 것(이동 금지): QMetaObject 문자열 마샬링되는 `@pyqtSlot` 4종(`_on_tunnel_status_changed`, `_update_tunnel_status_ui`, `_on_backup_complete`, `_show_backup_complete_notification`)과 `_ensure_tunnel_running`/`start_tunnel`/`stop_tunnel` — 옮기면 `invokeMethod(self, "_show_backup_complete_notification", ...)`와 dummy 기반 `test_ensure_tunnel_running_*`가 침묵 속에 깨진다.
- [CC-199] window에 `_require_db_credentials(self, tunnel) -> tuple[str, str] | None` 헬퍼를 추가(경고 QMessageBox 표시 후 실패 시 None 반환, 동일 경고 문자열 유지). 6개 호출부(`_on_tree_db_connect` 383, `_test_direct_connection` 453, `open_sql_editor` 1199, `_context_rust_core_export` 1217, `_context_rust_core_import` 1240, `_context_orphan_check` 1263)의 중복 가드 블록을 이 헬퍼 호출로 교체. 단 이 메서드들은 본문에 `self._ensure_tunnel_running(...)` 호출을 그대로 유지해야 `tests/test_main_window_export_import_labels.py::test_auto_start_paths_route_through_ensure_tunnel_running`(getsource로 `_ensure_tunnel_running` 존재 + `.engine.start_tunnel(` 부재 검사)이 통과한다.
- [CC-200] `WizardLauncher._launch_rust_dump_wizard(action: str, tunnel: dict | None = None)`로 `RustDumpWizard(parent, tunnel_engine, config_manager[, preselected_tunnel])`를 1회 생성 후 `getattr(wizard, action)()`으로 디스패치. 5개 진입점(`open_rust_dump_export`/`open_rust_dump_import`/`_context_rust_core_export`/`_context_rust_core_import`/`_context_orphan_check`)의 wizard 생성/실행 tail만 이 헬퍼로 위임한다(자격증명 가드 + `_ensure_tunnel_running` 게이트는 window에 유지).
- [CC-203] `_notify_backup_result(...)` 공용 헬퍼로 `_run_schedule_now`(944-958)와 `_show_backup_complete_notification`(974-987)의 성공/실패 트레이 분기를 통합. `_show_backup_complete_notification`은 `@pyqtSlot`라 window에 유지하되 본문을 헬퍼 호출로 축약한다. 이 변경으로 `tests/test_main_window_export_import_labels.py::test_show_backup_complete_notification_shows_tray_message`의 getsource 단언(`tray_icon.showMessage`)이 깨지므로, 해당 단언을 공용 헬퍼(또는 그 헬퍼의 `tray_icon.showMessage` 호출)를 검사하도록 갱신한다. (SCHEDULE_FEATURE_ENABLED=False라 런타임 무해)
- [CC-205] `tunnel_tree._show_context_menu`(297-365)를 `_build_group_context_menu(menu, group_id)`/`_build_tunnel_context_menu(menu, tunnel_data)`로 분리하고 `_show_context_menu`는 item_type 해석 후 위임하는 thin dispatcher로 남긴다. `tests/test_tunnel_tree.py::test_context_menu_wires_orphan_check_action`이 `_show_context_menu` 소스에서 `고아 레코드 분석`/`self.tunnel_orphan_check.emit`을 검사하므로, 그 단언 대상을 `_build_tunnel_context_menu`로 갱신한다.
- [CC-206] `TunnelTreeWidget`에 `group_collapsed_changed = pyqtSignal(str, bool)`을 선언하고 `_on_item_expanded`/`_on_item_collapsed`에서 부모 체인 워크 대신 `self.group_collapsed_changed.emit(group_id, collapsed)`을 emit, `_save_collapsed_state`는 제거한다. `TunnelManagerUI._connect_tree_signals`에 `self.tunnel_tree.group_collapsed_changed.connect(lambda gid, c: self.config_mgr.save_group_collapsed_state(gid, c))`를 추가(config_mgr는 window가 이미 소유). 접힘 상태 저장 동작을 동일하게 보존한다(이 경로에 기존 단위테스트는 없으니 수동 확인).
- [CC-207][CC-208] rust_dump_worker: `run()`을 `self.task_type` 디스패처로 축소하고 `_run_export_schema()/_run_export_tables()/_run_import()` private 메서드로 분리한다. 3개 브랜치에 중복된 pass-through 클로저(`detail_callback`/`table_status_callback`/`raw_output_callback`/`metadata_callback`/`table_chunk_progress_callback`)를 바운드 메서드(`self._on_detail` 등)로 1회 정의해 참조로 전달한다. ⛔ 시그널 정의, `__init__(task_type, config, **kwargs)`, `run()`/`cancel()` public 시그니처, `_active_runner`/`_owns_facade` 기반 취소 의미(try/except/finally에서 `_active_runner` 설정·리셋)는 절대 불변 — WP-3.2(db_dialogs)가 이 워커를 소비한다.
- [CC-214] `src/ui/workers/cancellable_worker.py`에 `CancellableWorker(QThread)`를 신설(`__init__`에서 `self._cancelled = False`, `cancel(self)`로 `self._cancelled = True`). validation_worker의 `ValidationWorker`/`MetadataLoadWorker`/`AutoCompleteWorker`가 이를 상속하게 하고, 각 `__init__`은 `super().__init__()` 후 자체 속성만 설정, 중복 `cancel()`/`self._cancelled = False`를 제거한다. `run()` 내 `if self._cancelled: return` 가드는 그대로 유지. ⛔ update_worker.py 등 이 WP가 소유하지 않은 워커에는 상속을 적용하지 않는다(파일 분리 유지).
- [CC-217] `src/ui/workers/sql_execution_worker.py`를 신설해 `SQLExecutionWorker`(및 그 전용 top-level import `create_rust_db_connector`, `normalize_db_engine`, `parse_sql_statements`, `read_dollar_quote`)를 이동한다. `test_worker.py`에는 하위호환 재노출(`from src.ui.workers.sql_execution_worker import SQLExecutionWorker`)을 남겨 `workers/__init__.py`·`dialogs/test_dialogs.py`(비소유 파일)를 무수정으로 유지한다. 이동으로 `create_rust_db_connector` 바인딩이 새 모듈로 옮겨가므로 `tests/test_sql_execution_worker.py`의 monkeypatch 타깃을 `src.ui.workers.sql_execution_worker.create_rust_db_connector`로 갱신(불변 시 테스트 실패). DB 접근은 계속 RustDbConnector/create_rust_db_connector(=tunnelforge-core 경유)만 사용하고 Python DB 드라이버를 재도입하지 않는다.
- [CC-216] test_worker: `_test_db`(52-141)와 `_test_integrated`(143-230)의 공통 연결 해석(직접모드/기존터널/bastion 도달성+`create_temp_tunnel`+`get_temp_tunnel_port`)과 finally 정리(connector.disconnect/close_temp_tunnel)를 `_resolve_connection()`(또는 임시터널 컨텍스트매니저)로 추출하고, 각 메서드에는 진행 메시지·결과 문자열 조립만 남긴다. 기존 테스트는 `_resolve_db_engine`/`_create_connector`/시그널 shadowing만 검사하므로 무변경 통과. 추출된 해석기에 대한 fake-engine 단위테스트를 `tests/test_connection_test_worker.py`에 추가한다.
- [CC-218] main.py의 5개 지연-import 트램폴린(`QApplication`/`QIcon`/`ConfigManager`/`TunnelEngine`/`TunnelManagerUI`)을 클래스명과 겹치지 않는 이름(예: `_load_qapplication_class` 등, 공용 `_lazy_class(module_path, name)`에 위임하되 5개 개별 seam은 유지)으로 rename하고, `main()`과 `run_ui_smoke_check()` 두 호출부를 갱신한다. `tests/test_app_self_check.py`의 `monkeypatch.setattr(main, "QApplication", ...)` 등 5줄이 이 함수들을 DI seam으로 패치하므로 새 이름으로 반드시 갱신한다(필수).
- [CC-201][CC-202] LOW sweep: `_load_column_ratios`의 리터럴 `7`을 `len(self._default_column_ratios)`로 교체(1062-1067); `refresh_table`(361-376)의 인라인 edit/delete 버튼 컨테이너 생성을 `_build_manage_buttons(tunnel) -> QWidget` 헬퍼로 추출해 `_build_power_button`과 대칭화하고 `refresh_table`에서 호출한다.

**검증:**
- `python -m py_compile main.py src/ui/main_window.py src/ui/widgets/tunnel_tree.py src/ui/workers/rust_dump_worker.py src/ui/workers/test_worker.py src/ui/workers/validation_worker.py src/ui/workers/sql_execution_worker.py src/ui/workers/cancellable_worker.py src/ui/controllers/__init__.py src/ui/controllers/wizard_launcher.py src/ui/controllers/tray_controller.py src/ui/controllers/tunnel_actions_controller.py`
- `python -m pytest tests/test_main_window_export_import_labels.py tests/test_tunnel_tree.py tests/test_connection_test_worker.py tests/test_sql_execution_worker.py tests/test_app_self_check.py -q`
- `python -m pytest -q`

**리스크:**
- getsource 기반 테스트 핀 다수: tests/test_main_window_export_import_labels.py와 tests/test_tunnel_tree.py가 `inspect.getsource(TunnelManagerUI.X)`/`_show_context_menu` 소스 문자열을 단언한다. window에서 메서드를 협력자로 옮기거나 컨텍스트 메뉴를 분리하면 이 소스-부분문자열 단언이 깨진다 — 두 owned 테스트를 협력자 메서드 대상으로 갱신해야 한다. 모든 핀 테스트를 녹색으로 유지하기 어려우면 신뢰도-우선으로 보수적 추출(WizardLauncher + TrayController 구성만; 자격증명/슬롯 메서드는 window 유지)로 축소한다.
- QMetaObject 문자열 기반 invokeMethod: `_show_backup_complete_notification`/`_update_tunnel_status_ui`는 QMainWindow의 @pyqtSlot로 남아야 하며 협력자로 옮기면 컴파일 에러 없이 마샬링이 침묵 속에 깨진다.
- 동일 라운드 계약 결합: rust_dump_worker.py의 public 시그널/`__init__`/`run()`/`cancel()` 표면은 WP-3.2(db_dialogs)가 소비한다. 파일 겹침은 없으나 시그니처를 바꾸면 WP-3.2가 깨진다 — 내부 리팩터로만 제한.
- tests/test_sql_execution_worker.py:67이 `src.ui.workers.test_worker.create_rust_db_connector`를 monkeypatch한다. SQLExecutionWorker를 새 모듈로 옮기면 실제 사용되는 바인딩이 새 모듈로 이동하므로 patch 타깃을 옮기지 않으면 테스트가 실패한다.
- tests/test_app_self_check.py:110-114가 `main.QApplication/QIcon/ConfigManager/TunnelEngine/TunnelManagerUI`를 DI seam으로 patch한다. CC-218 rename 시 이 5개 patch 타깃을 반드시 동시 갱신(누락 시 `test_run_ui_smoke_check_builds_window_without_background` 실패).
- 비소유 파일 import 보호: src/ui/workers/__init__.py와 src/ui/dialogs/test_dialogs.py가 `from src.ui.workers.test_worker import SQLExecutionWorker`를 사용한다. test_worker.py에 재노출을 반드시 남겨 이 두 파일을 건드리지 않아야 한다(건드려야 하면 중단·리스케줄 요청).
- CC-206 시그널 재배선: expand/collapse 시 emit이 실제로 발화하고 window가 새 시그널을 config_mgr.save_group_collapsed_state에 연결하는지 확인 — 누락 시 접힘 상태 저장이 에러 없이 조용히 중단된다(해당 경로 기존 단위테스트 부재).
- DB 작업 소유권: test_worker/_resolve_connection 및 sql_execution_worker 추출 중 Python DB 드라이버 hot path를 재도입하지 말 것 — RustDbConnector/create_rust_db_connector(=tunnelforge-core) 경유를 유지.

---

## 머지 순서 & 리뷰 프로토콜

리뷰어(Opus) 검증 결과: **초기 REVISE → 4건 반영 후 실행 가능**. 스크립트 감지 11개 파일겹침 중 실제 충돌은 1건(WP-2.2/WP-2.1)뿐이었고 나머지는 "단일 writer + 게이트 전용 reader"라 머지 순서로 해소.

- Round 1 (모두 depends_on=[], 파일 disjoint 확인됨 — constants.py는 WP-1.2 단독, migration_analyzer/dump_analyzer/migration_constants + UI dialog 파일은 WP-1.5 단독, settings/styles는 WP-1.6 단독): 대형·격리 작업인 WP-1.8(Rust, 긴 cargo 빌드)을 가장 먼저 착수/병렬. Python은 순서 자유이나 후속 라운드의 기반이 되는 WP-1.5(공유 상수 ISSUE_TYPE_DISPLAY_NAMES/AUTO_FIXABLE_ISSUE_TYPES)와 WP-1.6(styles 중앙화, WP-3.7이 rebase)을 우선 머지 권장. 이어 WP-1.1, WP-1.2, WP-1.3, WP-1.4, WP-1.7은 임의 순서.
- Round 2: 먼저 WP-2.2를 WP-2.1보다 앞서 머지(WP-2.1이 test_migration_fix_generator.py를 삭제하므로 WP-2.2 게이트를 먼저 통과시키고 검증 목록에서 해당 파일 제거). 그다음 WP-2.1 -> WP-2.3 순서(WP-2.1이 test_migration_analyzer.py를 쓰고 WP-2.3이 게이트). WP-2.4~2.9(파일 disjoint)는 임의 순서. Rust는 WP-1.8 머지 완료 후: WP-2.10(dump/import), WP-2.11(query/schema/oneclick), WP-2.12(migrate/dump_format/ddl)는 파일 disjoint라 병렬 가능하되, 위 fix로 CC-252/253 재배치가 반영된 뒤 WP-2.13(축소 또는 흡수)을 마지막에 처리.
- Round 3: 사전 조건 WP-3.5 depends_on WP-2.2, WP-3.7 depends_on WP-1.6은 라운드 순서로 이미 충족. db-다이얼로그 서브시스템은 WP-3.2(db_export/import + test 파일 소유)를 WP-3.3(db_dialogs 게이트)보다 먼저 머지해 WP-3.3이 갱신된 테스트 위에서 rebase. WP-3.4(test_migration_worker.py 소유)를 WP-3.5/WP-3.6보다 먼저 머지. WP-3.1은 sql_editor_* 격리라 임의. 최대 파일인 WP-3.8(main_window/컨트롤러)을 마지막에 머지해 형제 WP들의 facade/재수출 변경 위에서 회귀 확인.

### 반영된 리뷰 수정 4건
- WP-2.2: test_migration_fix_generator.py 참조 제거(WP-2.1 삭제 소유), WP-2.1보다 선머지
- WP-2.13 폐지 → CC-252/CC-253을 WP-2.12(ddl/dump_format 소유)로 흡수
- WP-1.8: 테스트 공치 결정 규칙 명문화(다운스트림 Rust WP 파일 소유권 확정)
- WP-2.1: 사전조건 완화(WP-1.5는 속성 제거만, 재스케줄 불필요), depends_on WP-1.5 명시

### 라운드 통합 검증 (integrator, 각 라운드 머지 후 1회)
- `python -m pytest` 전체 통과(macOS 1건 예외), Rust 라운드는 `cargo test`/`cargo build --release` 추가.
- `docs/current_status.md`는 라운드 integrator가 라운드당 1회만 갱신(병렬 브랜치 충돌 방지).

---

## Finding Coverage Matrix (255건 → WP)

| Finding | Sev | File | WP |
|---|---|---|---|
| CC-000 | H | `src/core/config_manager.py:87` | WP-1.3 |
| CC-001 | H | `src/core/db_core_service.py:1` | WP-1.1 |
| CC-002 | H | `src/core/sql_validator.py:1` | WP-1.2 |
| CC-003 | M | `src/core/config_manager.py:573` | WP-1.3 |
| CC-004 | M | `src/core/db_connector.py:165` | WP-2.9 |
| CC-005 | M | `src/core/db_connector.py:300` | WP-2.9 |
| CC-006 | L | `src/core/db_core_service.py:546` | WP-1.1 |
| CC-007 | M | `src/core/db_core_service.py:453` | WP-1.1 |
| CC-008 | M | `src/core/sql_validator.py:406` | WP-1.2 |
| CC-009 | M | `src/core/sql_validator.py:595` | WP-1.2 |
| CC-010 | M | `src/core/sql_validator.py:727` | WP-1.2 |
| CC-011 | M | `src/core/sql_history.py:36` | WP-2.9 |
| CC-012 | L | `src/core/sql_history.py:97` | WP-2.9 |
| CC-013 | L | `src/core/sql_statement_parser.py:35` | WP-2.9 |
| CC-014 | L | `src/core/db_core_service.py:736` | WP-1.1 |
| CC-015 | L | `src/core/sql_history.py:204` | WP-2.9 |
| CC-016 | L | `src/core/sql_validator.py:293` | WP-1.2 |
| CC-017 | L | `src/core/config_manager.py:63` | WP-1.3 |
| CC-018 | H | `src/core/scheduler.py:242` | WP-2.4 |
| CC-019 | M | `src/core/scheduler.py:654` | WP-2.4 |
| CC-020 | M | `src/core/scheduler.py:708` | WP-2.4 |
| CC-021 | L | `src/core/scheduler.py:23` | WP-2.4 |
| CC-022 | M | `src/core/scheduler.py:885` | WP-2.4 |
| CC-023 | M | `src/core/tunnel_engine.py:112` | WP-2.8 |
| CC-024 | L | `src/core/tunnel_engine.py:77` | WP-2.8 |
| CC-025 | M | `src/core/tunnel_monitor.py:81` | WP-2.8 |
| CC-026 | M | `src/core/tunnel_monitor.py:549` | WP-2.8 |
| CC-027 | L | `src/core/tunnel_monitor.py:537` | WP-2.8 |
| CC-028 | L | `src/core/single_instance.py:86` | WP-2.8 |
| CC-029 | M | `src/core/platform_paths.py:24` | WP-2.8 |
| CC-030 | M | `src/core/platform_integration.py:140` | WP-2.8 |
| CC-031 | M | `src/core/production_guard.py:76` | WP-2.8 |
| CC-032 | L | `src/core/production_guard.py:20` | WP-2.8 |
| CC-033 | L | `src/core/production_guard.py:124` | WP-2.8 |
| CC-034 | L | `src/core/mysql_login_path.py:222` | WP-2.8 |
| CC-035 | L | `src/core/mysql_login_path.py:104` | WP-2.8 |
| CC-036 | M | `src/core/github_app_auth.py:322` | WP-2.9 |
| CC-037 | M | `src/core/github_app_auth.py:248` | WP-2.9 |
| CC-038 | M | `src/core/github_issue_reporter.py:23` | WP-2.9 |
| CC-039 | M | `src/core/github_issue_reporter.py:383` | WP-2.9 |
| CC-040 | L | `src/core/github_issue_reporter.py:101` | WP-2.9 |
| CC-041 | H | `src/core/i18n.py:1` | WP-1.4 |
| CC-042 | L | `src/core/i18n.py:1` | WP-1.4 |
| CC-043 | M | `src/core/i18n.py:1517` | WP-1.4 |
| CC-044 | M | `src/core/i18n.py:1351` | WP-1.4 |
| CC-045 | M | `src/core/i18n.py:1371` | WP-1.4 |
| CC-046 | L | `src/core/i18n.py:1347` | WP-1.4 |
| CC-047 | L | `src/core/logger.py:173` | WP-2.8 |
| CC-048 | M | `src/core/update_downloader.py:204` | WP-2.9 |
| CC-049 | L | `src/core/oneclick_log.py:13` | WP-2.8 |
| CC-050 | H | `src/core/migration_analyzer.py:170` | WP-2.1 |
| CC-051 | M | `src/core/migration_analyzer.py:179` | WP-1.5 |
| CC-052 | M | `src/core/migration_analyzer.py:885` | WP-2.1 |
| CC-053 | M | `src/core/migration_analyzer.py:639` | WP-2.1 |
| CC-054 | M | `src/core/migration_analyzer.py:290` | WP-2.1 |
| CC-055 | L | `src/core/migration_analyzer.py:585` | WP-2.1 |
| CC-056 | L | `src/core/migration_analyzer.py:13` | WP-2.1 |
| CC-057 | L | `src/core/migration_analyzer.py:1280` | WP-2.1 |
| CC-058 | L | `src/core/migration_analyzer.py:266` | WP-2.1 |
| CC-059 | L | `src/core/migration_analyzer.py:753` | WP-2.1 |
| CC-060 | M | `src/core/migration_dump_analyzer.py:130` | WP-2.3 |
| CC-061 | L | `src/core/migration_dump_analyzer.py:249` | WP-2.3 |
| CC-062 | M | `src/core/migration_constants.py:433` | WP-1.5 |
| CC-063 | L | `src/core/migration_constants.py:302` | WP-1.5 |
| CC-064 | M | `src/core/migration_parsers.py:738` | WP-2.3 |
| CC-065 | H | `src/core/migration_fix_wizard.py:1` | WP-2.2 |
| CC-066 | H | `src/core/migration_fix_wizard.py:941` | WP-2.2 |
| CC-067 | L | `src/core/migration_fix_wizard.py:1196` | WP-2.2 |
| CC-068 | M | `src/core/migration_fix_wizard.py:44` | WP-2.2 |
| CC-069 | L | `src/core/migration_fix_wizard.py:166` | WP-2.2 |
| CC-070 | M | `src/core/migration_fix_wizard.py:1328` | WP-2.2 |
| CC-071 | M | `src/core/migration_fix_generator.py:18` | WP-2.1 |
| CC-072 | M | `src/core/migration_fix_wizard.py:236` | WP-2.2 |
| CC-073 | L | `src/core/migration_rollback_sql_generator.py:371` | WP-2.2 |
| CC-074 | M | `src/core/migration_fix_wizard.py:840` | WP-2.2 |
| CC-075 | M | `src/core/migration_fix_wizard.py:888` | WP-2.2 |
| CC-076 | H | `src/core/cross_engine_migration.py:269` | WP-2.7 |
| CC-077 | M | `src/core/cross_engine_migration.py:1` | WP-2.7 |
| CC-078 | L | `src/core/cross_engine_migration.py:285` | WP-2.7 |
| CC-079 | L | `src/core/cross_engine_migration.py:300` | WP-2.7 |
| CC-080 | L | `src/core/migration_fix_generator.py:182` | WP-2.1 |
| CC-081 | M | `src/core/migration_rollback_sql_generator.py:180` | WP-2.2 |
| CC-082 | M | `src/core/migration_rules/storage_rules.py:17` | WP-2.3 |
| CC-083 | M | `src/core/migration_rules/data_rules.py:166` | WP-2.3 |
| CC-084 | H | `src/core/migration_rules/data_rules.py:17` | WP-2.3 |
| CC-085 | H | `src/core/migration_rules/schema_rules.py:5` | WP-2.3 |
| CC-086 | M | `src/core/migration_rules/data_rules.py:726` | WP-2.3 |
| CC-087 | M | `src/core/migration_rules/data_rules.py:542` | WP-2.3 |
| CC-088 | M | `src/core/migration_rules/data_rules.py:1009` | WP-2.3 |
| CC-089 | M | `src/core/migration_rules/schema_rules.py:61` | WP-2.3 |
| CC-090 | M | `src/core/migration_rules/schema_rules.py:123` | WP-2.3 |
| CC-091 | M | `src/core/migration_rules/schema_rules.py:64` | WP-2.3 |
| CC-092 | M | `src/core/migration_rules/schema_rules.py:14` | WP-2.3 |
| CC-093 | M | `src/core/migration_rules/data_rules.py:344` | WP-2.3 |
| CC-094 | L | `src/core/migration_rules/data_rules.py:22` | WP-2.3 |
| CC-095 | L | `src/core/migration_rules/schema_rules.py:226` | WP-2.3 |
| CC-096 | H | `src/core/schema_comparator.py:202` | WP-2.5 |
| CC-097 | H | `src/core/schema_sync_script_generator.py:12` | WP-2.5 |
| CC-098 | H | `src/core/schema_severity_classifier.py:88` | WP-2.5 |
| CC-099 | H | `src/exporters/rust_dump_exporter.py:1` | WP-2.6 |
| CC-100 | M | `src/exporters/rust_dump_exporter.py:330` | WP-2.6 |
| CC-101 | M | `src/exporters/rust_dump_exporter.py:629` | WP-2.6 |
| CC-102 | L | `src/exporters/rust_dump_exporter.py:20` | WP-2.6 |
| CC-103 | M | `src/exporters/rust_dump_exporter.py:764` | WP-2.6 |
| CC-104 | M | `src/exporters/rust_dump_exporter.py:189` | WP-2.6 |
| CC-105 | M | `src/core/schema_sync_script_generator.py:40` | WP-2.5 |
| CC-106 | M | `src/core/schema_sync_script_generator.py:111` | WP-2.5 |
| CC-107 | H | `src/core/schema_extractor.py:24` | WP-2.5 |
| CC-108 | M | `src/core/schema_extractor.py:65` | WP-2.5 |
| CC-109 | M | `src/core/schema_diff_models.py:79` | WP-2.5 |
| CC-110 | L | `src/core/schema_severity_classifier.py:174` | WP-2.5 |
| CC-111 | L | `src/core/schema_severity_classifier.py:106` | WP-2.5 |
| CC-112 | H | `src/ui/dialogs/sql_editor_dialog.py:125` | WP-3.1 |
| CC-113 | M | `src/ui/dialogs/sql_editor_dialog.py:233` | WP-3.1 |
| CC-114 | M | `src/ui/dialogs/sql_editor_dialog.py:1508` | WP-3.1 |
| CC-115 | L | `src/ui/dialogs/sql_editor_dialog.py:1588` | WP-3.1 |
| CC-116 | L | `src/ui/dialogs/sql_editor_dialog.py:992` | WP-3.1 |
| CC-117 | L | `src/ui/dialogs/sql_editor_dialog.py:1211` | WP-3.1 |
| CC-118 | L | `src/ui/dialogs/sql_editor_dialog.py:72` | WP-3.1 |
| CC-119 | L | `src/ui/dialogs/sql_editor_code_editor.py:403` | WP-3.1 |
| CC-120 | L | `src/ui/dialogs/sql_editor_highlighters.py:152` | WP-3.1 |
| CC-121 | M | `src/ui/dialogs/sql_editor_workers.py:108` | WP-3.1 |
| CC-122 | L | `src/ui/dialogs/sql_editor_history_dialog.py:218` | WP-3.1 |
| CC-123 | M | `src/ui/dialogs/sql_editor_workers.py:33` | WP-3.1 |
| CC-124 | M | `src/ui/dialogs/db_dialogs.py:98` | WP-3.3 |
| CC-125 | M | `src/ui/dialogs/db_dialogs.py:9` | WP-3.3 |
| CC-126 | H | `src/ui/dialogs/db_export_dialog.py:168` | WP-3.2 |
| CC-127 | M | `src/ui/dialogs/db_export_dialog.py:212` | WP-3.2 |
| CC-128 | M | `src/ui/dialogs/db_export_dialog.py:877` | WP-3.2 |
| CC-129 | M | `src/ui/dialogs/db_export_dialog.py:645` | WP-3.2 |
| CC-130 | L | `src/ui/dialogs/db_export_dialog.py:642` | WP-3.2 |
| CC-131 | M | `src/ui/dialogs/db_export_dialog.py:604` | WP-3.2 |
| CC-132 | M | `src/ui/dialogs/db_export_dialog.py:1252` | WP-3.2 |
| CC-133 | M | `src/ui/dialogs/db_export_dialog.py:972` | WP-3.2 |
| CC-134 | L | `src/ui/dialogs/db_export_dialog.py:1238` | WP-3.2 |
| CC-135 | M | `src/ui/dialogs/db_export_dialog.py:1186` | WP-3.2 |
| CC-136 | H | `src/ui/dialogs/db_import_dialog.py:222` | WP-3.2 |
| CC-137 | M | `src/ui/dialogs/db_import_dialog.py:267` | WP-3.2 |
| CC-138 | M | `src/ui/dialogs/db_import_dialog.py:946` | WP-3.2 |
| CC-139 | M | `src/ui/dialogs/db_import_dialog.py:1216` | WP-3.2 |
| CC-140 | M | `src/ui/dialogs/db_import_dialog.py:1382` | WP-3.2 |
| CC-141 | L | `src/ui/dialogs/db_import_dialog.py:1199` | WP-3.2 |
| CC-142 | M | `src/ui/dialogs/db_import_dialog.py:899` | WP-3.2 |
| CC-143 | M | `src/ui/dialogs/db_connection_dialog.py:212` | WP-3.3 |
| CC-144 | M | `src/ui/dialogs/db_connection_dialog.py:160` | WP-3.3 |
| CC-145 | H | `src/ui/dialogs/migration_dialogs.py:146` | WP-3.4 |
| CC-146 | M | `src/ui/dialogs/migration_dialogs.py:1159` | WP-1.5 |
| CC-147 | H | `src/ui/dialogs/migration_dialogs.py:871` | WP-1.5 |
| CC-148 | M | `src/ui/dialogs/migration_dialogs.py:303` | WP-3.4 |
| CC-149 | L | `src/ui/dialogs/migration_dialogs.py:10` | WP-3.4 |
| CC-150 | L | `src/ui/dialogs/oneclick_migration_dialog.py:11` | WP-3.4 |
| CC-151 | L | `src/ui/dialogs/fix_wizard_dialog.py:20` | WP-3.5 |
| CC-152 | M | `src/ui/dialogs/migration_dialogs.py:924` | WP-3.4 |
| CC-153 | L | `src/ui/dialogs/migration_dialogs.py:821` | WP-3.4 |
| CC-154 | M | `src/ui/dialogs/migration_dialogs.py:224` | WP-3.4 |
| CC-155 | M | `src/ui/dialogs/migration_dialogs.py:714` | WP-3.4 |
| CC-156 | M | `src/ui/dialogs/migration_dialogs.py:981` | WP-3.4 |
| CC-157 | M | `src/ui/dialogs/fix_wizard_preview_page.py:92` | WP-3.5 |
| CC-158 | M | `src/ui/dialogs/fix_wizard_execution_page.py:207` | WP-3.5 |
| CC-159 | L | `src/ui/dialogs/fix_wizard_option_page.py:498` | WP-3.5 |
| CC-160 | M | `src/ui/dialogs/fix_wizard_charset_page.py:378` | WP-3.5 |
| CC-161 | L | `src/ui/dialogs/fix_wizard_charset_page.py:186` | WP-3.5 |
| CC-162 | M | `src/ui/dialogs/fix_wizard_execution_page.py:254` | WP-3.5 |
| CC-163 | L | `src/ui/dialogs/fix_wizard_execution_page.py:267` | WP-3.5 |
| CC-164 | M | `src/ui/dialogs/fix_wizard_option_page.py:191` | WP-3.5 |
| CC-165 | H | `src/ui/dialogs/cross_engine_migration_dialog.py:43` | WP-3.6 |
| CC-166 | M | `src/ui/dialogs/cross_engine_migration_dialog.py:805` | WP-3.6 |
| CC-167 | L | `src/ui/dialogs/cross_engine_migration_dialog.py:1124` | WP-3.6 |
| CC-168 | L | `src/ui/dialogs/cross_engine_migration_dialog.py:363` | WP-3.6 |
| CC-169 | M | `src/ui/dialogs/cross_engine_migration_dialog.py:990` | WP-3.6 |
| CC-170 | M | `src/ui/dialogs/cross_engine_migration_endpoint_form.py:43` | WP-3.6 |
| CC-171 | M | `src/ui/dialogs/cross_engine_migration_endpoint_form.py:182` | WP-3.6 |
| CC-172 | L | `src/ui/dialogs/cross_engine_migration_endpoint_form.py:219` | WP-3.6 |
| CC-173 | M | `src/ui/dialogs/cross_engine_migration_endpoint_form.py:290` | WP-3.6 |
| CC-174 | M | `src/ui/dialogs/diff_dialog.py:38` | WP-3.6 |
| CC-175 | M | `src/ui/dialogs/diff_dialog.py:337` | WP-3.6 |
| CC-176 | M | `src/ui/dialogs/diff_dialog.py:536` | WP-3.6 |
| CC-177 | L | `src/ui/dialogs/diff_dialog.py:7` | WP-3.6 |
| CC-178 | L | `src/ui/dialogs/diff_dialog.py:677` | WP-3.6 |
| CC-179 | H | `src/ui/dialogs/settings.py:42` | WP-3.7 |
| CC-180 | M | `src/ui/dialogs/settings.py:91` | WP-3.7 |
| CC-181 | H | `src/ui/dialogs/settings.py:67` | WP-1.6 |
| CC-182 | M | `src/ui/dialogs/settings.py:5` | WP-3.7 |
| CC-183 | M | `src/ui/dialogs/settings.py:948` | WP-3.7 |
| CC-184 | M | `src/ui/dialogs/tunnel_config.py:81` | WP-3.7 |
| CC-185 | M | `src/ui/dialogs/tunnel_config.py:200` | WP-3.7 |
| CC-186 | M | `src/ui/dialogs/tunnel_config.py:340` | WP-3.7 |
| CC-187 | L | `src/ui/dialogs/tunnel_config.py:457` | WP-3.7 |
| CC-188 | M | `src/ui/dialogs/tunnel_status_dialog.py:216` | WP-3.3 |
| CC-189 | L | `src/ui/dialogs/tunnel_status_dialog.py:111` | WP-3.3 |
| CC-190 | L | `src/ui/dialogs/group_dialog.py:29` | WP-3.7 |
| CC-191 | H | `src/ui/dialogs/schedule_dialog.py:164` | WP-3.7 |
| CC-192 | M | `src/ui/dialogs/schedule_dialog.py:132` | WP-3.7 |
| CC-193 | L | `src/ui/dialogs/schedule_dialog.py:511` | WP-3.7 |
| CC-194 | M | `src/ui/dialogs/schedule_dialog.py:610` | WP-3.7 |
| CC-195 | H | `src/ui/dialogs/test_dialogs.py:158` | WP-3.3 |
| CC-196 | M | `src/ui/dialogs/test_dialogs.py:186` | WP-3.3 |
| CC-197 | M | `src/ui/dialogs/test_dialogs.py:211` | WP-3.3 |
| CC-198 | H | `src/ui/main_window.py` | WP-3.8 |
| CC-199 | M | `src/ui/main_window.py:383` | WP-3.8 |
| CC-200 | M | `src/ui/main_window.py:680` | WP-3.8 |
| CC-201 | L | `src/ui/main_window.py:1062` | WP-3.8 |
| CC-202 | L | `src/ui/main_window.py:337` | WP-3.8 |
| CC-203 | L | `src/ui/main_window.py:932` | WP-3.8 |
| CC-204 | H | `src/ui/styles.py:23` | WP-1.6 |
| CC-205 | M | `src/ui/widgets/tunnel_tree.py:297` | WP-3.8 |
| CC-206 | M | `src/ui/widgets/tunnel_tree.py:404` | WP-3.8 |
| CC-207 | M | `src/ui/workers/rust_dump_worker.py:58` | WP-3.8 |
| CC-208 | M | `src/ui/workers/rust_dump_worker.py:70` | WP-3.8 |
| CC-209 | H | `src/ui/workers/migration_worker.py:16` | WP-3.4 |
| CC-210 | L | `src/ui/workers/migration_worker.py:5` | WP-3.4 |
| CC-211 | M | `src/ui/workers/migration_worker.py:89` | WP-3.4 |
| CC-212 | M | `src/ui/workers/fix_wizard_worker.py:74` | WP-3.5 |
| CC-213 | L | `src/ui/workers/cross_engine_migration_worker.py:103` | WP-3.6 |
| CC-214 | L | `src/ui/workers/validation_worker.py:48` | WP-3.8 |
| CC-215 | L | `src/ui/workers/validation_worker.py:7` | WP-3.8 |
| CC-216 | H | `src/ui/workers/test_worker.py:52` | WP-3.8 |
| CC-217 | L | `src/ui/workers/test_worker.py:262` | WP-3.8 |
| CC-218 | M | `main.py:99` | WP-3.8 |
| CC-219 | H | `scripts/smart_release.py:59` | WP-1.7 |
| CC-220 | M | `scripts/bump_version.py:121` | WP-1.7 |
| CC-221 | H | `migration_core/src/lib.rs:2019` | WP-2.10 |
| CC-222 | H | `migration_core/src/lib.rs:2107` | WP-2.10 |
| CC-223 | H | `migration_core/src/lib.rs:1620` | WP-2.10 |
| CC-224 | H | `migration_core/src/lib.rs:3234` | WP-2.10 |
| CC-225 | M | `migration_core/src/lib.rs:2945` | WP-2.10 |
| CC-226 | M | `migration_core/src/lib.rs:2526` | WP-2.10 |
| CC-227 | L | `migration_core/src/lib.rs:1640` | WP-2.10 |
| CC-228 | L | `migration_core/src/lib.rs:1568` | WP-2.10 |
| CC-229 | M | `migration_core/src/lib.rs:5656` | WP-2.11 |
| CC-230 | H | `migration_core/src/lib.rs:3669` | WP-2.10 |
| CC-231 | H | `migration_core/src/lib.rs:5480` | WP-2.11 |
| CC-232 | M | `migration_core/src/lib.rs:6797` | WP-2.11 |
| CC-233 | L | `migration_core/src/lib.rs:4783` | WP-2.11 |
| CC-234 | M | `migration_core/src/lib.rs:4612` | WP-2.11 |
| CC-235 | M | `migration_core/src/lib.rs:4828` | WP-2.11 |
| CC-236 | L | `migration_core/src/lib.rs:4001` | WP-2.10 |
| CC-237 | H | `migration_core/src/lib.rs:10336` | WP-2.12 |
| CC-238 | M | `migration_core/src/lib.rs:8167` | WP-2.12 |
| CC-239 | M | `migration_core/src/lib.rs:8435` | WP-2.12 |
| CC-240 | M | `migration_core/src/lib.rs:7599` | WP-2.12 |
| CC-241 | L | `migration_core/src/lib.rs:8336` | WP-2.12 |
| CC-242 | L | `migration_core/src/lib.rs:7026` | WP-2.11 |
| CC-243 | L | `migration_core/src/lib.rs:9373` | WP-2.12 |
| CC-244 | H | `migration_core/src/lib.rs:11119` | WP-2.12 |
| CC-245 | M | `migration_core/src/lib.rs:11185` | WP-2.12 |
| CC-246 | M | `migration_core/src/lib.rs:10580` | WP-2.12 |
| CC-247 | M | `migration_core/src/lib.rs:10775` | WP-2.12 |
| CC-248 | M | `migration_core/src/lib.rs:10737` | WP-2.12 |
| CC-249 | M | `migration_core/src/lib.rs:11023` | WP-2.12 |
| CC-250 | L | `migration_core/src/lib.rs:10859` | WP-2.12 |
| CC-251 | L | `migration_core/src/lib.rs:11104` | WP-2.12 |
| CC-252 | M | `migration_core/src/lib.rs:15940` | WP-2.12 |
| CC-253 | M | `migration_core/src/lib.rs:14112` | WP-2.12 |
| CC-254 | H | `migration_core/src/lib.rs:1` | WP-1.8 |


_배정 확인: 255/255건 배정, 미배정 0_