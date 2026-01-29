import json
import os
import uuid
import shutil
from datetime import datetime
from typing import List, Tuple, Optional
from cryptography.fernet import Fernet

from src.core.logger import get_logger

logger = get_logger('config_manager')

# 운영체제별 설정 파일 저장 경로 지정
# Windows: C:\Users\User\AppData\Local\TunnelForge
# Mac/Linux: ~/.config/tunnelforge
if os.name == 'nt':
    APP_DIR = os.path.join(os.environ['LOCALAPPDATA'], 'TunnelForge')
else:
    APP_DIR = os.path.join(os.path.expanduser('~'), '.config', 'tunnelforge')

CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
KEY_FILE = os.path.join(APP_DIR, '.encryption_key')
BACKUP_DIR = os.path.join(APP_DIR, 'backups')
MAX_BACKUPS = 5


class CredentialEncryptor:
    """MySQL 자격 증명 암호화/복호화"""

    def __init__(self):
        self._fernet = None
        self._ensure_key_exists()

    def _ensure_key_exists(self):
        """암호화 키 파일이 없으면 생성"""
        if not os.path.exists(KEY_FILE):
            key = Fernet.generate_key()
            with open(KEY_FILE, 'wb') as f:
                f.write(key)
            # Windows 숨김 파일 설정
            if os.name == 'nt':
                import ctypes
                ctypes.windll.kernel32.SetFileAttributesW(KEY_FILE, 0x02)

        with open(KEY_FILE, 'rb') as f:
            self._fernet = Fernet(f.read())

    def encrypt(self, plain_text: str) -> str:
        """평문을 암호화"""
        if not plain_text:
            return ""
        return self._fernet.encrypt(plain_text.encode('utf-8')).decode('utf-8')

    def decrypt(self, encrypted_text: str) -> str:
        """암호문을 복호화"""
        if not encrypted_text:
            return ""
        try:
            return self._fernet.decrypt(encrypted_text.encode('utf-8')).decode('utf-8')
        except Exception:
            return ""


class ConfigManager:
    def __init__(self):
        self._encryptor = None
        self._ensure_config_exists()

    def _ensure_config_exists(self):
        """설정 폴더와 파일이 없으면 기본값을 생성합니다."""
        if not os.path.exists(APP_DIR):
            os.makedirs(APP_DIR)
        
        if not os.path.exists(CONFIG_FILE):
            # 초기 실행 시 보여줄 더미 데이터
            default_config = {
                "tunnels": [
                    {
                        "id": str(uuid.uuid4()),
                        "name": "테스트 서버 (예시)",
                        "bastion_host": "1.2.3.4",
                        "bastion_port": 22,
                        "bastion_user": "ec2-user",
                        "bastion_key": "", # 키 파일 경로 비어있음
                        "remote_host": "rds-endpoint.amazonaws.com",
                        "remote_port": 3306,
                        "local_port": 3308
                    }
                ]
            }
            self.save_config(default_config)

    def load_config(self):
        """설정 파일을 읽어서 반환합니다."""
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"설정 로드 오류: {e}")
            return {"tunnels": []}

    def save_config(self, data):
        """설정 데이터를 파일에 저장합니다."""
        # 저장 전 자동 백업
        self._create_backup()

        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        logger.debug(f"설정 저장 완료: {CONFIG_FILE}")

    def _create_backup(self):
        """설정 변경 전 자동 백업"""
        if not os.path.exists(CONFIG_FILE):
            return

        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            backup_path = os.path.join(BACKUP_DIR, f'config.backup.{timestamp}.json')
            shutil.copy2(CONFIG_FILE, backup_path)
            logger.debug(f"설정 백업 생성: {backup_path}")
            self._cleanup_old_backups()
        except Exception as e:
            logger.warning(f"백업 생성 실패: {e}")

    def _cleanup_old_backups(self):
        """오래된 백업 파일 정리 (MAX_BACKUPS 초과 시 삭제)"""
        try:
            if not os.path.exists(BACKUP_DIR):
                return

            backups = self._get_backup_files()
            if len(backups) > MAX_BACKUPS:
                # 가장 오래된 백업부터 삭제
                for backup_file, _, _ in backups[MAX_BACKUPS:]:
                    backup_path = os.path.join(BACKUP_DIR, backup_file)
                    os.remove(backup_path)
                    logger.debug(f"오래된 백업 삭제: {backup_file}")
        except Exception as e:
            logger.warning(f"백업 정리 실패: {e}")

    def _get_backup_files(self) -> List[Tuple[str, str, int]]:
        """백업 파일 목록 반환 (최신순 정렬)"""
        if not os.path.exists(BACKUP_DIR):
            return []

        backups = []
        for filename in os.listdir(BACKUP_DIR):
            if filename.startswith('config.backup.') and filename.endswith('.json'):
                filepath = os.path.join(BACKUP_DIR, filename)
                # 파일명에서 타임스탬프 추출: config.backup.YYYYMMDD_HHMMSS.json
                try:
                    timestamp_str = filename[14:-5]  # "YYYYMMDD_HHMMSS" 부분
                    dt = datetime.strptime(timestamp_str, '%Y%m%d_%H%M%S')
                    display_time = dt.strftime('%Y-%m-%d %H:%M:%S')
                    size = os.path.getsize(filepath)
                    backups.append((filename, display_time, size))
                except (ValueError, OSError):
                    continue

        # 최신순 정렬
        backups.sort(key=lambda x: x[0], reverse=True)
        return backups

    def list_backups(self) -> List[Tuple[str, str, int]]:
        """백업 목록 반환 [(파일명, 타임스탬프, 크기), ...]"""
        return self._get_backup_files()

    def restore_backup(self, filename: str) -> Tuple[bool, str]:
        """선택한 백업으로 복원

        Args:
            filename: 복원할 백업 파일명

        Returns:
            (success, message) 튜플
        """
        backup_path = os.path.join(BACKUP_DIR, filename)

        if not os.path.exists(backup_path):
            return False, f"백업 파일을 찾을 수 없습니다: {filename}"

        try:
            # 복원 전 현재 설정 백업
            self._create_backup()

            # 백업 파일 검증 (유효한 JSON인지 확인)
            with open(backup_path, 'r', encoding='utf-8') as f:
                json.load(f)  # 유효성 검증만 수행

            # 복원 실행
            shutil.copy2(backup_path, CONFIG_FILE)
            logger.info(f"설정 복원 완료: {filename}")
            return True, f"설정이 복원되었습니다: {filename}"

        except json.JSONDecodeError:
            return False, "백업 파일이 손상되었습니다."
        except Exception as e:
            logger.error(f"설정 복원 실패: {e}")
            return False, f"복원 중 오류 발생: {e}"

    def export_config(self, export_path: str) -> Tuple[bool, str]:
        """설정 파일을 외부 경로로 내보내기

        Args:
            export_path: 내보낼 파일 경로

        Returns:
            (success, message) 튜플
        """
        if not os.path.exists(CONFIG_FILE):
            return False, "설정 파일이 없습니다."

        try:
            shutil.copy2(CONFIG_FILE, export_path)
            logger.info(f"설정 내보내기 완료: {export_path}")
            return True, f"설정이 내보내기되었습니다: {export_path}"
        except Exception as e:
            logger.error(f"설정 내보내기 실패: {e}")
            return False, f"내보내기 중 오류 발생: {e}"

    def import_config(self, import_path: str) -> Tuple[bool, str]:
        """외부 파일에서 설정 가져오기

        Args:
            import_path: 가져올 파일 경로

        Returns:
            (success, message) 튜플
        """
        if not os.path.exists(import_path):
            return False, f"파일을 찾을 수 없습니다: {import_path}"

        try:
            # 파일 유효성 검사
            with open(import_path, 'r', encoding='utf-8') as f:
                import_data = json.load(f)

            # 필수 필드 확인
            if 'tunnels' not in import_data:
                return False, "유효하지 않은 설정 파일입니다. (tunnels 필드 누락)"

            # 현재 설정 백업
            self._create_backup()

            # 가져오기 실행
            shutil.copy2(import_path, CONFIG_FILE)
            logger.info(f"설정 가져오기 완료: {import_path}")
            return True, f"설정이 가져오기되었습니다: {import_path}"

        except json.JSONDecodeError:
            return False, "유효하지 않은 JSON 파일입니다."
        except Exception as e:
            logger.error(f"설정 가져오기 실패: {e}")
            return False, f"가져오기 중 오류 발생: {e}"

    def get_backup_dir(self) -> str:
        """백업 디렉토리 경로 반환"""
        return BACKUP_DIR

    def get_config_path(self):
        return CONFIG_FILE

    def get_app_setting(self, key, default=None):
        """앱 설정 값 조회"""
        config = self.load_config()
        return config.get('settings', {}).get(key, default)

    def set_app_setting(self, key, value):
        """앱 설정 값 저장 (기존 설정 유지)"""
        config = self.load_config()
        if 'settings' not in config:
            config['settings'] = {}
        config['settings'][key] = value
        self.save_config(config)

    @property
    def encryptor(self):
        """CredentialEncryptor 인스턴스 (lazy initialization)"""
        if self._encryptor is None:
            self._encryptor = CredentialEncryptor()
        return self._encryptor

    def get_tunnel_credentials(self, tunnel_id: str) -> tuple:
        """터널의 MySQL 자격 증명 조회 -> (user, password)"""
        config = self.load_config()
        for tunnel in config.get('tunnels', []):
            if tunnel.get('id') == tunnel_id:
                db_user = tunnel.get('db_user', '')
                encrypted_pw = tunnel.get('db_password_encrypted', '')
                db_password = self.encryptor.decrypt(encrypted_pw)
                return (db_user, db_password)
        return ('', '')

    def save_active_tunnels(self, tunnel_ids: list):
        """종료 시 활성화된 터널 ID 목록 저장"""
        config = self.load_config()
        config['last_active_tunnels'] = tunnel_ids
        self.save_config(config)
        logger.info(f"활성 터널 상태 저장: {len(tunnel_ids)}개")

    def get_last_active_tunnels(self) -> list:
        """마지막으로 활성화되어 있던 터널 ID 목록 반환"""
        config = self.load_config()
        return config.get('last_active_tunnels', [])

    # =========================================================================
    # 그룹 관리 메서드
    # =========================================================================

    def get_groups(self) -> List[dict]:
        """모든 그룹 목록 반환

        Returns:
            그룹 목록: [{"id", "name", "color", "collapsed", "tunnel_ids"}, ...]
        """
        config = self.load_config()
        return config.get('tunnel_groups', [])

    def add_group(self, name: str, color: str = "#3498db") -> Tuple[bool, str, Optional[str]]:
        """새 그룹 생성

        Args:
            name: 그룹 이름
            color: 그룹 색상 (hex 코드)

        Returns:
            (success, message, group_id) 튜플
        """
        config = self.load_config()

        if 'tunnel_groups' not in config:
            config['tunnel_groups'] = []

        # 중복 이름 확인
        for group in config['tunnel_groups']:
            if group['name'] == name:
                return False, f"이미 존재하는 그룹 이름입니다: {name}", None

        group_id = str(uuid.uuid4())
        new_group = {
            "id": group_id,
            "name": name,
            "color": color,
            "collapsed": False,
            "tunnel_ids": []
        }

        config['tunnel_groups'].append(new_group)
        self.save_config(config)
        logger.info(f"그룹 생성: {name} (ID: {group_id})")
        return True, f"그룹이 생성되었습니다: {name}", group_id

    def update_group(self, group_id: str, data: dict) -> Tuple[bool, str]:
        """그룹 정보 수정

        Args:
            group_id: 수정할 그룹 ID
            data: 수정할 필드들 {"name", "color", "collapsed"}

        Returns:
            (success, message) 튜플
        """
        config = self.load_config()
        groups = config.get('tunnel_groups', [])

        for group in groups:
            if group['id'] == group_id:
                # 이름 변경 시 중복 확인
                if 'name' in data and data['name'] != group['name']:
                    for other in groups:
                        if other['id'] != group_id and other['name'] == data['name']:
                            return False, f"이미 존재하는 그룹 이름입니다: {data['name']}"

                # 허용된 필드만 업데이트
                for key in ['name', 'color', 'collapsed']:
                    if key in data:
                        group[key] = data[key]

                self.save_config(config)
                logger.info(f"그룹 수정: {group_id}")
                return True, "그룹이 수정되었습니다."

        return False, f"그룹을 찾을 수 없습니다: {group_id}"

    def delete_group(self, group_id: str) -> Tuple[bool, str]:
        """그룹 삭제 (터널은 그룹 없음으로 이동)

        Args:
            group_id: 삭제할 그룹 ID

        Returns:
            (success, message) 튜플
        """
        config = self.load_config()
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
                self.save_config(config)
                logger.info(f"그룹 삭제: {group_name} (ID: {group_id})")
                return True, f"그룹이 삭제되었습니다: {group_name}"

        return False, f"그룹을 찾을 수 없습니다: {group_id}"

    def move_tunnel_to_group(self, tunnel_id: str, group_id: Optional[str]) -> Tuple[bool, str]:
        """터널을 그룹으로 이동

        Args:
            tunnel_id: 이동할 터널 ID
            group_id: 대상 그룹 ID (None이면 그룹 없음으로 이동)

        Returns:
            (success, message) 튜플
        """
        config = self.load_config()

        # 터널 존재 확인
        tunnel_exists = any(t['id'] == tunnel_id for t in config.get('tunnels', []))
        if not tunnel_exists:
            return False, f"터널을 찾을 수 없습니다: {tunnel_id}"

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
                return False, f"대상 그룹을 찾을 수 없습니다: {group_id}"

        self.save_config(config)
        logger.debug(f"터널 이동: {tunnel_id} -> 그룹 {group_id or '(없음)'}")
        return True, "터널이 이동되었습니다."

    def get_tunnel_group(self, tunnel_id: str) -> Optional[str]:
        """터널이 속한 그룹 ID 반환

        Args:
            tunnel_id: 터널 ID

        Returns:
            그룹 ID (그룹 없으면 None)
        """
        config = self.load_config()

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
        config = self.load_config()

        for group in config.get('tunnel_groups', []):
            if group['id'] == group_id:
                group['collapsed'] = collapsed
                self.save_config(config)
                return True

        return False