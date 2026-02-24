"""
SeverityClassifier 단위 테스트
- 심각도 분류 규칙 검증
- CompareLevel 별 비교 범위 검증
- SeveritySummary 집계 검증
"""
import pytest

from src.core.schema_diff import (
    DiffType, DiffSeverity, CompareLevel,
    ColumnInfo, IndexInfo, ForeignKeyInfo,
    TableSchema, ColumnDiff, IndexDiff, ForeignKeyDiff, TableDiff,
    SeverityClassifier, VersionContext, SeveritySummary,
    SchemaComparator,
)


@pytest.fixture
def classifier():
    return SeverityClassifier()


@pytest.fixture
def classifier_with_version():
    ctx = VersionContext(
        source_version=(8, 4, 6),
        target_version=(8, 0, 42),
        source_version_str="8.4.6",
        target_version_str="8.0.42",
    )
    return SeverityClassifier(ctx)


# ============================================================
# 테이블 레벨 심각도
# ============================================================

class TestTableSeverity:

    def test_table_added_is_critical(self, classifier):
        diffs = [TableDiff(
            table_name="new_table",
            diff_type=DiffType.ADDED,
            source_schema=TableSchema(name="new_table"),
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].severity == DiffSeverity.CRITICAL
        assert summary.critical == 1

    def test_table_removed_is_critical(self, classifier):
        diffs = [TableDiff(
            table_name="old_table",
            diff_type=DiffType.REMOVED,
            target_schema=TableSchema(name="old_table"),
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].severity == DiffSeverity.CRITICAL
        assert summary.critical == 1

    def test_table_unchanged_no_severity(self, classifier):
        diffs = [TableDiff(
            table_name="ok_table",
            diff_type=DiffType.UNCHANGED,
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].severity is None
        assert summary.critical == 0
        assert summary.warning == 0
        assert summary.info == 0


# ============================================================
# 컬럼 레벨 심각도
# ============================================================

class TestColumnSeverity:

    def test_column_added_is_critical(self, classifier):
        col_diff = ColumnDiff(
            column_name="new_col",
            diff_type=DiffType.ADDED,
            source_info=ColumnInfo(name="new_col", data_type="varchar(255)",
                                   nullable=True, default=None),
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.CRITICAL

    def test_column_removed_is_critical(self, classifier):
        col_diff = ColumnDiff(
            column_name="old_col",
            diff_type=DiffType.REMOVED,
            target_info=ColumnInfo(name="old_col", data_type="int",
                                   nullable=True, default=None),
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.CRITICAL

    def test_type_change_varchar_to_int_critical(self, classifier):
        col_diff = ColumnDiff(
            column_name="col1",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="col1", data_type="varchar(100)",
                                   nullable=True, default=None),
            target_info=ColumnInfo(name="col1", data_type="int",
                                   nullable=True, default=None),
            differences=["타입: varchar(100) → int"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.CRITICAL

    def test_int_display_width_is_info(self, classifier):
        """int(11) vs int → Info (MySQL 버전 차이에 의한 표현 차이)"""
        col_diff = ColumnDiff(
            column_name="id",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="id", data_type="int",
                                   nullable=False, default=None),
            target_info=ColumnInfo(name="id", data_type="int(11)",
                                   nullable=False, default=None),
            differences=["타입: int → int(11)"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.INFO
        assert summary.info == 1

    def test_bigint_display_width_is_info(self, classifier):
        """bigint(20) vs bigint → Info"""
        col_diff = ColumnDiff(
            column_name="big_id",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="big_id", data_type="bigint",
                                   nullable=False, default=None),
            target_info=ColumnInfo(name="big_id", data_type="bigint(20)",
                                   nullable=False, default=None),
            differences=["타입: bigint → bigint(20)"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.INFO

    def test_tinyint_display_width_is_info(self, classifier):
        """tinyint(1) vs tinyint → Info"""
        col_diff = ColumnDiff(
            column_name="flag",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="flag", data_type="tinyint",
                                   nullable=False, default=None),
            target_info=ColumnInfo(name="flag", data_type="tinyint(1)",
                                   nullable=False, default=None),
            differences=["타입: tinyint → tinyint(1)"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.INFO

    def test_nullable_change_is_warning(self, classifier):
        col_diff = ColumnDiff(
            column_name="col1",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="col1", data_type="varchar(100)",
                                   nullable=False, default=None),
            target_info=ColumnInfo(name="col1", data_type="varchar(100)",
                                   nullable=True, default=None),
            differences=["Nullable: NOT NULL → NULL"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.WARNING

    def test_default_change_is_warning(self, classifier):
        col_diff = ColumnDiff(
            column_name="col1",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="col1", data_type="int",
                                   nullable=True, default="0"),
            target_info=ColumnInfo(name="col1", data_type="int",
                                   nullable=True, default="1"),
            differences=["Default: 0 → 1"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.WARNING

    def test_auto_increment_change_is_critical(self, classifier):
        col_diff = ColumnDiff(
            column_name="id",
            diff_type=DiffType.MODIFIED,
            source_info=ColumnInfo(name="id", data_type="int",
                                   nullable=False, default=None,
                                   extra="auto_increment"),
            target_info=ColumnInfo(name="id", data_type="int",
                                   nullable=False, default=None, extra=""),
            differences=["Extra: auto_increment → "],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[col_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].column_diffs[0].severity == DiffSeverity.CRITICAL


# ============================================================
# 인덱스 레벨 심각도
# ============================================================

class TestIndexSeverity:

    def test_primary_key_change_is_critical(self, classifier):
        idx_diff = IndexDiff(
            index_name="PRIMARY",
            diff_type=DiffType.MODIFIED,
            source_info=IndexInfo(name="PRIMARY", columns=["id"], unique=True),
            target_info=IndexInfo(name="PRIMARY", columns=["id", "sub_id"], unique=True),
            differences=["컬럼: ['id'] → ['id', 'sub_id']"],
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            index_diffs=[idx_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].index_diffs[0].severity == DiffSeverity.CRITICAL

    def test_index_change_is_warning(self, classifier):
        idx_diff = IndexDiff(
            index_name="idx_name",
            diff_type=DiffType.ADDED,
            source_info=IndexInfo(name="idx_name", columns=["col1"]),
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            index_diffs=[idx_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].index_diffs[0].severity == DiffSeverity.WARNING

    def test_index_unchanged_no_severity(self, classifier):
        idx_diff = IndexDiff(
            index_name="idx_name",
            diff_type=DiffType.UNCHANGED,
            source_info=IndexInfo(name="idx_name", columns=["col1"]),
            target_info=IndexInfo(name="idx_name", columns=["col1"]),
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.UNCHANGED,
            index_diffs=[idx_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].index_diffs[0].severity is None


# ============================================================
# FK 레벨 심각도
# ============================================================

class TestForeignKeySeverity:

    def test_fk_change_is_warning(self, classifier):
        fk_diff = ForeignKeyDiff(
            fk_name="fk_user",
            diff_type=DiffType.ADDED,
            source_info=ForeignKeyInfo(
                name="fk_user", columns=["user_id"],
                ref_table="users", ref_columns=["id"],
            ),
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            fk_diffs=[fk_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].fk_diffs[0].severity == DiffSeverity.WARNING

    def test_fk_unchanged_no_severity(self, classifier):
        fk_diff = ForeignKeyDiff(
            fk_name="fk_user",
            diff_type=DiffType.UNCHANGED,
        )
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.UNCHANGED,
            fk_diffs=[fk_diff],
        )]
        result, summary = classifier.classify(diffs)
        assert result[0].fk_diffs[0].severity is None


# ============================================================
# SeveritySummary 집계
# ============================================================

class TestSeveritySummary:

    def test_summary_counts(self, classifier):
        """여러 심각도가 섞인 경우 정확한 집계"""
        diffs = [
            # Critical: 테이블 추가
            TableDiff(table_name="t1", diff_type=DiffType.ADDED,
                      source_schema=TableSchema(name="t1")),
            # Modified 테이블 (컬럼에 Warning + Info 혼합)
            TableDiff(
                table_name="t2", diff_type=DiffType.MODIFIED,
                column_diffs=[
                    ColumnDiff(
                        column_name="c1", diff_type=DiffType.MODIFIED,
                        source_info=ColumnInfo(name="c1", data_type="int",
                                               nullable=True, default=None),
                        target_info=ColumnInfo(name="c1", data_type="int(11)",
                                               nullable=True, default=None),
                        differences=["타입: int → int(11)"],
                    ),
                    ColumnDiff(
                        column_name="c2", diff_type=DiffType.MODIFIED,
                        source_info=ColumnInfo(name="c2", data_type="varchar(100)",
                                               nullable=False, default=None),
                        target_info=ColumnInfo(name="c2", data_type="varchar(100)",
                                               nullable=True, default=None),
                        differences=["Nullable: NOT NULL → NULL"],
                    ),
                ],
                index_diffs=[
                    IndexDiff(index_name="idx_c2", diff_type=DiffType.ADDED,
                              source_info=IndexInfo(name="idx_c2", columns=["c2"])),
                ],
            ),
        ]

        result, summary = classifier.classify(diffs)

        # t1: Critical (테이블 추가)
        # c1: Info (int display width)
        # c2: Warning (nullable 변경)
        # idx_c2: Warning (인덱스 추가)
        assert summary.critical == 1
        assert summary.warning == 2
        assert summary.info == 1
        assert summary.has_critical is True

    def test_summary_no_critical(self, classifier):
        diffs = [TableDiff(
            table_name="t", diff_type=DiffType.MODIFIED,
            column_diffs=[
                ColumnDiff(
                    column_name="c1", diff_type=DiffType.MODIFIED,
                    source_info=ColumnInfo(name="c1", data_type="int",
                                           nullable=True, default="0"),
                    target_info=ColumnInfo(name="c1", data_type="int",
                                           nullable=True, default="1"),
                    differences=["Default: 0 → 1"],
                ),
            ],
        )]
        result, summary = classifier.classify(diffs)
        assert summary.has_critical is False


# ============================================================
# CompareLevel 통합 테스트
# ============================================================

class TestCompareLevelIntegration:

    def _make_tables(self):
        """테스트용 소스/타겟 테이블 생성"""
        source = TableSchema(
            name="t1",
            columns=[
                ColumnInfo(name="id", data_type="int", nullable=False,
                           default=None, extra="auto_increment", key="PRI"),
                ColumnInfo(name="name", data_type="varchar(100)", nullable=True,
                           default=None, charset="utf8mb4",
                           collation="utf8mb4_unicode_ci"),
            ],
            indexes=[
                IndexInfo(name="PRIMARY", columns=["id"], unique=True),
                IndexInfo(name="idx_name", columns=["name"]),
            ],
            foreign_keys=[
                ForeignKeyInfo(name="fk_ref", columns=["name"],
                               ref_table="other", ref_columns=["name"]),
            ],
        )
        target = TableSchema(
            name="t1",
            columns=[
                ColumnInfo(name="id", data_type="int(11)", nullable=False,
                           default=None, extra="auto_increment", key="PRI"),
                ColumnInfo(name="name", data_type="varchar(100)", nullable=True,
                           default=None, charset="utf8mb4",
                           collation="utf8mb4_general_ci"),
            ],
            indexes=[
                IndexInfo(name="PRIMARY", columns=["id"], unique=True),
                IndexInfo(name="idx_name", columns=["name"]),
            ],
            foreign_keys=[
                ForeignKeyInfo(name="fk_ref", columns=["name"],
                               ref_table="other", ref_columns=["name"]),
            ],
        )
        return {"t1": source}, {"t1": target}

    def test_quick_level_skips_indexes(self):
        """Quick 모드에서 인덱스/FK 비교 안 함"""
        source_tables, target_tables = self._make_tables()

        comparator = SchemaComparator()
        diffs = comparator.compare_schemas(
            source_tables, target_tables, CompareLevel.QUICK
        )

        assert len(diffs) == 1
        table_diff = diffs[0]

        # 인덱스/FK 비교가 스킵되어 빈 리스트
        assert table_diff.index_diffs == []
        assert table_diff.fk_diffs == []

        # 컬럼은 타입만 비교 (int vs int(11) → 차이 발견)
        modified_cols = [c for c in table_diff.column_diffs
                         if c.diff_type == DiffType.MODIFIED]
        assert len(modified_cols) == 1
        assert modified_cols[0].column_name == "id"

    def test_standard_level_includes_indexes(self):
        """Standard 모드에서 인덱스/FK 비교 포함"""
        source_tables, target_tables = self._make_tables()

        comparator = SchemaComparator()
        diffs = comparator.compare_schemas(
            source_tables, target_tables, CompareLevel.STANDARD
        )

        table_diff = diffs[0]
        # 인덱스/FK 비교가 포함됨 (Standard에서는 인덱스 비교 수행)
        assert len(table_diff.index_diffs) > 0
        # Standard에서는 charset/collation 비교 안 함
        for col_diff in table_diff.column_diffs:
            for d in col_diff.differences:
                assert not d.startswith("Charset:")
                assert not d.startswith("Collation:")

    def test_strict_level_includes_charset(self):
        """Strict 모드에서 charset/collation 비교 포함"""
        source_tables, target_tables = self._make_tables()

        comparator = SchemaComparator()
        diffs = comparator.compare_schemas(
            source_tables, target_tables, CompareLevel.STRICT
        )

        table_diff = diffs[0]
        # name 컬럼에서 collation 차이 발견 (utf8mb4_unicode_ci vs utf8mb4_general_ci)
        name_diff = next(
            (c for c in table_diff.column_diffs if c.column_name == "name"),
            None
        )
        assert name_diff is not None
        assert name_diff.diff_type == DiffType.MODIFIED
        collation_diffs = [d for d in name_diff.differences
                           if d.startswith("Collation:")]
        assert len(collation_diffs) == 1


# ============================================================
# display width 감지 엣지케이스
# ============================================================

class TestDisplayWidthDetection:

    def test_int_unsigned_display_width(self, classifier):
        """int unsigned vs int(10) unsigned → display width 차이 아님 (unsigned 처리)"""
        # int unsigned와 int(10) unsigned는 display width 제거 후 다름
        # int unsigned → "int unsigned", int(10) unsigned → "int unsigned"
        assert classifier._is_display_width_only_diff(
            "int unsigned", "int(10) unsigned"
        ) is True

    def test_varchar_not_display_width(self, classifier):
        """varchar(100) vs varchar(200) → display width가 아님"""
        assert classifier._is_display_width_only_diff(
            "varchar(100)", "varchar(200)"
        ) is False

    def test_decimal_not_display_width(self, classifier):
        """decimal(10,2) vs decimal(8,2) → display width가 아님"""
        assert classifier._is_display_width_only_diff(
            "decimal(10,2)", "decimal(8,2)"
        ) is False
