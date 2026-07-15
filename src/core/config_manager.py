import copy
import json
import os
import threading
import uuid
from datetime import datetime
from typing import Callable, Dict, List, Mapping, Optional, Tuple, TypeVar
from cryptography.fernet import Fernet

from src.core.logger import get_logger
from src.core.constants import DEFAULT_MYSQL_PORT
from src.core.platform_paths import backups_dir, config_file, encryption_key_file, app_support_dir
from src.core.group_manager import TunnelGroupManager

logger = get_logger('config_manager')

APP_DIR = str(app_support_dir())
CONFIG_FILE = str(config_file())
KEY_FILE = str(encryption_key_file())
BACKUP_DIR = str(backups_dir())
MAX_BACKUPS = 5
MAX_IMPORT_CONFIG_BYTES = 1_048_576
MAX_IMPORT_CONFIG_DEPTH = 64
MAX_IMPORT_CONFIG_COLLECTION_ITEMS = 500
FILE_ATTRIBUTE_HIDDEN = 0x02  # Win32 SetFileAttributesW 플래그: 숨김 파일 속성

# load_config/save_config를 여러 스레드(스케줄러, UI)가 동시에 호출해도
# 설정 파일이 중간 상태로 노출되지 않도록 보호하는 프로세스 전역 락.
# 공개 메서드들이 서로를 호출할 수 있어 재진입 가능한 RLock을 사용한다.
_CONFIG_LOCK = threading.RLock()

# _merge_snapshot_changes에서 "이 키는 원본에 존재하지 않았다"를 None과 구분하기 위한 sentinel
_MISSING = object()
_AppSettingsMutationResult = TypeVar('_AppSettingsMutationResult')
_NON_TRANSFERABLE_REPORTING_SETTINGS = frozenset({'github_auto_report'})
_NON_TRANSFERABLE_REPORTING_PREFIX = 'error_reporting_'


def _is_reporting_privacy_setting(key) -> bool:
    normalized_key = key.casefold() if isinstance(key, str) else ''
    return (
        normalized_key.startswith(_NON_TRANSFERABLE_REPORTING_PREFIX)
        or normalized_key in _NON_TRANSFERABLE_REPORTING_SETTINGS
    )


def _reporting_privacy_state(config_data: dict) -> dict:
    settings = config_data.get('settings')
    if not isinstance(settings, dict):
        return {}
    return {
        key: copy.deepcopy(value)
        for key, value in settings.items()
        if _is_reporting_privacy_setting(key)
    }


def _without_reporting_privacy_state(config_data: dict) -> dict:
    """Return a detached config without destination-local reporting state."""
    sanitized = copy.deepcopy(config_data)
    settings = sanitized.get('settings')
    if not isinstance(settings, dict):
        return sanitized
    for key in list(settings):
        if _is_reporting_privacy_setting(key):
            del settings[key]
    return sanitized


def _with_local_reporting_privacy_state(
    incoming_config: dict,
    local_config: dict,
) -> dict:
    merged = _without_reporting_privacy_state(incoming_config)
    local_privacy_state = _reporting_privacy_state(local_config)
    if local_privacy_state:
        merged.setdefault('settings', {}).update(local_privacy_state)
    return merged


class ConfigLoadError(RuntimeError):
    """설정 파일이 손상되었고 복원 가능한 백업도 없을 때 발생하는 예외"""
    pass


class _ConfigSnapshot(dict):
    """load_config()가 반환하는 설정 스냅샷.

    저장 시점에 디스크의 실제 상태가 로드 시점과 달라졌는지(스테일 여부) 판단하기 위해
    로드 당시의 파일 리비전과 원본 페이로드를 함께 보관한다.
    """

    def __init__(self, payload: dict, source_revision: Optional[Tuple[int, int]], original_payload: dict):
        super().__init__(payload)
        self._source_revision = source_revision
        self._original_payload = original_payload


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
                ctypes.windll.kernel32.SetFileAttributesW(KEY_FILE, FILE_ATTRIBUTE_HIDDEN)

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
        self._group_manager = None
        self._ensure_config_exists()

    def _default_config(self) -> dict:
        """초기 실행 시 보여줄 더미 데이터"""
        return {
            "tunnels": [
                {
                    "id": str(uuid.uuid4()),
                    "name": "테스트 서버 (예시)",
                    "bastion_host": "1.2.3.4",
                    "bastion_port": 22,
                    "bastion_user": "ec2-user",
                    "bastion_key": "", # 키 파일 경로 비어있음
                    "remote_host": "rds-endpoint.amazonaws.com",
                    "remote_port": DEFAULT_MYSQL_PORT,
                    "db_engine": "mysql",
                    "local_port": 3308
                }
            ]
        }

    def _ensure_config_exists(self):
        """설정 폴더와 파일이 없으면 기본값을 생성합니다."""
        with _CONFIG_LOCK:
            if not os.path.exists(APP_DIR):
                os.makedirs(APP_DIR)

            if not os.path.exists(CONFIG_FILE):
                # 첫 실행이므로 백업 없이 원자적으로 생성
                self._write_config_atomic_unlocked(self._default_config())

    def _file_revision(self, path: str) -> Optional[Tuple[int, int]]:
        """파일의 (수정시각_ns, 크기)를 반환. 파일이 없으면 None."""
        try:
            st = os.stat(path)
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _is_config_payload_valid(self, data) -> bool:
        """설정 데이터의 최소 구조 검증 (dict, tunnels는 필수 list, settings는 있으면 dict)"""
        if not isinstance(data, dict):
            return False
        if not isinstance(data.get('tunnels'), list):
            return False
        if 'settings' in data and not isinstance(data['settings'], dict):
            return False
        return True

    def _read_json_file(self, path: str) -> dict:
        """UTF-8 JSON 파일을 읽어 파싱한다."""
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _write_config_atomic_unlocked(self, data: dict):
        """임시 파일에 쓴 뒤 os.replace로 원자적으로 교체한다.

        호출자가 _CONFIG_LOCK을 보유하고 있어야 한다.
        """
        tmp_path = f"{CONFIG_FILE}.tmp.{os.getpid()}.{threading.get_ident()}"
        try:
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=4, ensure_ascii=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, CONFIG_FILE)
        except Exception:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        logger.debug(f"설정 저장 완료: {CONFIG_FILE}")

    def _restore_newest_valid_backup_unlocked(self, error: Exception) -> "_ConfigSnapshot":
        """가장 최신 백업부터 검사해 첫 유효한 백업으로 CONFIG_FILE을 복구한다.

        손상된 백업은 삭제하지 않고 건너뛴다. 유효한 백업이 하나도 없으면
        ConfigLoadError를 발생시킨다. 호출자가 _CONFIG_LOCK을 보유하고 있어야 한다.
        """
        for filename, _, _ in self._get_backup_files():
            backup_path = os.path.join(BACKUP_DIR, filename)
            try:
                data = self._read_json_file(backup_path)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue

            if not self._is_config_payload_valid(data):
                continue

            data = _without_reporting_privacy_state(data)
            self._write_config_atomic_unlocked(data)
            logger.warning(f"손상된 설정 파일을 백업에서 복원했습니다: {filename}")
            revision = self._file_revision(CONFIG_FILE)
            return _ConfigSnapshot(data, revision, copy.deepcopy(data))

        raise ConfigLoadError("설정 파일이 손상되었고 복원 가능한 백업이 없습니다.") from error

    def _merge_setting_values(self, original_settings: dict, new_settings: dict, current_settings: dict) -> dict:
        """settings의 개별 키 단위 병합.

        스테일 스냅샷에서 실제로 바뀐 설정 키만 최신 settings 위에 덮어써서,
        그 사이 동시에 저장된 다른 설정 키(예: 스케줄러의 schedules)를 보존한다.
        """
        merged_settings = dict(current_settings)
        for setting_key, setting_value in new_settings.items():
            old_value = original_settings.get(setting_key, _MISSING)
            if old_value is _MISSING or old_value != setting_value:
                merged_settings[setting_key] = copy.deepcopy(setting_value)
        return merged_settings

    def _merge_snapshot_changes(self, snapshot: "_ConfigSnapshot", current_data: dict) -> dict:
        """스테일 스냅샷을 저장할 때, 최신 디스크 데이터 위에 스냅샷에서
        실제로 변경된 최상위 키만 반영한다."""
        original = snapshot._original_payload
        merged = copy.deepcopy(current_data)

        for key, new_value in snapshot.items():
            if key == 'settings':
                merged['settings'] = self._merge_setting_values(
                    original.get('settings') or {},
                    new_value or {},
                    merged.get('settings') or {},
                )
                continue

            old_value = original.get(key, _MISSING)
            if old_value is _MISSING or old_value != new_value:
                merged[key] = copy.deepcopy(new_value)

        return merged

    def _save_config_unlocked(self, data, *, exclude_backup_paths: Optional[set] = None):
        """백업 생성 후 (스테일 스냅샷이면 병합하여) 원자적으로 기록한다.

        호출자가 _CONFIG_LOCK을 보유하고 있어야 한다.
        """
        self._create_backup(exclude_paths=exclude_backup_paths)

        payload = data
        if isinstance(data, _ConfigSnapshot):
            current_revision = self._file_revision(CONFIG_FILE)
            if current_revision == data._source_revision:
                payload = dict(data)
            else:
                try:
                    current_data = self._read_json_file(CONFIG_FILE)
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    current_data = None

                if current_data is not None and self._is_config_payload_valid(current_data):
                    payload = self._merge_snapshot_changes(data, current_data)
                else:
                    payload = dict(data)

        self._write_config_atomic_unlocked(payload)

    def load_config(self):
        """설정 파일을 읽어서 반환합니다.

        파일이 손상됐거나(JSON/인코딩 오류, 구조 불일치) 읽을 수 없으면
        가장 최신 유효 백업으로 자동 복원한다. 복원 가능한 백업이 없으면
        ConfigLoadError를 발생시킨다 (조용히 빈 설정을 반환하지 않는다).
        """
        with _CONFIG_LOCK:
            try:
                data = self._read_json_file(CONFIG_FILE)
            except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
                logger.error(f"설정 로드 오류: {e}")
                return self._restore_newest_valid_backup_unlocked(e)

            if not self._is_config_payload_valid(data):
                error = ConfigLoadError("설정 파일 구조가 올바르지 않습니다. (tunnels 필드 누락/잘못된 타입)")
                logger.error(str(error))
                return self._restore_newest_valid_backup_unlocked(error)

            revision = self._file_revision(CONFIG_FILE)
            return _ConfigSnapshot(data, revision, copy.deepcopy(data))

    def save_config(self, data):
        """설정 데이터를 파일에 저장합니다."""
        with _CONFIG_LOCK:
            self._save_config_unlocked(data)

    def _mutate_config(self, mutator):
        """config를 로드 -> mutator로 변경 -> 필요 시에만 저장하는 공통 헬퍼.

        mutator는 config(dict)를 받아 (should_save: bool, result) 튜플을 반환하는
        콜러블이다. should_save가 False면 save_config를 호출하지 않는다 (조기 실패
        반환 시 백업 로테이션/리비전 갱신이 일어나지 않던 기존 동작을 보존하기 위함).
        """
        with _CONFIG_LOCK:
            config = self.load_config()
            should_save, result = mutator(config)
            if should_save:
                self.save_config(config)
            return result

    def _create_backup(self, exclude_paths: Optional[set] = None):
        """설정 변경 전 자동 백업.

        현재 CONFIG_FILE이 손상되어 있으면(파싱 실패/구조 불일치) 백업하지 않는다.
        호출자가 _CONFIG_LOCK을 보유하고 있어야 한다.
        """
        if not os.path.exists(CONFIG_FILE):
            return

        try:
            current_data = self._read_json_file(CONFIG_FILE)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            logger.warning(f"손상된 설정 파일은 백업하지 않습니다: {e}")
            return

        if not self._is_config_payload_valid(current_data):
            logger.warning("설정 파일 구조가 올바르지 않아 백업하지 않습니다.")
            return

        try:
            os.makedirs(BACKUP_DIR, exist_ok=True)
            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
            backup_path = os.path.join(BACKUP_DIR, f'config.backup.{timestamp}.json')
            backup_data = _without_reporting_privacy_state(current_data)
            with open(backup_path, 'w', encoding='utf-8') as backup_file:
                json.dump(backup_data, backup_file, indent=4, ensure_ascii=False)
            logger.debug(f"설정 백업 생성: {backup_path}")
            self._cleanup_old_backups(exclude_paths=exclude_paths)
        except Exception as e:
            logger.warning(f"백업 생성 실패: {e}")

    def _cleanup_old_backups(self, exclude_paths: Optional[set] = None):
        """오래된 백업 파일 정리 (MAX_BACKUPS 초과 시 삭제)

        exclude_paths에 포함된 백업(예: 복원 대상)은 순번과 무관하게 삭제하지 않는다.
        """
        exclude_paths = exclude_paths or set()
        try:
            if not os.path.exists(BACKUP_DIR):
                return

            backups = self._get_backup_files()
            if len(backups) <= MAX_BACKUPS:
                return

            kept = 0
            for filename, _, _ in backups:
                if kept < MAX_BACKUPS:
                    kept += 1
                    continue

                backup_path = os.path.join(BACKUP_DIR, filename)
                if backup_path in exclude_paths or filename in exclude_paths:
                    continue

                os.remove(backup_path)
                logger.debug(f"오래된 백업 삭제: {filename}")
        except Exception as e:
            logger.warning(f"백업 정리 실패: {e}")

    def _get_backup_files(self) -> List[Tuple[str, str, int]]:
        """백업 파일 목록 반환 (최신순 정렬)"""
        if not os.path.exists(BACKUP_DIR):
            return []

        parsed = []
        prefix, suffix = 'config.backup.', '.json'
        for filename in os.listdir(BACKUP_DIR):
            if not (filename.startswith(prefix) and filename.endswith(suffix)):
                continue

            filepath = os.path.join(BACKUP_DIR, filename)
            timestamp_str = filename[len(prefix):-len(suffix)]

            dt = None
            for fmt in ('%Y%m%d_%H%M%S_%f', '%Y%m%d_%H%M%S'):
                try:
                    dt = datetime.strptime(timestamp_str, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                continue

            try:
                size = os.path.getsize(filepath)
            except OSError:
                continue

            display_time = dt.strftime('%Y-%m-%d %H:%M:%S')
            parsed.append((dt, filename, display_time, size))

        # 최신순 정렬 (파싱된 timestamp 기준, 동률이면 파일명 기준)
        parsed.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [(filename, display_time, size) for _, filename, display_time, size in parsed]

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

        with _CONFIG_LOCK:
            # 백업 회전으로 복원 대상이 삭제되지 않도록, 먼저 내용을 읽고 검증한다.
            try:
                restore_data = self._read_json_file(backup_path)
            except json.JSONDecodeError:
                return False, "백업 파일이 손상되었습니다."
            except Exception as e:
                logger.error(f"설정 복원 실패: {e}")
                return False, f"복원 중 오류 발생: {e}"

            if not self._is_config_payload_valid(restore_data):
                return False, "백업 파일이 손상되었습니다."

            try:
                try:
                    current_data = self._read_json_file(CONFIG_FILE)
                except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                    current_data = None
                if self._is_config_payload_valid(current_data):
                    restore_data = _with_local_reporting_privacy_state(
                        restore_data,
                        current_data,
                    )
                else:
                    restore_data = _without_reporting_privacy_state(
                        restore_data
                    )
                # 복원 전 현재 설정을 백업하되, 복원 대상 백업은 회전에서 보호한다.
                self._create_backup(exclude_paths={backup_path})
                self._write_config_atomic_unlocked(restore_data)
                logger.info(f"설정 복원 완료: {filename}")
                return True, f"설정이 복원되었습니다: {filename}"
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

        if not export_path:
            return False, "내보내기 경로가 비어 있습니다."

        normalized_export = os.path.abspath(export_path)
        export_dir = os.path.dirname(normalized_export)

        if export_dir and not os.path.isdir(export_dir):
            return False, f"내보낼 폴더를 찾을 수 없습니다: {export_dir}"

        if normalized_export == os.path.abspath(CONFIG_FILE):
            return False, "현재 설정 파일과 동일한 경로로는 내보낼 수 없습니다."

        try:
            with _CONFIG_LOCK:
                export_data = _without_reporting_privacy_state(
                    self._read_json_file(CONFIG_FILE)
                )
            with open(normalized_export, 'w', encoding='utf-8') as export_file:
                json.dump(export_data, export_file, indent=4, ensure_ascii=False)
            logger.info(f"설정 내보내기 완료: {normalized_export}")
            return True, f"설정이 내보내기되었습니다: {normalized_export}"
        except Exception as e:
            logger.error(f"설정 내보내기 실패: {e}")
            return False, f"내보내기 중 오류 발생: {e}"

    def _validate_port(self, value, field_name: str) -> Tuple[bool, str]:
        """포트 유효성 검증"""
        if not isinstance(value, int):
            return False, f"{field_name}는 숫자여야 합니다."

        if not 1 <= value <= 65535:
            return False, f"{field_name}는 1~65535 범위여야 합니다."

        return True, ""

    def _validate_import_data(self, import_data) -> Tuple[bool, str]:
        """가져올 설정 데이터 구조 검증"""
        if not isinstance(import_data, dict):
            return False, "유효하지 않은 설정 파일입니다. (JSON 객체 필요)"

        tunnels = import_data.get('tunnels')
        if tunnels is None:
            return False, "유효하지 않은 설정 파일입니다. (tunnels 필드 누락)"

        if not isinstance(tunnels, list):
            return False, "유효하지 않은 설정 파일입니다. (tunnels는 배열이어야 함)"

        seen_ids = set()
        for idx, tunnel in enumerate(tunnels, start=1):
            if not isinstance(tunnel, dict):
                return False, f"유효하지 않은 터널 데이터입니다. ({idx}번째 항목)"

            tunnel_id = tunnel.get('id')
            if not tunnel_id or not isinstance(tunnel_id, str):
                return False, f"터널 ID가 올바르지 않습니다. ({idx}번째 항목)"

            if tunnel_id in seen_ids:
                return False, f"중복된 터널 ID가 있습니다: {tunnel_id}"
            seen_ids.add(tunnel_id)

            required_fields = ['name', 'remote_host', 'remote_port']
            missing_fields = [f for f in required_fields if not tunnel.get(f)]
            if missing_fields:
                missing = ', '.join(missing_fields)
                return False, f"필수 필드가 누락되었습니다. ({idx}번째 항목: {missing})"

            valid, msg = self._validate_port(tunnel.get('remote_port'), 'remote_port')
            if not valid:
                return False, f"{msg} ({idx}번째 항목)"

            if 'local_port' in tunnel and tunnel.get('local_port') not in (None, ''):
                valid, msg = self._validate_port(tunnel.get('local_port'), 'local_port')
                if not valid:
                    return False, f"{msg} ({idx}번째 항목)"

            db_engine = tunnel.get('db_engine')
            if db_engine not in (None, '', 'mysql', 'postgresql'):
                return False, f"db_engine은 mysql 또는 postgresql이어야 합니다. ({idx}번째 항목)"

        settings = import_data.get('settings')
        if settings is not None and not isinstance(settings, dict):
            return False, "유효하지 않은 설정 파일입니다. (settings는 객체여야 함)"

        return True, ""

    def _validate_import_structure(self, import_data) -> Tuple[bool, str]:
        """Reject deeply nested or oversized collections before copying imports."""
        pending = [(import_data, 0)]
        while pending:
            current, depth = pending.pop()
            if depth > MAX_IMPORT_CONFIG_DEPTH:
                return False, "유효하지 않은 설정 파일입니다. (중첩이 너무 깊습니다)"
            if isinstance(current, dict):
                if len(current) > MAX_IMPORT_CONFIG_COLLECTION_ITEMS:
                    return False, "유효하지 않은 설정 파일입니다. (항목이 너무 많습니다)"
                pending.extend((item, depth + 1) for item in current.values())
            elif isinstance(current, list):
                if len(current) > MAX_IMPORT_CONFIG_COLLECTION_ITEMS:
                    return False, "유효하지 않은 설정 파일입니다. (항목이 너무 많습니다)"
                pending.extend((item, depth + 1) for item in current)
        return True, ""

    def import_config(self, import_path: str) -> Tuple[bool, str]:
        """외부 파일에서 설정 가져오기

        Args:
            import_path: 가져올 파일 경로

        Returns:
            (success, message) 튜플
        """
        try:
            with open(import_path, 'rb') as f:
                encoded_config = f.read(MAX_IMPORT_CONFIG_BYTES + 1)
            if len(encoded_config) > MAX_IMPORT_CONFIG_BYTES:
                return False, "설정 파일 크기가 허용 한도를 초과했습니다."
            import_data = json.loads(encoded_config.decode('utf-8'))

            is_valid, validation_msg = self._validate_import_structure(import_data)
            if not is_valid:
                return False, validation_msg
            is_valid, validation_msg = self._validate_import_data(import_data)
            if not is_valid:
                return False, validation_msg
            # 현재 설정 백업 + 원자적 교체 (shutil.copy2로 in-place 교체하지 않음)
            with _CONFIG_LOCK:
                current_data = self.load_config()
                import_data = _with_local_reporting_privacy_state(
                    import_data,
                    current_data,
                )
                self._create_backup()
                self._write_config_atomic_unlocked(import_data)

            logger.info(f"설정 가져오기 완료: {import_path}")
            return True, f"설정이 가져오기되었습니다: {import_path}"

        except FileNotFoundError:
            return False, f"파일을 찾을 수 없습니다: {import_path}"
        except IsADirectoryError:
            return False, f"파일이 아닙니다: {import_path}"
        except PermissionError:
            if os.path.isdir(import_path):
                return False, f"파일이 아닙니다: {import_path}"
            return False, "설정 파일을 읽을 권한이 없습니다."
        except json.JSONDecodeError:
            return False, "유효하지 않은 JSON 파일입니다."
        except UnicodeDecodeError:
            return False, "UTF-8 인코딩의 JSON 파일만 가져올 수 있습니다."
        except Exception as e:
            logger.error(f"설정 가져오기 실패: {e}")
            return False, f"가져오기 중 오류 발생: {e}"

    def get_backup_dir(self) -> str:
        """백업 디렉토리 경로 반환"""
        return BACKUP_DIR

    def get_config_path(self):
        return CONFIG_FILE

    # 네트워크 타임아웃 기본값
    DEFAULT_NETWORK_TIMEOUT_CHECK = 5     # 업데이트 확인 타임아웃 (초)
    DEFAULT_NETWORK_TIMEOUT_DOWNLOAD = 10  # 파일 다운로드 API 타임아웃 (초)

    def get_app_setting(self, key, default=None):
        """앱 설정 값 조회"""
        config = self.load_config()
        return config.get('settings', {}).get(key, default)

    def get_app_settings_snapshot(self) -> dict:
        """일관된 시점의 앱 설정 복사본을 반환한다."""
        config = self.load_config()
        return copy.deepcopy(config.get('settings', {}))

    def get_network_timeout_check(self) -> int:
        """업데이트 확인 네트워크 타임아웃 반환 (초)"""
        value = self.get_app_setting('network_timeout_check', self.DEFAULT_NETWORK_TIMEOUT_CHECK)
        try:
            return int(value)
        except (TypeError, ValueError):
            return self.DEFAULT_NETWORK_TIMEOUT_CHECK

    def get_network_timeout_download(self) -> int:
        """파일 다운로드 API 네트워크 타임아웃 반환 (초)"""
        value = self.get_app_setting('network_timeout_download', self.DEFAULT_NETWORK_TIMEOUT_DOWNLOAD)
        try:
            return int(value)
        except (TypeError, ValueError):
            return self.DEFAULT_NETWORK_TIMEOUT_DOWNLOAD

    def set_app_setting(self, key, value):
        """앱 설정 값 저장 (기존 설정 유지)"""
        def mutator(config):
            if 'settings' not in config:
                config['settings'] = {}
            config['settings'][key] = value
            return True, None

        self._mutate_config(mutator)

    def set_app_settings(self, updates: Mapping[str, object]) -> None:
        """여러 앱 설정 값을 하나의 원자적 config 변경으로 저장한다."""
        safe_updates = dict(updates)

        def mutator(settings):
            settings.update(safe_updates)
            return True, None

        self.mutate_app_settings(mutator)

    def mutate_app_settings(
        self,
        mutator: Callable[
            [Dict[str, object]],
            Tuple[bool, _AppSettingsMutationResult],
        ],
    ) -> _AppSettingsMutationResult:
        """앱 설정의 읽기, 판단, 변경, 저장을 한 config 트랜잭션으로 수행한다.

        mutator는 분리된 settings 복사본을 받아 (should_save, result)를 반환한다.
        should_save가 False면 복사본의 변경은 폐기한다.
        """
        def config_mutator(config):
            settings = copy.deepcopy(config.get('settings', {}))
            should_save, result = mutator(settings)
            if type(should_save) is not bool:
                raise TypeError('should_save must be a bool')
            if should_save:
                config['settings'] = copy.deepcopy(settings)
            return should_save, result

        return self._mutate_config(config_mutator)

    @property
    def encryptor(self):
        """CredentialEncryptor 인스턴스 (lazy initialization)"""
        if self._encryptor is None:
            self._encryptor = CredentialEncryptor()
        return self._encryptor

    @property
    def group_manager(self) -> TunnelGroupManager:
        """TunnelGroupManager 인스턴스 (lazy initialization)"""
        if self._group_manager is None:
            self._group_manager = TunnelGroupManager(self)
        return self._group_manager

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
        def mutator(config):
            config['last_active_tunnels'] = tunnel_ids
            return True, None

        self._mutate_config(mutator)
        logger.info(f"활성 터널 상태 저장: {len(tunnel_ids)}개")

    def get_last_active_tunnels(self) -> list:
        """마지막으로 활성화되어 있던 터널 ID 목록 반환"""
        config = self.load_config()
        return config.get('last_active_tunnels', [])

    # =========================================================================
    # 그룹 관리 메서드
    # =========================================================================

    def get_groups(self) -> List[dict]:
        """모든 그룹 목록 반환 (TunnelGroupManager 위임)

        Returns:
            그룹 목록: [{"id", "name", "color", "collapsed", "tunnel_ids"}, ...]
        """
        return self.group_manager.get_groups()

    def add_group(self, name: str, color: str = "#3498db") -> Tuple[bool, str, Optional[str]]:
        """새 그룹 생성 (TunnelGroupManager 위임)

        Args:
            name: 그룹 이름
            color: 그룹 색상 (hex 코드)

        Returns:
            (success, message, group_id) 튜플
        """
        return self.group_manager.add_group(name, color)

    def update_group(self, group_id: str, data: dict) -> Tuple[bool, str]:
        """그룹 정보 수정 (TunnelGroupManager 위임)

        Args:
            group_id: 수정할 그룹 ID
            data: 수정할 필드들 {"name", "color", "collapsed"}

        Returns:
            (success, message) 튜플
        """
        return self.group_manager.update_group(group_id, data)

    def delete_group(self, group_id: str) -> Tuple[bool, str]:
        """그룹 삭제 (TunnelGroupManager 위임, 터널은 그룹 없음으로 이동)

        Args:
            group_id: 삭제할 그룹 ID

        Returns:
            (success, message) 튜플
        """
        return self.group_manager.delete_group(group_id)

    def move_tunnel_to_group(self, tunnel_id: str, group_id: Optional[str]) -> Tuple[bool, str]:
        """터널을 그룹으로 이동 (TunnelGroupManager 위임)

        Args:
            tunnel_id: 이동할 터널 ID
            group_id: 대상 그룹 ID (None이면 그룹 없음으로 이동)

        Returns:
            (success, message) 튜플
        """
        return self.group_manager.move_tunnel_to_group(tunnel_id, group_id)

    def get_tunnel_group(self, tunnel_id: str) -> Optional[str]:
        """터널이 속한 그룹 ID 반환 (TunnelGroupManager 위임)

        Args:
            tunnel_id: 터널 ID

        Returns:
            그룹 ID (그룹 없으면 None)
        """
        return self.group_manager.get_tunnel_group(tunnel_id)

    def save_group_collapsed_state(self, group_id: str, collapsed: bool) -> bool:
        """그룹 접기/펼치기 상태 저장 (TunnelGroupManager 위임)

        Args:
            group_id: 그룹 ID
            collapsed: 접힘 상태

        Returns:
            성공 여부
        """
        return self.group_manager.save_group_collapsed_state(group_id, collapsed)
