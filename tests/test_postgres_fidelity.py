"""PostgreSQL refusal wording, translations, and visible export/import reports."""
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.core import i18n
from src.core.error_report_codes import RUST_CORE_CODES
from src.exporters.rust_dump_exporter import (
    CORE_CODE_SENTENCES, RustDumpConfig, RustDumpExporter, describe_core_code,
    describe_export_refusal, describe_manifest_warning, describe_program_warnings,
)


def test_every_postgres_refusal_reason_has_a_sentence_and_translation():
    source=(Path(__file__).resolve().parents[1]/"migration_core/src/schema.rs").read_text(encoding="utf-8")
    codes=[]
    for name in ("POSTGRES_REFUSAL_REASONS", "POSTGRES_WARNING_REASONS"):
        body=re.search(rf"const {name}:.*?=\s*&\[(.*?)\];",source,re.S).group(1)
        codes.extend(re.findall(r'"([a-z_]+)"',body))
    assert len(codes)>=16
    assert set(codes)<=RUST_CORE_CODES
    assert set(codes)<=CORE_CODE_SENTENCES.keys()
    i18n.set_language("en")
    try:
        for code in codes:
            sentence=describe_core_code(code,"sample")
            assert sentence and not re.search("[가-힣]",sentence),(code,sentence)
            if "담을 수 없는 객체는 빼고 내보내기" in CORE_CODE_SENTENCES[code]:
                assert i18n.translate_text("담을 수 없는 객체는 빼고 내보내기") in sentence
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("language",["ko","en"])
@pytest.mark.parametrize("object_id,code,name",[
    ("unsupported_default:orders:id","identity_always_unsupported","id"),
    ("unsupported_default:orders:id","sequence_options_unsupported","id"),
    ("unsupported_default:orders:token","default_expression_unsupported","token"),
    ("check_constraint:orders:positive","check_constraint_unsupported","positive"),
    ("custom_type:orders:label","custom_type_unsupported","label"),
    ("unsupported_index:orders:filtered","index_shape_unsupported","filtered"),
    ("cross_schema_fk:orders:parent","cross_schema_fk_unsupported","parent"),
    ("materialized_view:recent","materialized_view_unsupported","recent"),
    ("policy:orders","row_security_unsupported","orders"),
    ("policy:orders:archive","row_security_unsupported","orders:archive"),
    ("rule:orders:keep_rows","rule_unsupported","orders"),
    ("rule:orders:archive:keep_rows","rule_unsupported","orders:archive"),
    ("table_inheritance:orders","table_inheritance_unsupported","orders"),
    ("table_inheritance:orders:archive","table_inheritance_unsupported","orders:archive"),
    ("generated_column:orders:g","generated_dependency_unsupported","g"),
])
def test_export_refusal_uses_the_recorded_postgres_reason(language,object_id,code,name):
    i18n.set_language(language)
    try:
        lines=describe_export_refusal([object_id],{object_id:code})
        assert len(lines)==1
        assert describe_core_code(code,name) in lines[0]
        assert code not in lines[0]
        assert name in lines[0]
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("language",["ko","en"])
def test_comment_warning_reaches_export_completion_and_progress(language,tmp_path):
    facade=MagicMock()
    warning="comments_not_exported: comment:sample:TABLE, comment:sample:id"
    facade.run_dump.return_value={"success":True,"tables":1,"rows_dumped":1,"manifest_warnings":[warning]}
    progress=[]
    i18n.set_language(language)
    try:
        expected=describe_manifest_warning(warning)
        assert "sample:TABLE" in expected and "sample:id" in expected
        assert "comments_not_exported" not in expected
        exporter=RustDumpExporter(RustDumpConfig("localhost",5432,"test","dummy",engine="postgresql"),facade=facade)
        ok,message=exporter.export_full_schema("app",str(tmp_path),progress_callback=progress.append)
        assert ok and expected in message and any(expected in line for line in progress)
        assert not re.search("[가-힣]",expected) if language=="en" else "주석" in expected
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("language",["ko","en"])
def test_generated_recalculation_is_a_named_import_report_line(language):
    i18n.set_language(language)
    try:
        lines=describe_program_warnings([{"code":"generated_values_recomputed","name":"orders"}])
        assert lines==[describe_core_code("generated_values_recomputed","orders")]
        assert "orders" in lines[0]
        assert "generated_values_recomputed" not in lines[0]
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_generated_cross_engine_sentence_names_same_database_kind():
    assert CORE_CODE_SENTENCES["unsupported_generated_column"]=="계산 열은 덤프를 만든 DB와 같은 종류의 대상으로만 복원할 수 있습니다. 같은 종류의 DB로 가져오세요."
    i18n.set_language("en")
    try:
        assert describe_core_code("unsupported_generated_column")=="Generated columns can only be restored to the same kind of database that created the dump. Import into the same kind of database."
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)
