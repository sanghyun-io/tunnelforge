"""Fix-round regression coverage for exact PostgreSQL owners and plan warnings."""
import json
from pathlib import Path

import pytest

from src.core import i18n
from src.exporters.rust_dump_exporter import (
    CORE_CODE_SENTENCES, RustDumpConfig, RustDumpExporter, describe_core_code, describe_export_refusal,
)
from src.ui.dialogs.import_plan_dialog import describe_plan


@pytest.mark.parametrize("language",["ko","en"])
@pytest.mark.parametrize("object_id,code,name",[
    ("trigger:orders:archive:trg:x","postgres_trigger_unsupported","trg:x"),
    ("check_constraint:orders:archive:chk:y","check_constraint_unsupported","chk:y"),
    ("unsupported_default:orders:archive:col:z","sequence_options_unsupported","col:z"),
    ("rule:orders:archive:rule:x","rule_unsupported","orders:archive"),
])
def test_refusal_uses_core_owner_instead_of_splitting_names(language,object_id,code,name):
    i18n.set_language(language)
    try:
        line=describe_export_refusal([object_id],{object_id:code},{object_id:"orders:archive"})[0]
        assert "orders:archive" in line
        assert describe_core_code(code,name) in line
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("language",["ko","en"])
def test_real_comments_preview_keeps_objects_and_not_strict_note(language):
    preview=json.loads((Path(__file__).parent/"fixtures/postgres_fidelity_fix_preview.json").read_text(encoding="utf-8"))
    i18n.set_language(language)
    try:
        plan=describe_plan(preview,True)
        text="\n".join(plan["warnings"])
        assert "comment:orders:TABLE" in text and "comment:orders:id" in text
        assert describe_core_code("not_strict") in text
        assert "source dump:" not in text
        assert not any(line.rstrip().endswith(":") for line in plan["warnings"])
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("language",["ko","en"])
def test_comments_plan_caps_long_names(language):
    preview={"warnings":[{"code":"comments_not_exported","detail":"source dump: comments_not_exported: "+", ".join(f"comment:t:c{i}" for i in range(25))}]}
    i18n.set_language(language)
    try:
        text="\n".join(describe_plan(preview,True)["warnings"])
        assert "comment:t:c19" in text and "comment:t:c20" not in text
        assert "5" in text
    finally:
        i18n.set_language(i18n.DEFAULT_LANGUAGE)


def test_export_worker_payload_keeps_owning_table_map(tmp_path):
    event={"event":"error","error_code":"unsupported_objects","objects":["trigger:t:x:r"],"reasons":{"trigger:t:x:r":"postgres_trigger_unsupported"},"object_tables":{"trigger:t:x:r":"t:x"},"bypassable":True}
    class Facade:
        def run_dump(self,payload,on_event=None):
            on_event(event)
            raise RuntimeError("unsupported_objects")
    exporter=RustDumpExporter(RustDumpConfig("localhost",5432,"test","dummy",engine="postgresql"),facade=Facade())
    ok,_=exporter.export_full_schema("test",str(tmp_path))
    assert not ok
    assert exporter.last_refusal["object_tables"]==event["object_tables"]


def test_changed_postgres_wording_mentions_all_conditions():
    assert CORE_CODE_SENTENCES["row_security_blocks_read"]=="테이블 {}에 행 수준 보안(RLS)이 걸려 있어 이 계정으로는 모든 행을 읽을 수 없습니다. 슈퍼유저나 BYPASSRLS 권한이 있는 계정으로 다시 시도하세요."
    assert "최솟값·최댓값·순환·형식" in CORE_CODE_SENTENCES["sequence_options_unsupported"]
    assert "SELECT 같은 문장 키워드" in CORE_CODE_SENTENCES["generated_expression_unsafe"]
    assert CORE_CODE_SENTENCES["generated_expression_unsafe"].endswith("담을 수 없습니다.")
