"""TF-STATUS-209: PostgreSQL key-rule refusals and the cross-engine deferral warning in plain words."""
import pytest

from src.core import i18n
from src.exporters.rust_dump_exporter import (
    LOSSY_REFUSAL_KINDS, describe_core_code, describe_export_refusal, describe_program_warnings,
)
from src.ui.dialogs.import_plan_dialog import describe_plan


@pytest.fixture(params=["ko", "en"])
def language(request):
    i18n.set_language(request.param)
    yield request.param
    i18n.set_language(i18n.DEFAULT_LANGUAGE)


@pytest.mark.parametrize("object_id,code,name", [
    ("key_rule:orders:orders_pk", "deferrable_primary_key_unsupported", "orders_pk"),
    ("key_rule:orders:no_overlap", "exclusion_constraint_unsupported", "no_overlap"),
])
def test_key_rule_refusals_name_the_table_and_the_rule(language, object_id, code, name):
    line = describe_export_refusal([object_id], {object_id: code}, {object_id: "orders"})[0]
    assert "orders" in line and describe_core_code(code, name) in line
    if language == "en":
        assert not any("가" <= ch <= "힣" for ch in line), line


def test_unexplained_key_rule_is_a_lossy_refusal_with_its_own_sentence(language):
    assert "key_rule" in LOSSY_REFUSAL_KINDS
    line = describe_export_refusal(["key_rule:orders:dk"], {}, {"key_rule:orders:dk": "orders"})[0]
    assert "dk" in line
    assert ("NULL" in line) and "담을 수 없는 객체" not in line


def test_deferral_warning_lists_the_keys_in_the_plan_and_the_result(language):
    preview = {"target": {"engine": "mysql"}, "warnings": [
        {"code": "constraint_deferral_not_kept", "detail": "c.c_parent, d.d_fk", "names": ["c.c_parent", "d.d_fk"]}]}
    plan = describe_plan(preview, False)
    lines = [line for line in plan["warnings"] + plan.get("notes", []) if "c.c_parent" in line]
    assert lines and "d.d_fk" in lines[0], plan
    assert not lines[0].rstrip().endswith(":")
    result = describe_program_warnings([{"code": "constraint_deferral_not_kept", "name": "c.c_parent"}])
    assert result == [describe_core_code("constraint_deferral_not_kept", "c.c_parent")]
    assert "c.c_parent" in result[0]
