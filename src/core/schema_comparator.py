"""
두 스키마 구조 비교기
"""
from typing import Any, Callable, Dict, List, Optional

from src.core.schema_diff_models import (
    ColumnDiff, ColumnInfo, CompareLevel, DIFF_PREFIX_CHARSET,
    DIFF_PREFIX_COLLATION, DIFF_PREFIX_DEFAULT, DIFF_PREFIX_EXTRA,
    DIFF_PREFIX_NULLABLE, DIFF_PREFIX_TYPE, DiffType, ForeignKeyDiff,
    ForeignKeyInfo, IndexDiff, IndexInfo, TableDiff, TableSchema,
    _normalize_column_extra,
)


class SchemaComparator:
    """스키마 비교기"""

    def compare_tables(
        self,
        source: TableSchema,
        target: TableSchema,
        compare_level: CompareLevel = CompareLevel.STANDARD
    ) -> TableDiff:
        """두 테이블 스키마 비교

        Args:
            source: 소스 테이블 스키마
            target: 타겟 테이블 스키마
            compare_level: 비교 수준

        Returns:
            TableDiff
        """
        diff = TableDiff(
            table_name=source.name,
            diff_type=DiffType.UNCHANGED,
            source_schema=source,
            target_schema=target,
            row_count_source=source.row_count,
            row_count_target=target.row_count
        )

        # 컬럼 비교
        diff.column_diffs = self._compare_columns(
            source.columns, target.columns, compare_level
        )

        # 인덱스/FK 비교 (Quick 모드에서는 스킵)
        if compare_level in (CompareLevel.STANDARD, CompareLevel.STRICT):
            diff.index_diffs = self._compare_indexes(source.indexes, target.indexes)
            diff.fk_diffs = self._compare_foreign_keys(
                source.foreign_keys, target.foreign_keys
            )

        # 전체 상태 결정
        if diff.has_differences():
            diff.diff_type = DiffType.MODIFIED

        return diff

    def compare_schemas(
        self,
        source_tables: Dict[str, TableSchema],
        target_tables: Dict[str, TableSchema],
        compare_level: CompareLevel = CompareLevel.STANDARD
    ) -> List[TableDiff]:
        """두 스키마 전체 비교

        Args:
            source_tables: 소스 테이블 딕셔너리
            target_tables: 타겟 테이블 딕셔너리
            compare_level: 비교 수준

        Returns:
            TableDiff 목록
        """
        diffs = []

        all_tables = set(source_tables.keys()) | set(target_tables.keys())

        for table_name in sorted(all_tables):
            source = source_tables.get(table_name)
            target = target_tables.get(table_name)

            if source and not target:
                # 소스에만 있음 (타겟에 추가 필요)
                diff = TableDiff(
                    table_name=table_name,
                    diff_type=DiffType.ADDED,
                    source_schema=source,
                    row_count_source=source.row_count
                )
            elif target and not source:
                # 타겟에만 있음 (삭제 필요)
                diff = TableDiff(
                    table_name=table_name,
                    diff_type=DiffType.REMOVED,
                    target_schema=target,
                    row_count_target=target.row_count
                )
            else:
                # 둘 다 있음 (상세 비교)
                diff = self.compare_tables(source, target, compare_level)

            diffs.append(diff)

        return diffs

    def _compare_columns(
        self,
        source_cols: List[ColumnInfo],
        target_cols: List[ColumnInfo],
        compare_level: CompareLevel = CompareLevel.STANDARD
    ) -> List[ColumnDiff]:
        """컬럼 비교"""
        diffs = []

        source_map = {c.name.lower(): c for c in source_cols}
        target_map = {c.name.lower(): c for c in target_cols}

        all_cols = set(source_map.keys()) | set(target_map.keys())

        for col_name in sorted(all_cols):
            src = source_map.get(col_name)
            tgt = target_map.get(col_name)

            if src and not tgt:
                diffs.append(ColumnDiff(
                    column_name=src.name,
                    diff_type=DiffType.ADDED,
                    source_info=src
                ))
            elif tgt and not src:
                diffs.append(ColumnDiff(
                    column_name=tgt.name,
                    diff_type=DiffType.REMOVED,
                    target_info=tgt
                ))
            else:
                # 상세 비교
                differences = []

                # 타입 비교 (모든 레벨)
                if src.data_type.lower() != tgt.data_type.lower():
                    differences.append(f"{DIFF_PREFIX_TYPE} {src.data_type} → {tgt.data_type}")

                # Quick 모드: 타입만 비교
                if compare_level != CompareLevel.QUICK:
                    if src.nullable != tgt.nullable:
                        src_null = "NULL" if src.nullable else "NOT NULL"
                        tgt_null = "NULL" if tgt.nullable else "NOT NULL"
                        differences.append(f"{DIFF_PREFIX_NULLABLE} {src_null} → {tgt_null}")

                    if src.default != tgt.default:
                        differences.append(f"{DIFF_PREFIX_DEFAULT} {src.default} → {tgt.default}")

                    src_extra = _normalize_column_extra(src.extra)
                    tgt_extra = _normalize_column_extra(tgt.extra)
                    if src_extra.lower() != tgt_extra.lower():
                        differences.append(f"{DIFF_PREFIX_EXTRA} {src_extra} → {tgt_extra}")

                # Strict 모드: charset + collation 추가 비교
                if compare_level == CompareLevel.STRICT:
                    if src.charset and tgt.charset and src.charset.lower() != tgt.charset.lower():
                        differences.append(f"{DIFF_PREFIX_CHARSET} {src.charset} → {tgt.charset}")

                    if src.collation and tgt.collation and src.collation.lower() != tgt.collation.lower():
                        differences.append(f"{DIFF_PREFIX_COLLATION} {src.collation} → {tgt.collation}")

                if differences:
                    diffs.append(ColumnDiff(
                        column_name=src.name,
                        diff_type=DiffType.MODIFIED,
                        source_info=src,
                        target_info=tgt,
                        differences=differences
                    ))
                else:
                    diffs.append(ColumnDiff(
                        column_name=src.name,
                        diff_type=DiffType.UNCHANGED,
                        source_info=src,
                        target_info=tgt
                    ))

        return diffs

    @staticmethod
    def _index_content_key(idx: IndexInfo) -> tuple:
        """인덱스의 내용 기반 키 (이름 제외)"""
        return (tuple(c.lower() for c in idx.columns), idx.unique, idx.type)

    @staticmethod
    def _fk_content_key(fk: ForeignKeyInfo) -> tuple:
        """FK의 내용 기반 키 (이름 제외)"""
        return (
            tuple(c.lower() for c in fk.columns),
            fk.ref_table.lower(),
            tuple(c.lower() for c in fk.ref_columns),
            fk.on_delete,
            fk.on_update,
        )

    def _compare_named_entities(
        self,
        source_map: Dict[str, Any],
        target_map: Dict[str, Any],
        content_key_fn: Callable[[Any], tuple],
        diff_builder: Callable[..., Any],
    ) -> List[Any]:
        """이름 매칭 → content-key 기반 RENAMED 감지 → 잔여 ADDED/REMOVED
        3단계 제네릭 비교 알고리즘 (인덱스/FK 비교가 공유).

        diff_builder(src=None, tgt=None, diff_type=None, old_name=None)는
        diff_type이 None이면 이름이 매칭된 src/tgt를 비교해 MODIFIED/UNCHANGED를
        직접 판단하고, 그 외에는 지정된 diff_type(RENAMED/ADDED/REMOVED)으로
        엔티티별 Diff 인스턴스를 만들어 반환해야 한다.
        """
        diffs = []

        # 1단계: 이름으로 매칭
        matched_source = set()
        matched_target = set()

        common_names = set(source_map.keys()) & set(target_map.keys())
        for name in sorted(common_names):
            src = source_map[name]
            tgt = target_map[name]
            matched_source.add(name)
            matched_target.add(name)
            diffs.append(diff_builder(src=src, tgt=tgt))

        # 2단계: 미매칭 항목에서 rename 감지
        unmatched_source = {k: v for k, v in source_map.items() if k not in matched_source}
        unmatched_target = {k: v for k, v in target_map.items() if k not in matched_target}

        # 타겟 미매칭을 내용 기반으로 인덱싱
        target_by_content: Dict[tuple, List[str]] = {}
        for tgt_name, tgt in unmatched_target.items():
            key = content_key_fn(tgt)
            target_by_content.setdefault(key, []).append(tgt_name)

        renamed_target = set()
        source_added = []

        for src_name in sorted(unmatched_source.keys()):
            src = unmatched_source[src_name]
            content_key = content_key_fn(src)
            candidates = target_by_content.get(content_key, [])
            # 아직 매칭 안 된 후보 찾기
            match_found = False
            for tgt_name in candidates:
                if tgt_name not in renamed_target:
                    tgt = unmatched_target[tgt_name]
                    renamed_target.add(tgt_name)
                    diffs.append(diff_builder(
                        src=src, tgt=tgt, diff_type=DiffType.RENAMED, old_name=tgt.name
                    ))
                    match_found = True
                    break

            if not match_found:
                source_added.append(src)

        # 3단계: 남은 미매칭 → ADDED / REMOVED
        for src in source_added:
            diffs.append(diff_builder(src=src, diff_type=DiffType.ADDED))

        for tgt_name in sorted(unmatched_target.keys()):
            if tgt_name not in renamed_target:
                tgt = unmatched_target[tgt_name]
                diffs.append(diff_builder(tgt=tgt, diff_type=DiffType.REMOVED))

        return diffs

    @staticmethod
    def _build_index_diff(
        src: Optional[IndexInfo] = None,
        tgt: Optional[IndexInfo] = None,
        diff_type: Optional[DiffType] = None,
        old_name: Optional[str] = None,
    ) -> IndexDiff:
        """이름 매칭된 인덱스의 MODIFIED/UNCHANGED 판정, 또는
        RENAMED/ADDED/REMOVED IndexDiff 생성."""
        if diff_type is None:
            differences = []
            if src.columns != tgt.columns:
                differences.append(f"컬럼: {src.columns} → {tgt.columns}")
            if src.unique != tgt.unique:
                differences.append(f"Unique: {src.unique} → {tgt.unique}")
            return IndexDiff(
                index_name=src.name,
                diff_type=DiffType.MODIFIED if differences else DiffType.UNCHANGED,
                source_info=src,
                target_info=tgt,
                differences=differences
            )
        if diff_type == DiffType.RENAMED:
            return IndexDiff(
                index_name=src.name,
                diff_type=DiffType.RENAMED,
                source_info=src,
                target_info=tgt,
                differences=[f"이름 변경: {tgt.name} → {src.name}"],
                old_name=old_name
            )
        if diff_type == DiffType.ADDED:
            return IndexDiff(index_name=src.name, diff_type=DiffType.ADDED, source_info=src)
        return IndexDiff(index_name=tgt.name, diff_type=DiffType.REMOVED, target_info=tgt)

    @staticmethod
    def _build_fk_diff(
        src: Optional[ForeignKeyInfo] = None,
        tgt: Optional[ForeignKeyInfo] = None,
        diff_type: Optional[DiffType] = None,
        old_name: Optional[str] = None,
    ) -> ForeignKeyDiff:
        """이름 매칭된 FK의 MODIFIED/UNCHANGED 판정, 또는
        RENAMED/ADDED/REMOVED ForeignKeyDiff 생성."""
        if diff_type is None:
            differences = []
            if src.ref_table != tgt.ref_table:
                differences.append(f"참조 테이블: {src.ref_table} → {tgt.ref_table}")
            if src.columns != tgt.columns:
                differences.append(f"컬럼: {src.columns} → {tgt.columns}")
            if src.on_delete != tgt.on_delete:
                differences.append(f"ON DELETE: {src.on_delete} → {tgt.on_delete}")
            if src.on_update != tgt.on_update:
                differences.append(f"ON UPDATE: {src.on_update} → {tgt.on_update}")
            return ForeignKeyDiff(
                fk_name=src.name,
                diff_type=DiffType.MODIFIED if differences else DiffType.UNCHANGED,
                source_info=src,
                target_info=tgt,
                differences=differences
            )
        if diff_type == DiffType.RENAMED:
            return ForeignKeyDiff(
                fk_name=src.name,
                diff_type=DiffType.RENAMED,
                source_info=src,
                target_info=tgt,
                differences=[f"이름 변경: {tgt.name} → {src.name}"],
                old_name=old_name
            )
        if diff_type == DiffType.ADDED:
            return ForeignKeyDiff(fk_name=src.name, diff_type=DiffType.ADDED, source_info=src)
        return ForeignKeyDiff(fk_name=tgt.name, diff_type=DiffType.REMOVED, target_info=tgt)

    def _compare_indexes(
        self,
        source_idx: List[IndexInfo],
        target_idx: List[IndexInfo]
    ) -> List[IndexDiff]:
        """인덱스 비교 (rename 감지 포함)"""
        source_map = {i.name.lower(): i for i in source_idx}
        target_map = {i.name.lower(): i for i in target_idx}
        return self._compare_named_entities(
            source_map, target_map, self._index_content_key, self._build_index_diff
        )

    def _compare_foreign_keys(
        self,
        source_fks: List[ForeignKeyInfo],
        target_fks: List[ForeignKeyInfo]
    ) -> List[ForeignKeyDiff]:
        """외래 키 비교 (rename 감지 포함)"""
        source_map = {f.name.lower(): f for f in source_fks}
        target_map = {f.name.lower(): f for f in target_fks}
        return self._compare_named_entities(
            source_map, target_map, self._fk_content_key, self._build_fk_diff
        )
