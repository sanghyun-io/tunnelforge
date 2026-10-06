import inspect
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication, QDialog, QWidget

from src.ui.dialogs.tunnel_config import (
    TunnelConfigDialog,
    _RunningTestProgressDialog,
    _TempCredentials,
)
from src.ui.workers.test_worker import ConnectionTestWorker, TestType


app = QApplication.instance() or QApplication(sys.argv)


class ParentWithTunnels(QWidget):
    def __init__(self):
        super().__init__()
        self.tunnels = [
            {
                "id": "current",
                "name": "Current",
                "connection_mode": "ssh_tunnel",
                "bastion_host": "old-bastion",
                "bastion_port": 22,
                "bastion_user": "old-user",
                "bastion_key": "old.pem",
            },
            {
                "id": "template",
                "name": "Template",
                "connection_mode": "ssh_tunnel",
                "bastion_host": "template-bastion",
                "bastion_port": 2022,
                "bastion_user": "ec2-user",
                "bastion_key": "C:/keys/template.pem",
            },
            {
                "id": "direct",
                "name": "Direct",
                "connection_mode": "direct",
                "bastion_host": "ignore-me",
            },
        ]


def test_copy_bastion_from_another_connection_only_copies_bastion_fields():
    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(
        parent,
        tunnel_data={
            "id": "current",
            "name": "Current",
            "connection_mode": "ssh_tunnel",
            "remote_host": "db.example.com",
            "remote_port": 3306,
            "default_database": "postgres",
            "default_schema": "app",
        },
    )
    try:
        assert len(dialog.bastion_templates) == 1
        assert dialog.bastion_templates[0]["name"] == "Template"
        assert dialog.btn_copy_bastion.isEnabled()

        dialog._copy_bastion_from_tunnel(dialog.bastion_templates[0])

        assert dialog.input_bastion_host.text() == "template-bastion"
        assert dialog.input_bastion_port.value() == 2022
        assert dialog.input_bastion_user.text() == "ec2-user"
        assert dialog.input_bastion_key.text() == "C:/keys/template.pem"
        assert dialog.input_remote_host.text() == "db.example.com"
        assert dialog.input_remote_port.value() == 3306
        assert dialog.input_default_database.text() == "postgres"
        assert dialog.input_default_schema.text() == "app"
    finally:
        dialog.close()
        parent.close()


def test_db_engine_is_manual_select_field():
    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(
        parent,
        tunnel_data={
            "id": "current",
            "name": "Current",
            "connection_mode": "ssh_tunnel",
            "remote_host": "db.example.com",
            "remote_port": 5432,
            "db_engine": "postgresql",
        },
    )
    try:
        assert dialog.combo_db_engine.isEnabled()
        assert not hasattr(dialog, "btn_detect_engine")
        assert dialog.combo_db_engine.currentData() == "postgresql"

        mysql_index = dialog.combo_db_engine.findData("mysql")
        dialog.combo_db_engine.setCurrentIndex(mysql_index)
        assert dialog.get_data()["db_engine"] == "mysql"
    finally:
        dialog.close()
        parent.close()


def test_environment_combo_uses_item_data_for_persisted_value():
    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(
        parent,
        tunnel_data={
            "id": "current",
            "name": "Current",
            "connection_mode": "ssh_tunnel",
            "db_engine": "mysql",
            "environment": "staging",
        },
    )
    try:
        assert dialog.combo_environment.currentData() == "staging"

        production_index = dialog.combo_environment.findData("production")
        dialog.combo_environment.setCurrentIndex(production_index)
        assert dialog.get_data()["environment"] == "production"

        unset_index = dialog.combo_environment.findData(None)
        dialog.combo_environment.setCurrentIndex(unset_index)
        assert dialog.get_data()["environment"] is None
    finally:
        dialog.close()
        parent.close()


def test_unset_environment_copy_warns_that_dangerous_operations_need_confirmation():
    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(parent, tunnel_data={"id": "current"})
    try:
        assert dialog.combo_environment.itemText(0) == "(미설정)"
        assert "(미설정)" in dialog.combo_environment.toolTip()
        assert "위험 작업 시 확인 필요" in dialog.combo_environment.toolTip()
    finally:
        dialog.close()
        parent.close()


def test_available_tunnels_logs_and_returns_empty_list_on_config_failure(monkeypatch):
    class BrokenConfigManager:
        def load_config(self):
            raise RuntimeError("boom")

    parent = QWidget()
    parent.config_mgr = BrokenConfigManager()
    exception_calls = []

    monkeypatch.setattr(
        "src.ui.dialogs.tunnel_config.logger.exception",
        lambda message: exception_calls.append(message),
    )

    dialog = TunnelConfigDialog(parent, tunnel_data={"id": "current", "db_engine": "mysql"})
    try:
        exception_calls.clear()
        assert dialog._available_tunnels() == []
        assert exception_calls == ["failed to load tunnel list for bastion templates"]
    finally:
        dialog.close()
        parent.close()


def test_running_test_progress_dialog_blocks_reject_until_allowed():
    """WP-3.9 Finding 1 회귀: 테스트가 실행 중일 때는 ESC 등으로 트리거되는
    reject()가 완전히 무시되어야 한다. accept()는 별도 경로(닫기 버튼)이므로
    항상 정상 동작해야 한다 - 이를 기준선으로 삼아 reject() 차단 여부를
    간접 검증한다(QDialog의 기본 result()가 이미 Rejected이므로 accept() 후
    비교해야 신뢰할 수 있다).
    """
    parent = QWidget()
    dialog = _RunningTestProgressDialog(parent, "테스트")
    try:
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted

        # 실행 중 reject()는 완전히 무시되어야 한다
        dialog.reject()
        assert dialog.result() == QDialog.DialogCode.Accepted

        # 내장 QThread.finished() 이후에만 reject()가 허용된다
        dialog.allow_dismiss()
        dialog.reject()
        assert dialog.result() == QDialog.DialogCode.Rejected
    finally:
        dialog.close()
        parent.close()


def test_start_connection_test_retains_worker_until_thread_finished(monkeypatch):
    """WP-3.9 Finding 1 회귀: worker는 self._test_worker에 보관되어야 하고,
    결과 시그널(test_finished)만으로는 해제되면 안 되며, 내장 QThread.finished()
    발화 이후에만 참조를 해제하고 dialog dismiss를 허용해야 한다.

    실제 QThread를 실행하면 HANG/크래시 위험이 있으므로 start()를 no-op으로
    교체하고 시그널만 직접 emit한다.
    """
    monkeypatch.setattr(ConnectionTestWorker, "start", lambda self: None)

    parent = ParentWithTunnels()
    dialog = TunnelConfigDialog(parent, tunnel_data={"id": "current"}, tunnel_engine=object())
    progress_dialog = None
    try:
        progress_dialog = dialog._start_connection_test(
            TestType.TUNNEL_ONLY, {"name": "t"}, None, "터널 테스트"
        )

        worker = dialog._test_worker
        assert worker is not None
        assert progress_dialog._dismissable is False

        # 결과 시그널만으로는 아직 참조를 해제하면 안 된다
        worker.test_finished.emit(True, "ok")
        assert dialog._test_worker is worker
        assert progress_dialog._dismissable is False

        # 내장 QThread.finished()가 발화한 뒤에만 참조 해제 + dismiss 허용
        worker.finished.emit()
        assert dialog._test_worker is None
        assert progress_dialog._dismissable is True
    finally:
        if progress_dialog is not None:
            progress_dialog.close()
        dialog.close()
        parent.close()


def test_temp_credentials_prefers_plain_password_then_encrypted_fallback():
    """WP-3.9 Finding 2 회귀: _test_db_only/_test_integrated에 각각 인라인으로
    중복 정의되어 있던 임시 자격증명 클래스를 하나로 통합한 _TempCredentials가
    기존 우선순위(평문 > 암호화+encryptor > None)를 그대로 보존해야 한다.
    """

    class FakeEncryptor:
        def decrypt(self, value):
            return f"decrypted:{value}"

    plain = _TempCredentials("alice", "plainpw", None, None)
    assert plain.get_tunnel_credentials("any-id") == ("alice", "plainpw")

    encrypted_only = _TempCredentials("bob", "", "enc-blob", FakeEncryptor())
    assert encrypted_only.get_tunnel_credentials("any-id") == ("bob", "decrypted:enc-blob")

    neither = _TempCredentials("carol", "", None, None)
    assert neither.get_tunnel_credentials("any-id") == ("carol", None)


def test_test_db_only_and_test_integrated_share_temp_credentials_class():
    """WP-3.9 Finding 2 회귀: 두 테스트 플로우가 더 이상 각자 인라인 클래스를
    재정의하지 않고 동일한 모듈 레벨 _TempCredentials를 공유해야 한다.
    """
    db_only_src = inspect.getsource(TunnelConfigDialog._test_db_only)
    integrated_src = inspect.getsource(TunnelConfigDialog._test_integrated)

    for src in (db_only_src, integrated_src):
        assert "class " not in src, "임시 자격증명 클래스가 인라인으로 재정의되면 안 된다"
        assert "_TempCredentials(" in src


def test_connection_test_flows_share_run_test_wrapper():
    for method in (
        TunnelConfigDialog._test_tunnel_only,
        TunnelConfigDialog._test_db_only,
        TunnelConfigDialog._test_integrated,
    ):
        source = inspect.getsource(method)
        assert "_run_test(" in source
        assert "dialog.exec()" not in source


def test_dialog_scrolls_content_and_keeps_buttons_visible():
    from PyQt6.QtWidgets import QDialogButtonBox, QScrollArea

    dialog = TunnelConfigDialog(None, tunnel_data={"id": "t1", "name": "x", "db_engine": "mysql"})
    try:
        assert isinstance(dialog.scroll_area, QScrollArea)
        buttons = dialog.findChild(QDialogButtonBox)
        # 확인/취소와 통합 테스트는 스크롤 영역 밖(항상 보임)에 있다.
        assert not dialog.scroll_area.isAncestorOf(buttons)
        assert not dialog.scroll_area.isAncestorOf(dialog.btn_integrated_test)
        assert dialog.scroll_area.isAncestorOf(dialog.input_db_password)
        available = (dialog.screen() or app.primaryScreen()).availableGeometry()
        assert dialog.height() <= int(available.height() * 0.85)
    finally:
        dialog.deleteLater()


def test_dialog_group_choice_defaults_to_current_group():
    groups = [{"id": "g1", "name": "운영"}, {"id": "g2", "name": "개발"}]
    dialog = TunnelConfigDialog(None, tunnel_data={"id": "t1", "name": "x"}, groups=groups, current_group_id="g2")
    try:
        assert dialog.selected_group_id() == "g2"
        dialog.combo_group.setCurrentIndex(dialog.combo_group.findData(None))
        assert dialog.selected_group_id() is None
    finally:
        dialog.deleteLater()
    # 그룹 목록을 주지 않으면 선택란이 없고 원래 그룹을 유지한다.
    plain = TunnelConfigDialog(None, tunnel_data={"id": "t1"}, current_group_id="g1")
    try:
        assert plain.combo_group is None and plain.selected_group_id() == "g1"
    finally:
        plain.deleteLater()


def _ssh_dialog(parent=None, **data):
    tunnel = {
        "id": "new", "name": "N", "connection_mode": "ssh_tunnel", "db_engine": "mysql",
        "bastion_host": "b", "bastion_user": "u", "bastion_key": "k.pem", "remote_host": "db",
    }
    tunnel.update(data)
    return TunnelConfigDialog(parent, tunnel_data=tunnel)


def test_accept_rejects_missing_required_fields_and_marks_first(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warnings.append(a[2]))
    dialog = _ssh_dialog(name="", bastion_key="", local_port=3308)
    try:
        dialog.accept()
        assert dialog.result() != QDialog.DialogCode.Accepted
        assert "이름(별칭)" in warnings[0] and "SSH Key" in warnings[0]
        assert "#c0392b" in dialog.input_name.styleSheet()
        assert "#c0392b" in dialog.input_bastion_key.styleSheet()
        assert dialog.input_bastion_host.styleSheet() == ""

        # 직접 연결 모드에서는 Bastion/Endpoint가 필수가 아니다.
        dialog.input_name.setText("ok")
        dialog.radio_direct.setChecked(True)
        dialog.input_remote_host.clear()
        assert dialog._missing_required_fields() == []
        dialog.accept()
        assert dialog.result() == QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()


def test_local_port_defaults_to_next_free_and_warns_on_collision(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    parent = ParentWithTunnels()
    parent.tunnels[0]["local_port"] = 3308
    parent.tunnels[1]["local_port"] = 3309
    parent.tunnels[2]["local_port"] = 3310  # direct 연결은 포트를 쓰지 않는다
    new_dialog = TunnelConfigDialog(parent)
    assert new_dialog.input_local_port.value() == 3310
    new_dialog.deleteLater()

    asked = []
    monkeypatch.setattr(QMessageBox, "question",
                        lambda *a, **k: asked.append(a[2]) or QMessageBox.StandardButton.No)
    dialog = _ssh_dialog(parent, local_port=3309)
    try:
        dialog.accept()
        assert asked and "3309" in asked[0]
        assert dialog.result() != QDialog.DialogCode.Accepted
    finally:
        dialog.deleteLater()
        parent.close()


def test_credentials_usable_without_saving_and_uncheck_confirms(monkeypatch):
    from PyQt6.QtWidgets import QMessageBox

    blank = TunnelConfigDialog(None, tunnel_data={"id": "t"})
    try:
        assert not blank.chk_save_credentials.isChecked()
        assert blank.input_db_user.isEnabled() and blank.input_db_password.isEnabled()
        assert blank.btn_db_test.isEnabled()
    finally:
        blank.deleteLater()

    answers = [QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: answers.pop(0))
    dialog = TunnelConfigDialog(None, tunnel_data={"id": "t", "db_user": "app",
                                                   "db_password_encrypted": "enc"})
    try:
        dialog.chk_save_credentials.setChecked(False)  # No -> 되돌림
        assert dialog.chk_save_credentials.isChecked()
        assert dialog.get_data()["db_password_encrypted"] == "enc"
        dialog.chk_save_credentials.setChecked(False)  # Yes -> 저장 시 삭제
        assert not dialog.chk_save_credentials.isChecked()
        assert dialog.input_db_user.text() == "app"  # 입력값은 지우지 않는다
        assert "db_user" not in dialog.get_data()
    finally:
        dialog.deleteLater()


def test_environment_is_near_name_with_description():
    dialog = TunnelConfigDialog(None, tunnel_data={"id": "t", "environment": "production"})
    try:
        form = dialog._form_layout
        env_row = form.getWidgetPosition(dialog.combo_environment)[0]
        assert env_row == form.getWidgetPosition(dialog.input_name)[0] + 1
        assert "스키마명 직접 입력" in dialog.lbl_environment_desc.text()
        dialog.combo_environment.setCurrentIndex(dialog.combo_environment.findData("development"))
        assert "확인 없이" in dialog.lbl_environment_desc.text()
    finally:
        dialog.deleteLater()


def test_direct_mode_hides_ssh_only_rows():
    dialog = TunnelConfigDialog(None, tunnel_data={"id": "t", "connection_mode": "direct"})
    try:
        form = dialog._form_layout
        for widget in (dialog.input_bastion_host, dialog.key_layout_widget, dialog.input_local_port,
                       dialog.btn_tunnel_test, dialog.btn_host_key, dialog.lbl_bastion):
            assert not form.isRowVisible(widget)
        dialog.radio_ssh_tunnel.setChecked(True)
        assert form.isRowVisible(dialog.input_bastion_host)
        assert form.isRowVisible(dialog.btn_tunnel_test)
    finally:
        dialog.deleteLater()


def test_engine_change_switches_default_port_only():
    dialog = TunnelConfigDialog(None, tunnel_data={"id": "t"})
    try:
        assert dialog.input_remote_port.value() == 3306
        dialog.combo_db_engine.setCurrentIndex(dialog.combo_db_engine.findData("postgresql"))
        assert dialog.input_remote_port.value() == 5432
        dialog.combo_db_engine.setCurrentIndex(dialog.combo_db_engine.findData("mysql"))
        assert dialog.input_remote_port.value() == 3306
        dialog.input_remote_port.setValue(13306)
        dialog.combo_db_engine.setCurrentIndex(dialog.combo_db_engine.findData("postgresql"))
        assert dialog.input_remote_port.value() == 13306
    finally:
        dialog.deleteLater()
