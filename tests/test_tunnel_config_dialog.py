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
