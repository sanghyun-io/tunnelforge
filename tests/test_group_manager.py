"""
TunnelGroupManager 테스트
"""
import pytest
import os
import importlib
from unittest.mock import patch

# 실제 패키지로 import해둔다. 각 테스트는 patch.dict(os.environ, ...)를 적용한
# 뒤 이 모듈을 reload해서 모듈 레벨 경로 상수를 그 테스트의 임시 디렉토리
# 기준으로 다시 계산하게 만든다. (tests/test_config_manager.py와 동일 패턴)
import src.core.config_manager as _config_manager_module


def _load_config_manager_module():
    """환경변수(LOCALAPPDATA/HOME) 패치가 적용된 상태에서 config_manager를 reload한다."""
    return importlib.reload(_config_manager_module)


class TestTunnelGroupManager:
    """TunnelGroupManager 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """각 테스트 전 임시 디렉토리로 환경 설정"""
        self.test_dir = tmp_path / 'TunnelForge'
        self.test_dir.mkdir()

        self.env_patch = patch.dict(
            os.environ,
            {'LOCALAPPDATA': str(tmp_path), 'HOME': str(tmp_path)}
        )
        self.env_patch.start()

        config_module = _load_config_manager_module()
        self.config_module = config_module
        self.config_mgr = config_module.ConfigManager()
        self.group_mgr = self.config_mgr.group_manager

    def teardown_method(self):
        self.env_patch.stop()

    # -- add_group -----------------------------------------------------

    def test_add_group_success(self):
        """정상적인 그룹 생성"""
        success, msg, group_id = self.group_mgr.add_group("운영 서버", "#ff0000")

        assert success is True
        assert "생성" in msg
        assert group_id is not None

        groups = self.group_mgr.get_groups()
        assert len(groups) == 1
        assert groups[0]['id'] == group_id
        assert groups[0]['name'] == "운영 서버"
        assert groups[0]['color'] == "#ff0000"
        assert groups[0]['collapsed'] is False
        assert groups[0]['tunnel_ids'] == []

    def test_add_group_duplicate_name_rejected_without_save(self):
        """중복된 그룹 이름은 거부되고 저장도 되지 않아야 한다"""
        self.group_mgr.add_group("운영 서버")

        backups_before = self.config_mgr.list_backups()

        success, msg, group_id = self.group_mgr.add_group("운영 서버")

        assert success is False
        assert "이미 존재하는" in msg
        assert group_id is None

        groups = self.group_mgr.get_groups()
        assert len(groups) == 1

        # 조기 실패 반환이므로 저장(백업 생성)이 일어나지 않아야 한다
        backups_after = self.config_mgr.list_backups()
        assert len(backups_after) == len(backups_before)

    # -- update_group ----------------------------------------------------

    def test_update_group_not_found(self):
        """존재하지 않는 그룹 수정 시 실패"""
        success, msg = self.group_mgr.update_group("no-such-id", {"name": "new"})

        assert success is False
        assert "찾을 수 없습니다" in msg

    def test_update_group_rename_duplicate_rejected(self):
        """다른 그룹과 이름이 겹치는 rename은 거부되어야 한다"""
        _, _, group_id_a = self.group_mgr.add_group("그룹 A")
        _, _, group_id_b = self.group_mgr.add_group("그룹 B")

        success, msg = self.group_mgr.update_group(group_id_b, {"name": "그룹 A"})

        assert success is False
        assert "이미 존재하는" in msg

        groups = {g['id']: g for g in self.group_mgr.get_groups()}
        assert groups[group_id_b]['name'] == "그룹 B"

    def test_update_group_success(self):
        """정상적인 그룹 수정"""
        _, _, group_id = self.group_mgr.add_group("그룹 A", "#111111")

        success, msg = self.group_mgr.update_group(
            group_id, {"name": "그룹 A2", "color": "#222222", "collapsed": True}
        )

        assert success is True
        assert "수정" in msg

        groups = {g['id']: g for g in self.group_mgr.get_groups()}
        assert groups[group_id]['name'] == "그룹 A2"
        assert groups[group_id]['color'] == "#222222"
        assert groups[group_id]['collapsed'] is True

    # -- delete_group ----------------------------------------------------

    def test_delete_group_moves_tunnels_to_ungrouped_order(self):
        """그룹 삭제 시 tunnel_ids가 ungrouped_order로 이동해야 한다"""
        _, _, group_id = self.group_mgr.add_group("그룹 A")

        # delete_group은 group.tunnel_ids를 그대로 옮기므로, move_tunnel_to_group을
        # 거치지 않고 config에 직접 tunnel_ids를 채워 시나리오를 구성한다.
        config = self.config_mgr.load_config()
        for group in config['tunnel_groups']:
            if group['id'] == group_id:
                group['tunnel_ids'] = ["tunnel-1", "tunnel-2"]
        self.config_mgr.save_config(config)

        success, msg = self.group_mgr.delete_group(group_id)

        assert success is True
        assert "삭제" in msg
        assert self.group_mgr.get_groups() == []

        config = self.config_mgr.load_config()
        assert set(config.get('ungrouped_order', [])) == {"tunnel-1", "tunnel-2"}

    def test_delete_group_not_found(self):
        """존재하지 않는 그룹 삭제 시 실패"""
        success, msg = self.group_mgr.delete_group("no-such-id")

        assert success is False
        assert "찾을 수 없습니다" in msg

    # -- move_tunnel_to_group ---------------------------------------------

    def test_move_tunnel_to_group_tunnel_not_found(self):
        """존재하지 않는 터널을 이동하려 하면 실패"""
        _, _, group_id = self.group_mgr.add_group("그룹 A")

        sample_config_data = self.config_mgr.load_config()
        sample_config_data['tunnels'] = []
        self.config_mgr.save_config(sample_config_data)

        success, msg = self.group_mgr.move_tunnel_to_group("no-such-tunnel", group_id)

        assert success is False
        assert "터널을 찾을 수 없습니다" in msg

    def test_move_tunnel_to_group_target_group_not_found(self, sample_config_data):
        """존재하지 않는 대상 그룹으로 이동하려 하면 실패"""
        self.config_mgr.save_config(sample_config_data)

        success, msg = self.group_mgr.move_tunnel_to_group("test-001", "no-such-group")

        assert success is False
        assert "대상 그룹을 찾을 수 없습니다" in msg

    def test_move_tunnel_to_group_none_moves_to_ungrouped(self, sample_config_data):
        """group_id=None이면 그룹 없음(ungrouped_order)으로 이동해야 한다"""
        self.config_mgr.save_config(sample_config_data)
        _, _, group_id = self.group_mgr.add_group("그룹 A")
        self.group_mgr.move_tunnel_to_group("test-001", group_id)

        success, msg = self.group_mgr.move_tunnel_to_group("test-001", None)

        assert success is True
        assert self.group_mgr.get_tunnel_group("test-001") is None

        config = self.config_mgr.load_config()
        assert "test-001" in config.get('ungrouped_order', [])

    def test_move_tunnel_to_group_between_groups(self, sample_config_data):
        """터널을 한 그룹에서 다른 그룹으로 이동하면 이전 그룹에서 제거되어야 한다"""
        self.config_mgr.save_config(sample_config_data)
        _, _, group_id_a = self.group_mgr.add_group("그룹 A")
        _, _, group_id_b = self.group_mgr.add_group("그룹 B")

        self.group_mgr.move_tunnel_to_group("test-001", group_id_a)
        success, msg = self.group_mgr.move_tunnel_to_group("test-001", group_id_b)

        assert success is True
        assert self.group_mgr.get_tunnel_group("test-001") == group_id_b

        groups = {g['id']: g for g in self.group_mgr.get_groups()}
        assert "test-001" not in groups[group_id_a]['tunnel_ids']
        assert "test-001" in groups[group_id_b]['tunnel_ids']

    # -- get_tunnel_group --------------------------------------------------

    def test_get_tunnel_group_none_when_ungrouped(self, sample_config_data):
        """그룹에 속하지 않은 터널은 None을 반환해야 한다"""
        self.config_mgr.save_config(sample_config_data)

        assert self.group_mgr.get_tunnel_group("test-001") is None

    # -- save_group_collapsed_state ------------------------------------------

    def test_save_group_collapsed_state_success(self):
        """그룹 접기 상태 저장 성공"""
        _, _, group_id = self.group_mgr.add_group("그룹 A")

        result = self.group_mgr.save_group_collapsed_state(group_id, True)

        assert result is True
        groups = {g['id']: g for g in self.group_mgr.get_groups()}
        assert groups[group_id]['collapsed'] is True

    def test_save_group_collapsed_state_not_found(self):
        """존재하지 않는 그룹의 접기 상태 저장은 실패해야 한다"""
        result = self.group_mgr.save_group_collapsed_state("no-such-id", True)

        assert result is False

    # -- ConfigManager facade 위임 회귀 테스트 --------------------------------

    def test_config_manager_facade_delegates_to_group_manager(self):
        """ConfigManager의 그룹 메서드 호출이 여전히 동작해야 한다 (facade 위임)"""
        success, msg, group_id = self.config_mgr.add_group("파사드 그룹", "#abcdef")
        assert success is True

        groups = self.config_mgr.get_groups()
        assert any(g['id'] == group_id for g in groups)

        success, msg = self.config_mgr.update_group(group_id, {"name": "파사드 그룹 2"})
        assert success is True

        assert self.config_mgr.save_group_collapsed_state(group_id, True) is True

        # 이동 동작도 facade를 통해 확인
        cfg = self.config_mgr.load_config()
        cfg['tunnels'] = [{'id': 'facade-tunnel', 'name': 't', 'remote_host': 'h', 'remote_port': 3306}]
        self.config_mgr.save_config(cfg)

        success, msg = self.config_mgr.move_tunnel_to_group('facade-tunnel', group_id)
        assert success is True
        assert self.config_mgr.get_tunnel_group('facade-tunnel') == group_id

        success, msg = self.config_mgr.delete_group(group_id)
        assert success is True
        assert self.config_mgr.get_groups() == []
