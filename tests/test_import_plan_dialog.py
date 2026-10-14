"""The one confirmation before an Import run (ImportPlanDialog), fed by dump.import_preview."""
import pytest
from PyQt6.QtWidgets import QApplication, QFrame, QLabel

from src.core.production_guard import Environment
from src.ui.dialogs.import_plan_dialog import ImportPlanDialog, describe_plan

_app = QApplication.instance() or QApplication([])

PREVIEW = {
    "dry_run": True, "target": {"engine": "mysql", "database": "app", "schema": "app", "exists": True},
    "replace": ["users", "orders"], "create": ["audit"], "untouched": ["notes"],
    "kept_children": [{"table": "notes", "constraint": "fk_notes_users", "parent": "users"}],
    "external_parents": [{"child": "orders", "constraint": "fk_orders_shops", "parent": "shops", "exists_in_target": True}],
    "views": {"replace": ["v_users"], "create": [], "may_not_replace": [{"name": "v_users", "definer": "admin@%"}],
              "may_break": [{"view": "v_notes", "table": "users"}]},
    "triggers_dropped": [{"table": "users", "name": "trg_users"}],
    "blockers": [], "copy_blockers": [],
    "warnings": [{"code": "unsynchronized_snapshot", "detail": "source dump: independent snapshots"},
                 {"code": "unsynchronized_snapshot", "detail": "legacy policy"},
                 {"code": "views_outside_snapshot", "detail": "views were captured later"},
                 {"code": "mystery_code", "detail": "a future warning"}],
    "strict_export": False, "snapshot_policy": "mysql_parallel_unsynchronized_snapshots", "source_timezone": "UTC",
}


def _dialog(preview=PREVIEW, environment=Environment.DEVELOPMENT, verify=False, toggles="", timezone_label=""):
    plan = describe_plan(preview, verify, timezone_label)
    return ImportPlanDialog(None, preview, plan, environment, "127.0.0.1:3306 / app", "app", toggles)


def _texts(dialog):
    return "\n".join(label.text() for label in dialog.findChildren(QLabel)) + "\n" + "\n".join(
        button.text() for button in dialog.findChildren(type(dialog.btn_execute)))


def test_import_plan_dialog_lists_replace_create_untouched_and_warnings():
    dialog = _dialog(toggles="켜진 고급 설정: 동시 작업 4개")
    try:
        texts = _texts(dialog)
        assert "넣을 곳: 127.0.0.1:3306 / app" in texts and "(새로 만듭니다)" not in texts
        assert "켜진 고급 설정: 동시 작업 4개" in texts
        for key, title, names in (("replace", "덮어쓸 테이블 2개 ▸", "users\norders"),
                                  ("create", "새로 만들 테이블 1개 ▸", "audit"),
                                  ("untouched", "건드리지 않을 테이블 1개 ▸", "notes")):
            button, label = dialog.lists[key]
            assert button.text() == title and label.text() == names and label.isHidden()
            button.click()
            assert not label.isHidden()
        warnings = dialog.label_warnings.text()
        assert "- 교체하면 사라지는 트리거: users.trg_users" in warnings
        assert "- 이 계정으로 지우지 못할 수 있는 뷰(다른 계정이 만듦): v_users (admin@%)" in warnings
        assert "- 교체 뒤 동작하지 않을 수 있는 뷰: v_notes → users" in warnings
        assert "- 부모 행과 맞는지 확인하지 않습니다: orders.fk_orders_shops → shops" in warnings
        # One line per code, in plain Korean; an unknown code gets the general sentence.
        assert warnings.count("같은 시점에 내보내지지 않아") == 1
        assert "뷰 정의는 테이블 데이터와 같은 시점에 담기지 않았습니다." in warnings
        assert "덤프를 만들 때 남긴 경고가 있습니다." in warnings
        assert "independent snapshots" not in warnings and "mystery_code" not in warnings
        # Behind '자세히': kept children, view counts and the core's own text.
        button, details = dialog.details
        assert details.isHidden()
        assert "notes.fk_notes_users → users" in details.text() and "교체할 뷰 1개, 새로 만들 뷰 0개" in details.text()
        assert "mystery_code: a future warning" in details.text()
        assert not hasattr(dialog, "label_blockers") and dialog.btn_execute.isEnabled()
        assert "부분 Import" not in texts
    finally:
        dialog.close()


def test_point_in_time_warning_only_for_unsynchronized_policies():
    preview = {**PREVIEW, "snapshot_policy": "mysql_shared_consistent_snapshot"}
    assert not any("같은 시점에 내보내지지" in line for line in describe_plan(preview, False)["warnings"])
    legacy = {**PREVIEW, "snapshot_policy": "mysql_parallel_no_backup_lock_consistent_snapshot"}
    assert any("같은 시점에 내보내지지" in line for line in describe_plan(legacy, False)["warnings"])


def test_legacy_timezone_warning_names_the_choice_or_where_to_make_it():
    preview = {"warnings": [{"code": "legacy_timezone", "detail": "x"}]}
    unchosen = describe_plan(preview, False)
    assert unchosen["must_see"] is True and "'타임존 직접 정하기'에서 고를 수 있습니다." in unchosen["warnings"][0]
    chosen = describe_plan(preview, False, "한국 시간(+09:00)")
    assert chosen["must_see"] is False and chosen["warnings"][0].endswith("고른 시간대: 한국 시간(+09:00)")


def test_import_plan_dialog_production_requires_typed_name():
    dialog = _dialog(environment=Environment.PRODUCTION)
    try:
        assert not dialog.btn_execute.isEnabled()
        dialog.input_schema.setText("ap")
        assert not dialog.btn_execute.isEnabled()
        dialog.input_schema.setText(" app ")
        assert dialog.btn_execute.isEnabled()
    finally:
        dialog.close()


@pytest.mark.parametrize("environment, header", [
    (Environment.PRODUCTION, "운영 환경"), (Environment.STAGING, "스테이징 환경"), (Environment.UNKNOWN, "미분류 환경"),
])
def test_import_plan_dialog_uses_korean_env_and_correct_warning_line(environment, header):
    for preview, verify, line in (
        (PREVIEW, False, "⚠️ 덮어쓴 테이블은 되돌릴 수 없습니다"),
        (PREVIEW, True, "⚠️ 교체 전 테이블은 백업 DB에 남습니다"),
        ({**PREVIEW, "replace": [], "target": {"exists": False}}, False, "⚠️ 새로 만들기만 합니다"),
        ({**PREVIEW, "replace": [], "target": {"exists": False}}, True, "⚠️ 새로 만들기만 합니다"),
    ):
        dialog = _dialog(preview, environment, verify)
        try:
            assert dialog.label_warning.text() == line
            assert "되돌릴 수 없습니다." not in _texts(dialog)  # the inherited line is replaced
            assert ("(새로 만듭니다)" in _texts(dialog)) is (preview["target"].get("exists") is False)
            frame_labels = [label.text() for frame in dialog.findChildren(QFrame) for label in frame.findChildren(QLabel)]
            assert header in frame_labels and "PRODUCTION" not in _texts(dialog) and "STAGING" not in _texts(dialog)
            # Staging and unclassified start on Cancel; production waits for the typed name.
            assert dialog.btn_cancel.isDefault() is (environment != Environment.PRODUCTION)
        finally:
            dialog.close()


def test_development_plan_has_no_colored_warning():
    dialog = _dialog(environment=Environment.DEVELOPMENT)
    try:
        assert "개발 환경" not in _texts(dialog)  # no header frame
        assert "#333" in dialog.label_warning.styleSheet()
        assert dialog.btn_execute.isEnabled() and not hasattr(dialog, "input_schema")
    finally:
        dialog.close()


def test_import_plan_blocker_disables_start():
    blocked = {**PREVIEW, "blockers": [{"code": "dependent_view", "table": "users", "detail": "v depends on users"},
                                       {"code": "dependent_view", "table": "orders", "detail": "w depends on orders"},
                                       {"code": "brand_new_code", "table": None, "detail": "x"}]}
    for environment in (Environment.DEVELOPMENT, Environment.PRODUCTION):
        dialog = _dialog(blocked, environment)
        try:
            assert not dialog.btn_execute.isEnabled()
            if environment == Environment.PRODUCTION:
                dialog.input_schema.setText("app")
                assert not dialog.btn_execute.isEnabled(), "a typed name never overrides a blocker"
            text = dialog.label_blockers.text()
            assert "- 대상의 뷰가 이 테이블을 쓰고 있어 교체할 수 없습니다. (users, orders)" in text
            assert "진행할 수 없는 다른 이유가 있습니다. '자세히'에서 확인하세요." in text
            assert "depends on" not in text
        finally:
            dialog.close()


def test_copy_blockers_stop_only_verify_in_copy_and_say_how_to_go_on():
    preview = {**PREVIEW, "copy_blockers": [{"code": "target_triggers", "table": "users", "detail": "trg"}]}
    assert describe_plan(preview, False)["blockers"] == []
    blockers = describe_plan(preview, True)["blockers"]
    assert blockers == ["- 대상 테이블에 트리거가 있어 교체할 수 없습니다. (users)",
                        "'먼저 복사본에서 검증하고 교체'를 끄면 진행할 수 있습니다."]
    dialog = _dialog(preview, verify=True)
    try:
        assert not dialog.btn_execute.isEnabled()
    finally:
        dialog.close()


def test_must_see_is_only_set_by_blockers_and_data_warnings():
    quiet = {"replace": [], "create": ["a"], "warnings": [{"code": "not_strict", "detail": "x"},
                                                          {"code": "views_outside_snapshot", "detail": "y"}]}
    assert describe_plan(quiet, False)["must_see"] is False
    assert describe_plan({**quiet, "external_parents": [{"child": "a", "constraint": "fk", "parent": "p"}]}, False)["must_see"]
    assert describe_plan({**quiet, "blockers": [{"code": "no_tables"}]}, False)["must_see"]
