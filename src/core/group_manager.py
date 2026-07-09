"""
터널 그룹 관리 - TunnelGroupManager

ConfigManager로부터 주입받아 그룹 CRUD(생성/수정/삭제/이동/조회/접기상태)를 담당한다.
읽기는 config_manager.load_config(), 쓰기는 config_manager._mutate_config(...)를 통해서만
수행하며, 모듈 레벨 경로/락 상수는 두지 않는다 (ConfigManager가 reload로 테스트 격리를
하므로, 상태는 반드시 주입된 인스턴스를 통해서만 접근해야 한다).
"""
import uuid
from typing import List, Optional, Tuple, TYPE_CHECKING

from src.core.logger import get_logger

if TYPE_CHECKING:
    from src.core.config_manager import ConfigManager

logger = get_logger('group_manager')


class TunnelGroupManager:
    """터널 그룹 CRUD 담당. config_manager 인스턴스를 주입받아 load/save를 위임한다."""

    def __init__(self, config_manager: "ConfigManager"):
        self._config = config_manager

    def get_groups(self) -> List[dict]:
        """모든 그룹 목록 반환

        Returns:
            그룹 목록: [{"id", "name", "color", "collapsed", "tunnel_ids"}, ...]
        """
        config = self._config.load_config()
        return config.get('tunnel_groups', [])

    def add_group(self, name: str, color: str = "#3498db") -> Tuple[bool, str, Optional[str]]:
        """새 그룹 생성

        Args:
            name: 그룹 이름
            color: 그룹 색상 (hex 코드)

        Returns:
            (success, message, group_id) 튜플
        """
        def mutator(config):
            if 'tunnel_groups' not in config:
                config['tunnel_groups'] = []

            # 중복 이름 확인
            for group in config['tunnel_groups']:
                if group['name'] == name:
                    return False, (False, f"이미 존재하는 그룹 이름입니다: {name}", None)

            group_id = str(uuid.uuid4())
            new_group = {
                "id": group_id,
                "name": name,
                "color": color,
                "collapsed": False,
                "tunnel_ids": []
            }

            config['tunnel_groups'].append(new_group)
            return True, (True, f"그룹이 생성되었습니다: {name}", group_id)

        success, message, group_id = self._config._mutate_config(mutator)
        if success:
            logger.info(f"그룹 생성: {name} (ID: {group_id})")
        return success, message, group_id

    def update_group(self, group_id: str, data: dict) -> Tuple[bool, str]:
        """그룹 정보 수정

        Args:
            group_id: 수정할 그룹 ID
            data: 수정할 필드들 {"name", "color", "collapsed"}

        Returns:
            (success, message) 튜플
        """
        def mutator(config):
            groups = config.get('tunnel_groups', [])

            for group in groups:
                if group['id'] == group_id:
                    # 이름 변경 시 중복 확인
                    if 'name' in data and data['name'] != group['name']:
                        for other in groups:
                            if other['id'] != group_id and other['name'] == data['name']:
                                return False, (False, f"이미 존재하는 그룹 이름입니다: {data['name']}")

                    # 허용된 필드만 업데이트
                    for key in ['name', 'color', 'collapsed']:
                        if key in data:
                            group[key] = data[key]

                    logger.info(f"그룹 수정: {group_id}")
                    return True, (True, "그룹이 수정되었습니다.")

            return False, (False, f"그룹을 찾을 수 없습니다: {group_id}")

        return self._config._mutate_config(mutator)

    def delete_group(self, group_id: str) -> Tuple[bool, str]:
        """그룹 삭제 (터널은 그룹 없음으로 이동)

        Args:
            group_id: 삭제할 그룹 ID

        Returns:
            (success, message) 튜플
        """
        def mutator(config):
            groups = config.get('tunnel_groups', [])

            for i, group in enumerate(groups):
                if group['id'] == group_id:
                    group_name = group['name']

                    # 그룹에 속한 터널들을 ungrouped_order로 이동
                    if 'ungrouped_order' not in config:
                        config['ungrouped_order'] = []
                    config['ungrouped_order'].extend(group.get('tunnel_ids', []))

                    # 그룹 삭제
                    groups.pop(i)
                    logger.info(f"그룹 삭제: {group_name} (ID: {group_id})")
                    return True, (True, f"그룹이 삭제되었습니다: {group_name}")

            return False, (False, f"그룹을 찾을 수 없습니다: {group_id}")

        return self._config._mutate_config(mutator)

    def move_tunnel_to_group(self, tunnel_id: str, group_id: Optional[str]) -> Tuple[bool, str]:
        """터널을 그룹으로 이동

        Args:
            tunnel_id: 이동할 터널 ID
            group_id: 대상 그룹 ID (None이면 그룹 없음으로 이동)

        Returns:
            (success, message) 튜플
        """
        def mutator(config):
            # 터널 존재 확인
            tunnel_exists = any(t['id'] == tunnel_id for t in config.get('tunnels', []))
            if not tunnel_exists:
                return False, (False, f"터널을 찾을 수 없습니다: {tunnel_id}")

            # ungrouped_order 초기화
            if 'ungrouped_order' not in config:
                config['ungrouped_order'] = []

            # 기존 그룹에서 터널 제거
            for group in config.get('tunnel_groups', []):
                if tunnel_id in group.get('tunnel_ids', []):
                    group['tunnel_ids'].remove(tunnel_id)

            # ungrouped_order에서도 제거
            if tunnel_id in config['ungrouped_order']:
                config['ungrouped_order'].remove(tunnel_id)

            # 새 그룹에 추가 또는 ungrouped로 이동
            if group_id is None:
                config['ungrouped_order'].append(tunnel_id)
            else:
                for group in config.get('tunnel_groups', []):
                    if group['id'] == group_id:
                        if 'tunnel_ids' not in group:
                            group['tunnel_ids'] = []
                        group['tunnel_ids'].append(tunnel_id)
                        break
                else:
                    return False, (False, f"대상 그룹을 찾을 수 없습니다: {group_id}")

            return True, (True, "터널이 이동되었습니다.")

        success, message = self._config._mutate_config(mutator)
        if success:
            logger.debug(f"터널 이동: {tunnel_id} -> 그룹 {group_id or '(없음)'}")
        return success, message

    def get_tunnel_group(self, tunnel_id: str) -> Optional[str]:
        """터널이 속한 그룹 ID 반환

        Args:
            tunnel_id: 터널 ID

        Returns:
            그룹 ID (그룹 없으면 None)
        """
        config = self._config.load_config()

        for group in config.get('tunnel_groups', []):
            if tunnel_id in group.get('tunnel_ids', []):
                return group['id']

        return None

    def save_group_collapsed_state(self, group_id: str, collapsed: bool) -> bool:
        """그룹 접기/펼치기 상태 저장

        Args:
            group_id: 그룹 ID
            collapsed: 접힘 상태

        Returns:
            성공 여부
        """
        def mutator(config):
            for group in config.get('tunnel_groups', []):
                if group['id'] == group_id:
                    group['collapsed'] = collapsed
                    return True, True

            return False, False

        return self._config._mutate_config(mutator)
