"""
migration_fix_wizard.py 단위 테스트

SmartFixGenerator, CollationFKGraphBuilder, FKSafeCharsetChanger,
BatchFixExecutor, RollbackSQLGenerator, CharsetFixPlanBuilder 검증.
"""
import pytest
from unittest.mock import MagicMock, patch

from src.core.migration_constants import IssueType, CompatibilityIssue
from src.core.migration_fix_wizard import (
    FixStrategy,
    FKDefinition,
    FixOption,
    FixWizardStep,
    FixExecutionResult,
    BatchExecutionResult,
    SmartFixGenerator,
    CollationFKGraphBuilder,
    FKSafeCharsetChanger,
    BatchFixExecutor,
    RollbackSQLGenerator,
    CharsetFixPlanBuilder,
    CharsetTableInfo,
    create_wizard_steps,
)
from tests.conftest import FakeMySQLConnector, make_issue


# ============================================================
# Helper
# ============================================================
def _make_option(strategy, label="opt", description="desc", **kw):
    return FixOption(strategy=strategy, label=label, description=description, **kw)


def _make_step(idx, issue_type, location="test_db.table", options=None,
               selected_option=None, **kw):
    return FixWizardStep(
        issue_index=idx,
        issue_type=issue_type,
        location=location,
        description="test",
        options=options or [],
        selected_option=selected_option,
        **kw,
    )


# ============================================================
# FKDefinition 테스트
# ============================================================
class TestFKDefinition:
    def test_get_drop_sql(self):
        fk = FKDefinition(
            constraint_name="fk_order_user",
            table_name="orders",
            columns=["user_id"],
            ref_table="users",
            ref_columns=["id"],
        )
        sql = fk.get_drop_sql("test_db")
        assert "DROP FOREIGN KEY" in sql
        assert "`test_db`.`orders`" in sql
        assert "`fk_order_user`" in sql

    def test_get_add_sql(self):
        fk = FKDefinition(
            constraint_name="fk_order_user",
            table_name="orders",
            columns=["user_id"],
            ref_table="users",
            ref_columns=["id"],
            on_delete="CASCADE",
            on_update="NO ACTION",
        )
        sql = fk.get_add_sql("test_db")
        assert "ADD CONSTRAINT `fk_order_user`" in sql
        assert "FOREIGN KEY (`user_id`)" in sql
        assert "REFERENCES `users` (`id`)" in sql
        assert "ON DELETE CASCADE" in sql
        assert "ON UPDATE NO ACTION" in sql

    def test_composite_fk(self):
        """복합 FK 컬럼 지원"""
        fk = FKDefinition(
            constraint_name="fk_composite",
            table_name="child",
            columns=["a", "b"],
            ref_table="parent",
            ref_columns=["x", "y"],
        )
        add_sql = fk.get_add_sql("s")
        assert "(`a`, `b`)" in add_sql
        assert "(`x`, `y`)" in add_sql


# ============================================================
# SmartFixGenerator 테스트
# ============================================================
class TestSmartFixGenerator:
    @pytest.fixture
    def generator(self):
        conn = FakeMySQLConnector()
        return SmartFixGenerator(conn, "test_db")

    def test_invalid_date_nullable(self, generator):
        """nullable 컬럼이면 DATE_TO_NULL이 권장"""
        generator.connector.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'YES'}],
        }
        issue = make_issue(
            IssueType.INVALID_DATE,
            location="test_db.orders.created_at",
            table_name="orders", column_name="created_at",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.DATE_TO_NULL in strategies
        assert FixStrategy.DATE_TO_MIN in strategies
        assert FixStrategy.DATE_TO_CUSTOM in strategies
        assert FixStrategy.SKIP in strategies  # 항상 마지막에 추가

        # is_recommended 검증
        null_opt = next(o for o in options if o.strategy == FixStrategy.DATE_TO_NULL)
        assert null_opt.is_recommended is True

    def test_invalid_date_not_nullable(self, generator):
        """NOT NULL 컬럼이면 DATE_TO_MIN이 권장"""
        generator.connector.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'NO'}],
        }
        issue = make_issue(
            IssueType.INVALID_DATE,
            location="test_db.orders.created_at",
            table_name="orders", column_name="created_at",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.DATE_TO_NULL not in strategies  # nullable이 아니므로

        min_opt = next(o for o in options if o.strategy == FixStrategy.DATE_TO_MIN)
        assert min_opt.is_recommended is True

    def test_invalid_date_no_table_column_falls_back(self, generator):
        """table_name/column_name이 없으면 기본 옵션"""
        issue = make_issue(IssueType.INVALID_DATE)
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies
        assert FixStrategy.SKIP in strategies

    def test_charset_table_level(self, generator):
        """테이블 레벨 charset 변경 옵션"""
        # FK 그래프가 비어있으면 SINGLE만 나옴
        generator.connector.query_results = {
            'KEY_COLUMN_USAGE': [],
        }
        issue = make_issue(
            IssueType.CHARSET_ISSUE,
            location="test_db.users",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.COLLATION_SINGLE in strategies
        assert FixStrategy.SKIP in strategies

    def test_charset_column_level_with_definition(self, generator):
        """컬럼 레벨 charset 변경 옵션 (정의 조회 성공)"""
        generator.connector.query_results = {
            'INFORMATION_SCHEMA.COLUMNS': [{
                'COLUMN_TYPE': 'varchar(255)',
                'IS_NULLABLE': 'YES',
                'COLUMN_DEFAULT': None,
                'EXTRA': '',
            }],
        }
        issue = make_issue(
            IssueType.CHARSET_ISSUE,
            location="test_db.users.name",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.COLLATION_SINGLE in strategies

    def test_charset_column_level_no_definition(self, generator):
        """컬럼 정의 조회 실패 시 MANUAL"""
        issue = make_issue(
            IssueType.CHARSET_ISSUE,
            location="test_db.users.name",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies

    def test_zerofill_options(self, generator):
        issue = make_issue(IssueType.ZEROFILL_USAGE)
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies
        assert FixStrategy.SKIP in strategies

    def test_float_precision_options(self, generator):
        issue = make_issue(
            IssueType.FLOAT_PRECISION,
            table_name="orders", column_name="amount",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies
        assert FixStrategy.SKIP in strategies
        # DECIMAL 변환 옵션은 requires_input
        decimal_opt = [o for o in options if o.requires_input]
        assert len(decimal_opt) >= 1

    def test_int_display_width_recommends_skip(self, generator):
        issue = make_issue(IssueType.INT_DISPLAY_WIDTH)
        options = generator.get_fix_options(issue)
        skip_opt = next(o for o in options if o.strategy == FixStrategy.SKIP)
        assert skip_opt.is_recommended is True

    def test_enum_empty_options(self, generator):
        issue = make_issue(IssueType.ENUM_EMPTY_VALUE)
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies

    def test_deprecated_engine_options(self, generator):
        issue = make_issue(
            IssueType.DEPRECATED_ENGINE,
            table_name="logs",
        )
        options = generator.get_fix_options(issue)
        innodb_opt = next(o for o in options if "InnoDB" in (o.sql_template or ""))
        assert innodb_opt.is_recommended is True

    def test_deprecated_engine_no_table_falls_back(self, generator):
        """table_name 없이 location에서 추출"""
        issue = make_issue(
            IssueType.DEPRECATED_ENGINE,
            location="test_db.logs",
        )
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies

    def test_unknown_issue_type_default_options(self, generator):
        """핸들러가 없는 타입이면 기본 옵션"""
        issue = make_issue(IssueType.RESERVED_KEYWORD)
        options = generator.get_fix_options(issue)
        strategies = [o.strategy for o in options]
        assert FixStrategy.MANUAL in strategies
        assert FixStrategy.SKIP in strategies

    def test_generate_sql_with_user_input(self, generator):
        """사용자 입력값 대체"""
        step = _make_step(
            0, IssueType.INVALID_DATE,
            selected_option=_make_option(
                FixStrategy.DATE_TO_CUSTOM,
                sql_template="UPDATE t SET c = '{custom_date}' WHERE c = '0000-00-00';",
                requires_input=True,
            ),
            user_input="2000-01-01",
        )
        sql = generator.generate_sql(step)
        assert "2000-01-01" in sql
        assert "{custom_date}" not in sql

    def test_generate_sql_no_option_returns_empty(self, generator):
        step = _make_step(0, IssueType.INVALID_DATE)
        assert generator.generate_sql(step) == ""

    def test_nullable_cache_hit(self, generator):
        """동일 컬럼 두 번째 호출 시 캐시 히트"""
        generator.connector.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'YES'}],
        }
        # 첫 호출
        result1 = generator._is_column_nullable("users", "name")
        # 결과 변경해도 캐시에서 반환
        generator.connector.query_results = {
            'IS_NULLABLE': [{'IS_NULLABLE': 'NO'}],
        }
        result2 = generator._is_column_nullable("users", "name")
        assert result1 == result2 is True
        # execute는 1번만 호출
        nullable_calls = [q for q, _ in generator.connector.executed_queries if 'IS_NULLABLE' in q]
        assert len(nullable_calls) == 1


# ============================================================
# CollationFKGraphBuilder 테스트
# ============================================================
class TestCollationFKGraphBuilder:
    def _make_builder(self, fk_rows):
        """FK 행 데이터로 빌더 생성"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': fk_rows,
        }
        builder = CollationFKGraphBuilder(conn, "test_db")
        builder.build_graph()
        return builder

    def test_empty_graph(self):
        builder = self._make_builder([])
        assert builder.get_related_tables("users") == set()

    def test_simple_fk(self):
        """단순 FK: orders → users"""
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
        ])
        related = builder.get_related_tables("users")
        assert "orders" in related

        related_from_orders = builder.get_related_tables("orders")
        assert "users" in related_from_orders

    def test_chain_fk(self):
        """체인 FK: order_items → orders → users"""
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            {'CHILD_TABLE': 'order_items', 'PARENT_TABLE': 'orders'},
        ])
        # users에서 시작하면 orders, order_items 모두 관련
        related = builder.get_related_tables("users")
        assert related == {"orders", "order_items"}

    def test_topological_order_parents_first(self):
        """위상 정렬: 부모가 먼저"""
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            {'CHILD_TABLE': 'order_items', 'PARENT_TABLE': 'orders'},
        ])
        order = builder.get_topological_order({"users", "orders", "order_items"})
        assert order.index("users") < order.index("orders")
        assert order.index("orders") < order.index("order_items")

    def test_topological_order_cyclic(self):
        """순환 참조 시 남은 노드 추가"""
        builder = self._make_builder([
            {'CHILD_TABLE': 'a', 'PARENT_TABLE': 'b'},
            {'CHILD_TABLE': 'b', 'PARENT_TABLE': 'a'},
        ])
        order = builder.get_topological_order({"a", "b"})
        # 순환이라 하나는 정상 큐에서 나오지 못해 remaining에 추가
        assert set(order) == {"a", "b"}

    def test_get_children(self):
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            {'CHILD_TABLE': 'reviews', 'PARENT_TABLE': 'users'},
        ])
        children = builder.get_children("users")
        assert children == {"orders", "reviews"}

    def test_get_parents(self):
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'products'},
        ])
        parents = builder.get_parents("orders")
        assert parents == {"users", "products"}

    def test_get_parents_returns_copy(self):
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
        ])
        p1 = builder.get_parents("orders")
        p1.add("fake")
        p2 = builder.get_parents("orders")
        assert "fake" not in p2

    def test_cascade_skip_propagates(self):
        """연쇄 건너뛰기: 부모를 건너뛰면 자식도 건너뛰기"""
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            {'CHILD_TABLE': 'order_items', 'PARENT_TABLE': 'orders'},
        ])
        target = {"users", "orders", "order_items"}
        skip_set = builder.get_cascade_skip_tables("users", target)
        assert "orders" in skip_set
        assert "order_items" in skip_set

    def test_cascade_skip_child_skips_parent(self):
        """자식 건너뛰기 → 부모도 건너뛰기 (FK 일관성)"""
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
        ])
        target = {"users", "orders"}
        skip_set = builder.get_cascade_skip_tables("orders", target)
        assert "users" in skip_set

    def test_unknown_table_no_related(self):
        builder = self._make_builder([
            {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
        ])
        assert builder.get_related_tables("unknown") == set()


# ============================================================
# FKSafeCharsetChanger 테스트
# ============================================================
class TestFKSafeCharsetChanger:
    def test_generate_safe_charset_sql_with_fks(self):
        """FK가 있는 경우 3-phase SQL 생성"""
        conn = FakeMySQLConnector()
        # REFERENTIAL_CONSTRAINTS 패턴이 먼저 매칭되도록 순서 조정
        # (get_related_fks 쿼리는 두 패턴을 모두 포함하므로)
        conn.query_results = {
            'REFERENTIAL_CONSTRAINTS': [
                {
                    'CONSTRAINT_NAME': 'fk_orders_user',
                    'TABLE_NAME': 'orders',
                    'COLUMN_NAME': 'user_id',
                    'REFERENCED_TABLE_NAME': 'users',
                    'REFERENCED_COLUMN_NAME': 'id',
                    'ORDINAL_POSITION': 1,
                    'DELETE_RULE': 'CASCADE',
                    'UPDATE_RULE': 'RESTRICT',
                }
            ],
            'KEY_COLUMN_USAGE kcu': [
                {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            ],
        }
        changer = FKSafeCharsetChanger(conn, "test_db")
        result = changer.generate_safe_charset_sql({"users", "orders"})

        assert 'drop_fks' in result
        assert 'alter_tables' in result
        assert 'add_fks' in result
        assert 'full_sql' in result
        assert result['fk_count'] == 1
        assert result['table_count'] == 2

    def test_generate_safe_charset_sql_no_fks(self):
        """FK가 없는 경우"""
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': [],
        }
        changer = FKSafeCharsetChanger(conn, "test_db")
        result = changer.generate_safe_charset_sql({"users"})

        assert result['fk_count'] == 0
        assert result['table_count'] == 1
        assert len(result['alter_tables']) == 1

    def test_generate_safe_charset_sql_empty_tables(self):
        conn = FakeMySQLConnector()
        changer = FKSafeCharsetChanger(conn, "test_db")
        result = changer.get_related_fks(set())
        assert result == []

    def test_dry_run_returns_sql_only(self):
        """dry_run=True이면 SQL만 반환하고 실행 안함"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        changer = FKSafeCharsetChanger(conn, "test_db")

        success, msg, result = changer.execute_safe_charset_change(
            {"users"}, dry_run=True
        )
        assert success is True
        assert "DRY-RUN" in msg


# ============================================================
# BatchFixExecutor 테스트
# ============================================================
class TestBatchFixExecutor:
    @pytest.fixture
    def executor(self):
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        return BatchFixExecutor(conn, "test_db")

    def test_dry_run_skip_step(self, executor):
        """SKIP 전략은 건너뛰기"""
        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                       selected_option=_make_option(FixStrategy.SKIP)),
        ]
        result = executor.execute_batch(steps, dry_run=True)
        assert isinstance(result, BatchExecutionResult)
        assert result.skip_count == 1
        assert result.success_count == 0

    def test_dry_run_manual_step(self, executor):
        """MANUAL/comment-only SQL은 건너뛰기"""
        steps = [
            _make_step(0, IssueType.ZEROFILL_USAGE,
                       selected_option=_make_option(
                           FixStrategy.MANUAL,
                           sql_template="-- 수동 처리 필요",
                       )),
        ]
        result = executor.execute_batch(steps, dry_run=True)
        assert result.skip_count == 1

    def test_dry_run_update_estimates_rows(self, executor):
        """DRY-RUN: UPDATE문은 COUNT로 영향 행 추정"""
        executor.connector.query_results['COUNT'] = [{'cnt': 42}]
        steps = [
            _make_step(0, IssueType.INVALID_DATE,
                       selected_option=_make_option(
                           FixStrategy.DATE_TO_NULL,
                           sql_template="UPDATE `test_db`.`orders` SET `col` = NULL WHERE `col` = '0000-00-00';",
                       )),
        ]
        result = executor.execute_batch(steps, dry_run=True)
        assert result.success_count == 1
        assert result.results[0].affected_rows == 42

    def test_dry_run_alter_no_estimate(self, executor):
        """DRY-RUN: ALTER TABLE은 영향 행 추정 불가"""
        steps = [
            _make_step(0, IssueType.CHARSET_ISSUE,
                       selected_option=_make_option(
                           FixStrategy.COLLATION_SINGLE,
                           sql_template="ALTER TABLE `test_db`.`users` CONVERT TO CHARACTER SET utf8mb4;",
                       )),
        ]
        result = executor.execute_batch(steps, dry_run=True)
        assert result.success_count == 1
        assert "DDL" in result.results[0].message

    def test_has_charset_issues_true(self, executor):
        steps = [
            _make_step(0, IssueType.CHARSET_ISSUE,
                       selected_option=_make_option(FixStrategy.COLLATION_SINGLE)),
        ]
        assert executor._has_charset_issues(steps) is True

    def test_has_charset_issues_skip_excluded(self, executor):
        steps = [
            _make_step(0, IssueType.CHARSET_ISSUE,
                       selected_option=_make_option(FixStrategy.SKIP)),
        ]
        assert executor._has_charset_issues(steps) is False

    def test_has_charset_issues_fk_safe_excluded(self, executor):
        """COLLATION_FK_SAFE는 자체 FK 관리하므로 제외"""
        steps = [
            _make_step(0, IssueType.CHARSET_ISSUE,
                       selected_option=_make_option(FixStrategy.COLLATION_FK_SAFE)),
        ]
        assert executor._has_charset_issues(steps) is False

    def test_progress_callback(self, executor):
        """진행 콜백이 호출되는지 확인"""
        logs = []
        executor.set_progress_callback(lambda msg: logs.append(msg))

        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                       selected_option=_make_option(FixStrategy.SKIP)),
        ]
        executor.execute_batch(steps, dry_run=True)
        assert len(logs) > 0

    def test_batch_result_totals(self, executor):
        """배치 결과 합계"""
        executor.connector.query_results['COUNT'] = [{'cnt': 10}]
        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                       selected_option=_make_option(FixStrategy.SKIP)),
            _make_step(1, IssueType.INVALID_DATE,
                       selected_option=_make_option(
                           FixStrategy.DATE_TO_NULL,
                           sql_template="UPDATE t SET c = NULL WHERE c = '0000-00-00';",
                       )),
            _make_step(2, IssueType.ZEROFILL_USAGE,
                       selected_option=_make_option(
                           FixStrategy.MANUAL,
                           sql_template="-- 수동",
                       )),
        ]
        result = executor.execute_batch(steps, dry_run=True)
        assert result.total_steps == 3
        assert result.skip_count == 2  # SKIP + MANUAL
        assert result.success_count == 1
        assert result.fail_count == 0

    def test_user_input_substitution(self, executor):
        """사용자 입력값이 SQL에 대체되는지 확인"""
        executor.connector.query_results['COUNT'] = [{'cnt': 5}]
        steps = [
            _make_step(0, IssueType.INVALID_DATE,
                       selected_option=_make_option(
                           FixStrategy.DATE_TO_CUSTOM,
                           sql_template="UPDATE t SET c = '{custom_date}' WHERE c = '0000-00-00';",
                           requires_input=True,
                       ),
                       user_input="2000-01-01"),
        ]
        result = executor.execute_batch(steps, dry_run=True)
        assert result.success_count == 1


# ============================================================
# RollbackSQLGenerator 테스트
# ============================================================
class TestRollbackSQLGenerator:
    @pytest.fixture
    def rollback_gen(self):
        conn = FakeMySQLConnector()
        return RollbackSQLGenerator(conn, "test_db")

    def test_capture_table_charset(self, rollback_gen):
        rollback_gen.connector.query_results = {
            'INFORMATION_SCHEMA.TABLES': [{
                'TABLE_NAME': 'users',
                'TABLE_COLLATION': 'utf8_general_ci',
                'TABLE_CHARSET': 'utf8',
            }],
        }
        info = rollback_gen.capture_table_charset("users")
        assert info['charset'] == 'utf8'
        assert info['collation'] == 'utf8_general_ci'

    def test_capture_table_charset_not_found(self, rollback_gen):
        info = rollback_gen.capture_table_charset("nonexistent")
        assert info['charset'] == 'utf8mb3'
        assert info['collation'] == 'utf8mb3_general_ci'

    def test_capture_table_charset_cache(self, rollback_gen):
        rollback_gen.connector.query_results = {
            'INFORMATION_SCHEMA.TABLES': [{
                'TABLE_NAME': 'users',
                'TABLE_COLLATION': 'utf8_general_ci',
                'TABLE_CHARSET': 'utf8',
            }],
        }
        rollback_gen.capture_table_charset("users")
        rollback_gen.capture_table_charset("users")  # 캐시 히트
        charset_calls = [
            q for q, _ in rollback_gen.connector.executed_queries
            if 'TABLE_COLLATION' in q
        ]
        assert len(charset_calls) == 1

    def test_capture_column_info(self, rollback_gen):
        rollback_gen.connector.query_results = {
            'INFORMATION_SCHEMA.COLUMNS': [{
                'COLUMN_NAME': 'name',
                'COLUMN_TYPE': 'varchar(255)',
                'IS_NULLABLE': 'YES',
                'COLUMN_DEFAULT': None,
                'CHARACTER_SET_NAME': 'utf8',
                'COLLATION_NAME': 'utf8_general_ci',
                'EXTRA': '',
            }],
        }
        info = rollback_gen.capture_column_info("users", "name")
        assert info['CHARACTER_SET_NAME'] == 'utf8'

    def test_generate_rollback_skip(self, rollback_gen):
        step = _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                          selected_option=_make_option(FixStrategy.SKIP))
        sql = rollback_gen.generate_rollback_sql(step)
        assert sql == ""

    def test_generate_rollback_manual(self, rollback_gen):
        step = _make_step(0, IssueType.ZEROFILL_USAGE,
                          selected_option=_make_option(FixStrategy.MANUAL))
        sql = rollback_gen.generate_rollback_sql(step)
        assert sql == ""

    def test_generate_rollback_date_warning(self, rollback_gen):
        """날짜 변경은 원본 값을 알 수 없어 경고만"""
        step = _make_step(
            0, IssueType.INVALID_DATE,
            location="test_db.orders.created_at",
            selected_option=_make_option(FixStrategy.DATE_TO_NULL),
        )
        sql = rollback_gen.generate_rollback_sql(step)
        assert "롤백 불가" in sql

    def test_generate_rollback_collation_single_table(self, rollback_gen):
        """COLLATION_SINGLE 테이블 레벨 롤백 SQL"""
        step = _make_step(
            0, IssueType.CHARSET_ISSUE,
            location="test_db.users",
            selected_option=_make_option(FixStrategy.COLLATION_SINGLE),
        )
        original = {'charset': 'utf8', 'collation': 'utf8_general_ci'}
        sql = rollback_gen.generate_rollback_sql(step, original_state=original)
        assert "CONVERT TO CHARACTER SET utf8" in sql
        assert "utf8_general_ci" in sql

    def test_generate_rollback_collation_single_column(self, rollback_gen):
        """COLLATION_SINGLE 컬럼 레벨 롤백 SQL"""
        step = _make_step(
            0, IssueType.CHARSET_ISSUE,
            location="test_db.users.name",
            selected_option=_make_option(FixStrategy.COLLATION_SINGLE),
        )
        original = {
            'CHARACTER_SET_NAME': 'utf8',
            'COLLATION_NAME': 'utf8_general_ci',
            'COLUMN_TYPE': 'varchar(255)',
            'IS_NULLABLE': 'YES',
        }
        sql = rollback_gen.generate_rollback_sql(step, original_state=original)
        assert "MODIFY COLUMN `name`" in sql
        assert "CHARACTER SET utf8" in sql

    def test_generate_rollback_fk_cascade(self, rollback_gen):
        """FK CASCADE 롤백 SQL"""
        step = _make_step(
            0, IssueType.CHARSET_ISSUE,
            location="test_db.users",
            selected_option=_make_option(
                FixStrategy.COLLATION_FK_CASCADE,
                related_tables=["users", "orders"],
            ),
        )
        pre_states = {
            'test_db.users': {'charset': 'utf8', 'collation': 'utf8_general_ci'},
            'test_db.orders': {'charset': 'utf8', 'collation': 'utf8_general_ci'},
        }
        sql = rollback_gen.generate_rollback_sql(step, all_pre_states=pre_states)
        assert "users" in sql
        assert "orders" in sql
        assert "CONVERT TO CHARACTER SET utf8" in sql

    def test_generate_batch_rollback(self, rollback_gen):
        """배치 롤백 SQL 생성"""
        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                       location="test_db.products.stock",
                       selected_option=_make_option(FixStrategy.SKIP)),
            _make_step(1, IssueType.CHARSET_ISSUE,
                       location="test_db.users",
                       selected_option=_make_option(
                           FixStrategy.COLLATION_SINGLE,
                       )),
        ]
        pre_states = {
            'test_db.users': {'charset': 'utf8', 'collation': 'utf8_general_ci'},
        }
        sql = rollback_gen.generate_batch_rollback(steps, pre_states)
        assert "ROLLBACK SQL" in sql
        assert "users" in sql
        # SKIP step은 롤백 대상 아님
        assert "products" not in sql

    def test_generate_batch_rollback_empty(self, rollback_gen):
        """롤백 대상 없는 경우"""
        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                       selected_option=_make_option(FixStrategy.SKIP)),
        ]
        sql = rollback_gen.generate_batch_rollback(steps, {})
        assert "롤백 가능한 변경사항이 없습니다" in sql

    # --- Bug 1 regression: 같은 테이블의 여러 컬럼 ---
    def test_generate_batch_rollback_multi_column_same_table(self, rollback_gen):
        """Bug 1 회귀: 동일 테이블의 여러 컬럼이 모두 롤백 SQL에 포함돼야 한다"""
        steps = [
            _make_step(0, IssueType.CHARSET_ISSUE,
                       location="test_db.users.name",
                       selected_option=_make_option(FixStrategy.COLLATION_SINGLE)),
            _make_step(1, IssueType.CHARSET_ISSUE,
                       location="test_db.users.email",
                       selected_option=_make_option(FixStrategy.COLLATION_SINGLE)),
            _make_step(2, IssueType.CHARSET_ISSUE,
                       location="test_db.users.bio",
                       selected_option=_make_option(FixStrategy.COLLATION_SINGLE)),
        ]
        col_state = {
            'CHARACTER_SET_NAME': 'utf8', 'COLLATION_NAME': 'utf8_general_ci',
            'COLUMN_TYPE': 'varchar(255)', 'IS_NULLABLE': 'YES', 'COLUMN_DEFAULT': None,
            'EXTRA': '',
        }
        pre_states = {
            'test_db.users.name': col_state,
            'test_db.users.email': col_state,
            'test_db.users.bio': col_state,
        }
        sql = rollback_gen.generate_batch_rollback(steps, pre_states)
        # 세 컬럼 모두 MODIFY COLUMN 절이 있어야 한다
        assert sql.count('MODIFY COLUMN `name`') == 1
        assert sql.count('MODIFY COLUMN `email`') == 1
        assert sql.count('MODIFY COLUMN `bio`') == 1

    # --- Bug 2 regression: DEFAULT / EXTRA 보존 ---
    def test_format_default_clause_string(self, rollback_gen):
        """문자열 DEFAULT → 따옴표 포함"""
        col_info = {'COLUMN_DEFAULT': 'active', 'COLUMN_TYPE': 'varchar(20)',
                    'IS_NULLABLE': 'NO'}
        result = rollback_gen._format_default_clause(col_info)
        assert result == "DEFAULT 'active'"

    def test_format_default_clause_numeric(self, rollback_gen):
        """숫자형 DEFAULT → 따옴표 없음"""
        col_info = {'COLUMN_DEFAULT': '0', 'COLUMN_TYPE': 'int(11)',
                    'IS_NULLABLE': 'NO'}
        result = rollback_gen._format_default_clause(col_info)
        assert result == "DEFAULT 0"

    def test_format_default_clause_current_timestamp(self, rollback_gen):
        """CURRENT_TIMESTAMP → 따옴표 없음"""
        col_info = {'COLUMN_DEFAULT': 'CURRENT_TIMESTAMP', 'COLUMN_TYPE': 'timestamp',
                    'IS_NULLABLE': 'NO'}
        result = rollback_gen._format_default_clause(col_info)
        assert result == "DEFAULT CURRENT_TIMESTAMP"

    def test_format_default_clause_nullable_none(self, rollback_gen):
        """IS_NULLABLE='YES', COLUMN_DEFAULT=None → DEFAULT NULL"""
        col_info = {'COLUMN_DEFAULT': None, 'COLUMN_TYPE': 'varchar(255)',
                    'IS_NULLABLE': 'YES'}
        result = rollback_gen._format_default_clause(col_info)
        assert result == "DEFAULT NULL"

    def test_format_default_clause_not_null_none(self, rollback_gen):
        """IS_NULLABLE='NO', COLUMN_DEFAULT=None → 빈 문자열"""
        col_info = {'COLUMN_DEFAULT': None, 'COLUMN_TYPE': 'varchar(255)',
                    'IS_NULLABLE': 'NO'}
        result = rollback_gen._format_default_clause(col_info)
        assert result == ""

    def test_format_extra_clause_auto_increment(self, rollback_gen):
        """AUTO_INCREMENT → 포함"""
        assert rollback_gen._format_extra_clause({'EXTRA': 'auto_increment'}) == 'AUTO_INCREMENT'

    def test_format_extra_clause_on_update(self, rollback_gen):
        """ON UPDATE CURRENT_TIMESTAMP → 포함"""
        assert rollback_gen._format_extra_clause(
            {'EXTRA': 'DEFAULT_GENERATED on update CURRENT_TIMESTAMP'}
        ) == 'ON UPDATE CURRENT_TIMESTAMP'

    def test_format_extra_clause_empty(self, rollback_gen):
        """EXTRA 없음 → 빈 문자열"""
        assert rollback_gen._format_extra_clause({'EXTRA': ''}) == ''

    def test_rollback_column_includes_default(self, rollback_gen):
        """컬럼 롤백 SQL에 DEFAULT 절이 포함돼야 한다"""
        step = _make_step(0, IssueType.CHARSET_ISSUE,
                          location="test_db.users.status",
                          selected_option=_make_option(FixStrategy.COLLATION_SINGLE))
        original = {
            'CHARACTER_SET_NAME': 'utf8',
            'COLLATION_NAME': 'utf8_general_ci',
            'COLUMN_TYPE': 'varchar(20)',
            'IS_NULLABLE': 'NO',
            'COLUMN_DEFAULT': 'active',
            'EXTRA': '',
        }
        sql = rollback_gen.generate_rollback_sql(step, original_state=original)
        assert "DEFAULT 'active'" in sql
        assert "MODIFY COLUMN `status`" in sql

    def test_rollback_column_on_update_preserved(self, rollback_gen):
        """ON UPDATE CURRENT_TIMESTAMP가 롤백 SQL에 보존돼야 한다"""
        step = _make_step(0, IssueType.CHARSET_ISSUE,
                          location="test_db.users.updated_at",
                          selected_option=_make_option(FixStrategy.COLLATION_SINGLE))
        original = {
            'CHARACTER_SET_NAME': 'utf8',
            'COLLATION_NAME': 'utf8_general_ci',
            'COLUMN_TYPE': 'timestamp',
            'IS_NULLABLE': 'YES',
            'COLUMN_DEFAULT': 'CURRENT_TIMESTAMP',
            'EXTRA': 'DEFAULT_GENERATED on update CURRENT_TIMESTAMP',
        }
        sql = rollback_gen.generate_rollback_sql(step, original_state=original)
        assert "ON UPDATE CURRENT_TIMESTAMP" in sql
        assert "DEFAULT CURRENT_TIMESTAMP" in sql


# ============================================================
# CharsetFixPlanBuilder 테스트
# ============================================================
class TestCharsetFixPlanBuilder:
    def test_build_full_table_list(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': [
                {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            ],
            'TABLE_COLLATION': [{
                'TABLE_COLLATION': 'utf8_general_ci',
                'TABLE_CHARSET': 'utf8',
            }],
        }
        builder = CharsetFixPlanBuilder(conn, "test_db", {"users"})
        table_list = builder.build_full_table_list()

        names = [t.table_name for t in table_list]
        assert "users" in names
        assert "orders" in names

        # users는 original_issue
        user_info = next(t for t in table_list if t.table_name == "users")
        assert user_info.is_original_issue is True

        # orders는 FK로 추가됨
        order_info = next(t for t in table_list if t.table_name == "orders")
        assert order_info.is_original_issue is False

    def test_build_full_table_list_no_fk(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': [],
            'TABLE_COLLATION': [{
                'TABLE_COLLATION': 'utf8_general_ci',
                'TABLE_CHARSET': 'utf8',
            }],
        }
        builder = CharsetFixPlanBuilder(conn, "test_db", {"users"})
        table_list = builder.build_full_table_list()
        assert len(table_list) == 1
        assert table_list[0].table_name == "users"

    def test_generate_fix_sql_empty(self):
        conn = FakeMySQLConnector()
        builder = CharsetFixPlanBuilder(conn, "test_db", set())
        result = builder.generate_fix_sql(set())
        assert result['fk_count'] == 0
        assert result['table_count'] == 0

    def test_cascade_skip(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': [
                {'CHILD_TABLE': 'orders', 'PARENT_TABLE': 'users'},
            ],
            'TABLE_COLLATION': [{
                'TABLE_COLLATION': 'utf8_general_ci',
                'TABLE_CHARSET': 'utf8',
            }],
        }
        builder = CharsetFixPlanBuilder(conn, "test_db", {"users", "orders"})
        cascade = builder.get_cascade_skip_tables("users")
        assert "orders" in cascade


# ============================================================
# create_wizard_steps 테스트
# ============================================================
class TestCreateWizardSteps:
    def test_creates_steps_for_all_issues(self):
        conn = FakeMySQLConnector()
        conn.query_results = {
            'KEY_COLUMN_USAGE': [],
            'IS_NULLABLE': [{'IS_NULLABLE': 'YES'}],
        }
        issues = [
            make_issue(IssueType.INT_DISPLAY_WIDTH),
            make_issue(IssueType.ZEROFILL_USAGE),
            make_issue(IssueType.INVALID_DATE, table_name="t", column_name="c"),
        ]
        steps = create_wizard_steps(issues, conn, "test_db")

        assert len(steps) == 3
        assert all(isinstance(s, FixWizardStep) for s in steps)
        assert steps[0].issue_index == 0
        assert steps[1].issue_index == 1
        assert steps[2].issue_index == 2

    def test_each_step_has_options(self):
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        issues = [make_issue(IssueType.INT_DISPLAY_WIDTH)]
        steps = create_wizard_steps(issues, conn, "test_db")
        assert len(steps[0].options) >= 1

    def test_all_steps_have_skip(self):
        """모든 step에 SKIP 옵션이 포함되어야 함"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        issues = [
            make_issue(IssueType.INT_DISPLAY_WIDTH),
            make_issue(IssueType.DEPRECATED_ENGINE, table_name="t"),
            make_issue(IssueType.ENUM_EMPTY_VALUE),
        ]
        steps = create_wizard_steps(issues, conn, "test_db")
        for step in steps:
            strategies = [o.strategy for o in step.options]
            assert FixStrategy.SKIP in strategies, f"Step {step.issue_index} has no SKIP"


# ============================================================
# Dataclass 기본 동작 테스트
# ============================================================
class TestDataclasses:
    def test_fix_option_defaults(self):
        opt = FixOption(
            strategy=FixStrategy.SKIP,
            label="skip",
            description="skip",
        )
        assert opt.requires_input is False
        assert opt.is_recommended is False
        assert opt.related_tables == []
        assert opt.sql_template is None

    def test_fix_wizard_step_defaults(self):
        step = FixWizardStep(
            issue_index=0,
            issue_type=IssueType.CHARSET_ISSUE,
            location="test_db.users",
            description="test",
            options=[],
        )
        assert step.selected_option is None
        assert step.user_input is None
        assert step.included_by is None
        assert step.included_reason == ""

    def test_fix_execution_result(self):
        r = FixExecutionResult(
            success=True,
            message="ok",
            sql_executed="SELECT 1;",
            affected_rows=5,
        )
        assert r.error is None

    def test_batch_execution_result(self):
        r = BatchExecutionResult(
            total_steps=3,
            success_count=2,
            fail_count=0,
            skip_count=1,
            results=[],
        )
        assert r.total_affected_rows == 0
        assert r.rollback_sql == ""

    def test_charset_table_info(self):
        info = CharsetTableInfo(
            table_name="users",
            current_charset="utf8",
            current_collation="utf8_general_ci",
            fk_parents=[],
            fk_children=["orders"],
            is_original_issue=True,
        )
        assert info.skip is False


# ============================================================
# Session Guard & 2-Phase Bookkeeping Fault-Injection 테스트
# ============================================================
def _make_charset_step(idx, col_name, table="users", schema="test_db"):
    """COLLATION_SINGLE column-level 스텝 헬퍼"""
    return _make_step(
        idx, IssueType.CHARSET_ISSUE,
        location=f"{schema}.{table}.{col_name}",
        selected_option=_make_option(
            FixStrategy.COLLATION_SINGLE,
            sql_template=(
                f"ALTER TABLE `{schema}`.`{table}`"
                f" MODIFY COLUMN `{col_name}` VARCHAR(255) CHARACTER SET utf8mb4;"
            ),
            modify_clause=f"`{col_name}` VARCHAR(255) CHARACTER SET utf8mb4",
        )
    )


def _make_executor_with_session_mocks():
    """dry_run=False 지원을 위해 세션 메서드가 mocking된 executor 반환"""
    conn = FakeMySQLConnector()
    conn.query_results = {'KEY_COLUMN_USAGE': []}
    conn.get_session_sql_mode = MagicMock(return_value='STRICT_TRANS_TABLES')
    conn.set_session_sql_mode = MagicMock(return_value=True)
    return BatchFixExecutor(conn, "test_db")


class TestSessionGuardFaultInjection:
    """_session_guard try/finally 및 2-phase bookkeeping 오류 주입 테스트"""

    def test_session_restored_on_exception_in_main_loop(self):
        """메인 루프 도중 예외가 발생해도 sql_mode가 복원된다"""
        executor = _make_executor_with_session_mocks()
        step = _make_step(
            0, IssueType.CHARSET_ISSUE,
            location="test_db.users",
            selected_option=_make_option(
                FixStrategy.COLLATION_SINGLE,
                sql_template="ALTER TABLE `test_db`.`users` CONVERT TO CHARACTER SET utf8mb4;",
            )
        )

        with patch.object(executor, '_execute_single', side_effect=RuntimeError("injected")):
            with pytest.raises(RuntimeError):
                executor.execute_batch([step], dry_run=False)

        # set_session_sql_mode 호출 시퀀스: [call('')(설정), call('STRICT...')(복원)]
        # 마지막 호출이 원래 sql_mode로 복원이어야 함
        calls = executor.connector.set_session_sql_mode.call_args_list
        assert len(calls) >= 1
        assert calls[-1].args[0] == 'STRICT_TRANS_TABLES', (
            f"마지막 복원 호출이 원래 sql_mode여야 함: {calls}"
        )

    def test_session_restored_with_empty_original_sql_mode(self):
        """original sql_mode='' 일 때도 복원 호출된다 (빈 문자열 falsy 버그 방지)"""
        executor = _make_executor_with_session_mocks()
        executor.connector.get_session_sql_mode = MagicMock(return_value='')
        step = _make_step(
            0, IssueType.CHARSET_ISSUE,
            location="test_db.users",
            selected_option=_make_option(
                FixStrategy.COLLATION_SINGLE,
                sql_template="ALTER TABLE `test_db`.`users` CONVERT TO CHARACTER SET utf8mb4;",
            )
        )

        with patch.object(executor, '_execute_single', side_effect=RuntimeError("injected")):
            with pytest.raises(RuntimeError):
                executor.execute_batch([step], dry_run=False)

        # '' is not None → 빈 문자열로도 복원 호출되어야 함
        calls = executor.connector.set_session_sql_mode.call_args_list
        assert len(calls) >= 1, "set_session_sql_mode가 한 번도 호출되지 않음"
        assert calls[-1].args[0] == '', (
            f"마지막 복원 호출이 빈 문자열이어야 함: {calls}"
        )

    def test_collation_single_merge_all_steps_in_results(self):
        """같은 테이블 3개 column이 병합될 때 모든 step이 results에 정확히 1번 포함된다"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        executor = BatchFixExecutor(conn, "test_db")

        steps = [_make_charset_step(i, f"col{i}") for i in range(3)]

        result = executor.execute_batch(steps, dry_run=True)

        assert result.total_steps == 3
        assert len(result.results) == 3
        assert result.success_count + result.fail_count + result.skip_count == 3

    def test_merge_fallback_all_steps_in_results(self):
        """병합 ALTER 실패 시 fallback 개별 실행으로 모든 step이 results에 포함된다"""
        executor = _make_executor_with_session_mocks()

        steps = [_make_charset_step(i, f"col{i}") for i in range(2)]

        def _execute_side_effect(sql):
            # 두 컬럼을 한꺼번에 포함하는 merged SQL은 실패
            if '`col0`' in sql and '`col1`' in sql:
                return FixExecutionResult(
                    success=False, message="merge failed", sql_executed=sql
                )
            return FixExecutionResult(
                success=True, message="실행 완료", sql_executed=sql, affected_rows=1
            )

        with patch.object(executor, '_execute_single', side_effect=_execute_side_effect):
            result = executor.execute_batch(steps, dry_run=False)

        assert result.total_steps == 2
        assert len(result.results) == 2
        assert result.success_count == 2
        assert result.fail_count == 0

    def test_no_duplicate_step_processing_after_merge(self):
        """병합 처리된 step은 메인 루프에서 중복 처리되지 않는다"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        executor = BatchFixExecutor(conn, "test_db")

        steps = [_make_charset_step(i, f"col{i}") for i in range(2)]

        executed_sqls = []

        def _execute_side_effect(sql):
            executed_sqls.append(sql)
            return FixExecutionResult(
                success=True, message="실행 완료", sql_executed=sql, affected_rows=1
            )

        with patch.object(executor, '_execute_single', side_effect=_execute_side_effect):
            result = executor.execute_batch(steps, dry_run=False)

        # 병합되면 1번만 _execute_single 호출 (2번이 아님)
        assert len(executed_sqls) == 1, (
            f"병합된 step이 중복 실행됨: {len(executed_sqls)}번 호출"
        )
        assert result.total_steps == 2
        assert len(result.results) == 2

    def test_accounting_consistent_after_mixed_steps(self):
        """SKIP + COLLATION_SINGLE 혼합 시 accounting이 일관적이다"""
        conn = FakeMySQLConnector()
        conn.query_results = {'KEY_COLUMN_USAGE': []}
        executor = BatchFixExecutor(conn, "test_db")

        steps = [
            _make_step(0, IssueType.INT_DISPLAY_WIDTH,
                       selected_option=_make_option(FixStrategy.SKIP)),
            _make_charset_step(1, "name"),
            _make_charset_step(2, "email"),  # 같은 테이블 → 병합
        ]

        result = executor.execute_batch(steps, dry_run=True)

        assert result.total_steps == 3
        assert len(result.results) == 3
        assert result.success_count + result.fail_count + result.skip_count == 3
