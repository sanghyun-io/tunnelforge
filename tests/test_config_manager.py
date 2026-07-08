"""
ConfigManager 테스트
"""
import pytest
import os
import json
import importlib
import threading
from unittest.mock import patch, MagicMock

# 테스트 전 APP_DIR 패치를 위한 준비
from pathlib import Path

# 실제 패키지로 import해둔다. 각 테스트는 patch.dict(os.environ, ...)를 적용한
# 뒤 이 모듈을 reload해서 모듈 레벨 경로 상수를 그 테스트의 임시 디렉토리
# 기준으로 다시 계산하게 만든다.
import src.core.config_manager as _config_manager_module


def _load_config_manager_module():
    """환경변수(LOCALAPPDATA/HOME) 패치가 적용된 상태에서 config_manager를 reload한다.

    실제 패키지(import src.core.config_manager)를 사용하므로 이 테스트 파일을
    단독으로 실행해도 config_manager 내부의 `from src.core.constants import ...`
    같은 서브모듈 임포트가 정상 동작한다. (예전에는 importlib.util로 가짜
    src/src.core 패키지를 sys.modules에 주입해 격리 로드했는데, 가짜 패키지에는
    __path__가 없어 단독 실행 시 constants 서브모듈을 찾지 못하고 실패했다 —
    전체 스위트에서 다른 테스트가 src.core.constants를 먼저 정상 import해
    캐시해둔 경우에만 우연히 통과했다.)

    reload는 APP_DIR/CONFIG_FILE/KEY_FILE/BACKUP_DIR 같은 모듈 레벨 경로 상수와
    _CONFIG_LOCK을 현재 patch.dict(os.environ, ...)가 적용된 상태 기준으로 다시
    계산해, 테스트마다 격리된 임시 디렉토리를 사용하게 한다.
    """
    return importlib.reload(_config_manager_module)


class TestCredentialEncryptor:
    """CredentialEncryptor 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """각 테스트 전 임시 디렉토리로 환경 설정"""
        self.test_dir = tmp_path / 'TunnelForge'
        self.test_dir.mkdir()

        # 환경 변수 패치 (OS별 설정 경로 분기 대응)
        self.env_patch = patch.dict(
            os.environ,
            {'LOCALAPPDATA': str(tmp_path), 'HOME': str(tmp_path)}
        )
        self.env_patch.start()

        config_module = _load_config_manager_module()
        Path(config_module.APP_DIR).mkdir(parents=True, exist_ok=True)
        self.encryptor = config_module.CredentialEncryptor()

    def teardown_method(self):
        self.env_patch.stop()

    def test_encrypt_decrypt_roundtrip(self):
        """암호화 후 복호화 시 원본과 동일해야 함"""
        original = "my_secure_password_123!"
        encrypted = self.encryptor.encrypt(original)
        decrypted = self.encryptor.decrypt(encrypted)

        assert encrypted != original  # 암호화가 됐는지
        assert decrypted == original  # 복호화 후 원본과 동일

    def test_encrypt_empty_string(self):
        """빈 문자열 암호화 테스트"""
        result = self.encryptor.encrypt("")
        assert result == ""

    def test_decrypt_empty_string(self):
        """빈 문자열 복호화 테스트"""
        result = self.encryptor.decrypt("")
        assert result == ""

    def test_decrypt_invalid_text(self):
        """잘못된 암호문 복호화 시 빈 문자열 반환"""
        result = self.encryptor.decrypt("invalid_encrypted_text")
        assert result == ""

    def test_encrypt_unicode(self):
        """유니코드 문자열 암호화/복호화 테스트"""
        original = "비밀번호_테스트_한글!@#"
        encrypted = self.encryptor.encrypt(original)
        decrypted = self.encryptor.decrypt(encrypted)

        assert decrypted == original


class TestConfigManager:
    """ConfigManager 클래스 테스트"""

    @pytest.fixture(autouse=True)
    def setup(self, tmp_path):
        """각 테스트 전 임시 디렉토리로 환경 설정"""
        self.test_dir = tmp_path / 'TunnelForge'
        self.test_dir.mkdir()

        # 환경 변수 패치 (OS별 설정 경로 분기 대응)
        self.env_patch = patch.dict(
            os.environ,
            {'LOCALAPPDATA': str(tmp_path), 'HOME': str(tmp_path)}
        )
        self.env_patch.start()

        config_module = _load_config_manager_module()
        self.config_module = config_module
        self.config_mgr = config_module.ConfigManager()

    def teardown_method(self):
        self.env_patch.stop()

    def test_load_config_default(self):
        """기본 설정 로드 테스트"""
        config = self.config_mgr.load_config()

        assert 'tunnels' in config
        assert isinstance(config['tunnels'], list)

    def test_save_config(self, sample_config_data):
        """설정 저장 테스트"""
        self.config_mgr.save_config(sample_config_data)

        # 다시 로드하여 확인
        loaded = self.config_mgr.load_config()
        assert loaded['tunnels'][0]['name'] == '테스트 서버 1'
        assert loaded['settings']['close_action'] == 'ask'

    def test_get_set_app_setting(self):
        """앱 설정 저장/조회 테스트"""
        # 설정 저장
        self.config_mgr.set_app_setting('test_key', 'test_value')
        self.config_mgr.set_app_setting('test_bool', True)
        self.config_mgr.set_app_setting('test_int', 42)

        # 설정 조회
        assert self.config_mgr.get_app_setting('test_key') == 'test_value'
        assert self.config_mgr.get_app_setting('test_bool') is True
        assert self.config_mgr.get_app_setting('test_int') == 42

    def test_get_app_setting_default(self):
        """존재하지 않는 설정 조회 시 기본값 반환"""
        result = self.config_mgr.get_app_setting('non_existent', 'default_value')
        assert result == 'default_value'

    def test_backup_creation(self, sample_config_data):
        """설정 저장 시 백업 생성 확인"""
        # 최초 저장
        self.config_mgr.save_config(sample_config_data)

        # 두 번째 저장 (백업이 생성되어야 함)
        sample_config_data['tunnels'][0]['name'] = '수정된 서버'
        self.config_mgr.save_config(sample_config_data)

        # 백업 목록 확인
        backups = self.config_mgr.list_backups()
        assert len(backups) >= 1

    def test_restore_backup(self, sample_config_data):
        """백업 복원 테스트"""
        # 원본 저장
        original_name = '원본 서버 이름'
        sample_config_data['tunnels'][0]['name'] = original_name
        self.config_mgr.save_config(sample_config_data)

        # 수정 후 저장 (이 시점의 백업이 원본 이름 상태를 담고 있음)
        sample_config_data['tunnels'][0]['name'] = '수정된 이름'
        self.config_mgr.save_config(sample_config_data)

        # 백업 목록 가져오기 (최신순 정렬 - 마이크로초 타임스탬프로 결정적)
        backups = self.config_mgr.list_backups()
        assert backups

        # 가장 최신 백업 복원 (직전 저장 시점에 만든 백업 = 원본 이름 상태)
        filename, _, _ = backups[0]
        success, msg = self.config_mgr.restore_backup(filename)

        assert success is True
        loaded = self.config_mgr.load_config()
        assert loaded['tunnels'][0]['name'] == original_name

    def test_save_config_uses_atomic_replace(self, sample_config_data):
        """save_config는 임시 파일 작성 후 os.replace로 원자적으로 교체해야 한다"""
        real_replace = self.config_module.os.replace
        with patch.object(self.config_module.os, 'replace', wraps=real_replace) as mock_replace:
            self.config_mgr.save_config(sample_config_data)

        assert mock_replace.called
        tmp_path, target_path = mock_replace.call_args[0]

        assert target_path == self.config_module.CONFIG_FILE
        config_basename = os.path.basename(self.config_module.CONFIG_FILE)
        assert os.path.basename(tmp_path).startswith(f"{config_basename}.tmp.")

        config_dir = os.path.dirname(self.config_module.CONFIG_FILE)
        leftover_tmp = [
            f for f in os.listdir(config_dir)
            if f.startswith(f"{config_basename}.tmp.")
        ]
        assert leftover_tmp == []

    def test_save_config_write_failure_preserves_existing_config(self, sample_config_data):
        """쓰기 도중 실패해도 기존 유효한 config.json은 그대로 보존돼야 한다"""
        self.config_mgr.save_config(sample_config_data)

        with open(self.config_module.CONFIG_FILE, 'r', encoding='utf-8') as f:
            original_content = f.read()

        def broken_dump(data, fp, **kwargs):
            fp.write('{"partial": true')
            raise RuntimeError("simulated write failure")

        with patch.object(self.config_module.json, 'dump', side_effect=broken_dump):
            with pytest.raises(RuntimeError):
                self.config_mgr.save_config(sample_config_data)

        with open(self.config_module.CONFIG_FILE, 'r', encoding='utf-8') as f:
            after_content = f.read()

        assert after_content == original_content
        assert json.loads(after_content) == sample_config_data

        # 임시 파일도 남아있으면 안 됨
        config_dir = os.path.dirname(self.config_module.CONFIG_FILE)
        config_basename = os.path.basename(self.config_module.CONFIG_FILE)
        leftover_tmp = [
            f for f in os.listdir(config_dir)
            if f.startswith(f"{config_basename}.tmp.")
        ]
        assert leftover_tmp == []

    def test_load_config_restores_newest_valid_backup_when_config_corrupt(self, sample_config_data):
        """설정 파일이 손상되면 가장 최신의 '유효한' 백업으로 복원해야 한다"""
        with open(self.config_module.CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write('{corrupt json')

        backup_dir = self.config_module.BACKUP_DIR
        os.makedirs(backup_dir, exist_ok=True)

        # 가장 최신: 손상된 백업 (건너뛰어야 함)
        corrupt_backup = os.path.join(backup_dir, 'config.backup.20260101_000003_000000.json')
        with open(corrupt_backup, 'w', encoding='utf-8') as f:
            f.write('{also corrupt')

        # 두 번째로 최신: 유효한 백업 (이게 선택되어야 함)
        valid_backup = os.path.join(backup_dir, 'config.backup.20260101_000002_000000.json')
        with open(valid_backup, 'w', encoding='utf-8') as f:
            json.dump(sample_config_data, f, ensure_ascii=False)

        # 가장 오래됨: 유효하지만 더 오래된 백업 (선택되면 안 됨)
        older_valid_backup = os.path.join(backup_dir, 'config.backup.20260101_000001_000000.json')
        with open(older_valid_backup, 'w', encoding='utf-8') as f:
            json.dump({'tunnels': []}, f, ensure_ascii=False)

        loaded = self.config_mgr.load_config()

        assert loaded['tunnels'] == sample_config_data['tunnels']
        assert loaded['tunnels'] != []

        with open(self.config_module.CONFIG_FILE, 'r', encoding='utf-8') as f:
            on_disk = json.load(f)
        assert on_disk == sample_config_data

    def test_load_config_raises_when_corrupt_and_no_valid_backup(self):
        """손상된 설정 파일이고 복원 가능한 백업도 없으면 ConfigLoadError를 발생시켜야 한다"""
        with open(self.config_module.CONFIG_FILE, 'w', encoding='utf-8') as f:
            f.write('{corrupt json')

        with pytest.raises(self.config_module.ConfigLoadError) as exc_info:
            self.config_mgr.load_config()

        assert "백업" in str(exc_info.value)

    def test_restore_backup_validates_target_before_backup_rotation(self):
        """복원 대상 백업은 백업 회전(cleanup)으로 삭제되면 안 된다"""
        max_backups = self.config_module.MAX_BACKUPS
        backup_dir = self.config_module.BACKUP_DIR
        os.makedirs(backup_dir, exist_ok=True)

        backup_payloads = []
        for i in range(max_backups):
            payload = {
                'tunnels': [{
                    'id': f'backup-{i}',
                    'name': f'백업 {i}',
                    'remote_host': 'db.example.com',
                    'remote_port': 3306,
                }]
            }
            backup_payloads.append(payload)
            timestamp = f"20260101_0000{i:02d}_000000"
            backup_path = os.path.join(backup_dir, f'config.backup.{timestamp}.json')
            with open(backup_path, 'w', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False)

        backups = self.config_mgr.list_backups()
        assert len(backups) == max_backups

        # 최신순 정렬이므로 마지막 원소가 가장 오래된 백업
        oldest_filename, _, _ = backups[-1]
        oldest_backup_path = os.path.join(backup_dir, oldest_filename)

        success, msg = self.config_mgr.restore_backup(oldest_filename)

        assert success is True
        assert os.path.exists(oldest_backup_path)

        loaded = self.config_mgr.load_config()
        assert loaded['tunnels'] == backup_payloads[0]['tunnels']

    def test_stale_snapshot_save_preserves_concurrent_settings_update(self, sample_config_data):
        """스테일 스냅샷을 저장해도 그 사이 동시에 저장된 다른 설정 키를 지우면 안 된다"""
        self.config_mgr.save_config(sample_config_data)

        snapshot_a = self.config_mgr.load_config()

        # 스냅샷 A를 들고 있는 동안, 다른 경로(스케줄러 등)에서 설정을 저장
        self.config_mgr.set_app_setting('schedules', [{'name': 'daily'}])

        # 스냅샷 A에는 스케줄 변경이 반영되지 않은 채로 tunnels만 수정
        new_tunnel = {
            'id': 'new-tunnel',
            'name': '새 터널',
            'remote_host': 'db3.example.com',
            'remote_port': 3306,
        }
        snapshot_a['tunnels'].append(new_tunnel)

        self.config_mgr.save_config(snapshot_a)

        final = self.config_mgr.load_config()
        tunnel_ids = [t['id'] for t in final['tunnels']]
        assert 'new-tunnel' in tunnel_ids
        assert final['settings']['schedules'] == [{'name': 'daily'}]

    def test_multiple_set_app_setting_calls_preserve_all_keys(self):
        """여러 스레드가 동시에 서로 다른 설정 키를 저장해도 전부 보존돼야 한다"""
        keys_values = [(f'key_{i}', i) for i in range(20)]

        threads = [
            threading.Thread(target=self.config_mgr.set_app_setting, args=(key, value))
            for key, value in keys_values
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final = self.config_mgr.load_config()
        for key, value in keys_values:
            assert final['settings'][key] == value

    def test_get_tunnel_credentials(self, sample_config_data):
        """터널 자격 증명 조회 테스트"""
        # 자격 증명이 없는 터널
        sample_config_data['tunnels'][0]['db_user'] = ''
        self.config_mgr.save_config(sample_config_data)

        user, password = self.config_mgr.get_tunnel_credentials('test-001')
        assert user == ''
        assert password == ''

    def test_save_active_tunnels(self):
        """활성 터널 저장 테스트"""
        active_ids = ['tunnel-1', 'tunnel-2', 'tunnel-3']
        self.config_mgr.save_active_tunnels(active_ids)

        result = self.config_mgr.get_last_active_tunnels()
        assert result == active_ids

    def test_get_last_active_tunnels_empty(self):
        """활성 터널 없는 경우 테스트"""
        result = self.config_mgr.get_last_active_tunnels()
        assert result == []

    def test_export_config_success(self, tmp_path, sample_config_data):
        """설정 내보내기 성공 테스트"""
        self.config_mgr.save_config(sample_config_data)
        export_file = tmp_path / 'exported_config.json'

        success, msg = self.config_mgr.export_config(str(export_file))

        assert success is True
        assert export_file.exists()
        assert "내보내기" in msg

        with open(export_file, 'r', encoding='utf-8') as f:
            exported_data = json.load(f)
        assert exported_data == sample_config_data

    def test_import_config_success(self, tmp_path, sample_config_data):
        """설정 가져오기 성공 테스트"""
        import_file = tmp_path / 'import_config.json'
        with open(import_file, 'w', encoding='utf-8') as f:
            json.dump(sample_config_data, f, ensure_ascii=False)

        success, msg = self.config_mgr.import_config(str(import_file))

        assert success is True
        assert "가져오기" in msg
        loaded = self.config_mgr.load_config()
        assert loaded == sample_config_data

    def test_import_config_missing_tunnels_field(self, tmp_path):
        """tunnels 필드 누락 시 실패 테스트"""
        invalid_file = tmp_path / 'invalid_config.json'
        with open(invalid_file, 'w', encoding='utf-8') as f:
            json.dump({'settings': {}}, f, ensure_ascii=False)

        success, msg = self.config_mgr.import_config(str(invalid_file))

        assert success is False
        assert "tunnels" in msg


    def test_export_config_missing_directory(self, tmp_path):
        """존재하지 않는 폴더로 내보내기 시 실패"""
        missing_dir = tmp_path / 'not_exists'
        export_file = missing_dir / 'export.json'

        success, msg = self.config_mgr.export_config(str(export_file))

        assert success is False
        assert "폴더" in msg

    def test_import_config_invalid_root_type(self, tmp_path):
        """JSON 루트가 객체가 아니면 실패"""
        invalid_file = tmp_path / 'invalid_root.json'
        with open(invalid_file, 'w', encoding='utf-8') as f:
            json.dump([{'id': '1'}], f, ensure_ascii=False)

        success, msg = self.config_mgr.import_config(str(invalid_file))

        assert success is False
        assert "JSON 객체" in msg

    def test_import_config_tunnels_not_list(self, tmp_path):
        """tunnels가 배열이 아니면 실패"""
        invalid_file = tmp_path / 'invalid_tunnels.json'
        with open(invalid_file, 'w', encoding='utf-8') as f:
            json.dump({'tunnels': {}}, f, ensure_ascii=False)

        success, msg = self.config_mgr.import_config(str(invalid_file))

        assert success is False
        assert "배열" in msg

    def test_import_config_duplicate_tunnel_id(self, tmp_path):
        """중복 터널 ID가 있으면 실패"""
        invalid_file = tmp_path / 'duplicate_id.json'
        dup_data = {
            'tunnels': [
                {
                    'id': 'dup-id',
                    'name': '서버1',
                    'remote_host': 'db1.example.com',
                    'remote_port': 3306,
                },
                {
                    'id': 'dup-id',
                    'name': '서버2',
                    'remote_host': 'db2.example.com',
                    'remote_port': 3307,
                }
            ]
        }
        with open(invalid_file, 'w', encoding='utf-8') as f:
            json.dump(dup_data, f, ensure_ascii=False)

        success, msg = self.config_mgr.import_config(str(invalid_file))

        assert success is False
        assert "중복된 터널 ID" in msg

    def test_import_config_invalid_port_range(self, tmp_path):
        """포트 범위가 유효하지 않으면 실패"""
        invalid_file = tmp_path / 'invalid_port.json'
        invalid_data = {
            'tunnels': [
                {
                    'id': 'test-1',
                    'name': '서버1',
                    'remote_host': 'db1.example.com',
                    'remote_port': 70000,
                }
            ]
        }
        with open(invalid_file, 'w', encoding='utf-8') as f:
            json.dump(invalid_data, f, ensure_ascii=False)

        success, msg = self.config_mgr.import_config(str(invalid_file))

        assert success is False
        assert "1~65535" in msg
    def test_import_config_invalid_json(self, tmp_path):
        """잘못된 JSON 파일 가져오기 실패 테스트"""
        broken_file = tmp_path / 'broken.json'
        broken_file.write_text('{invalid_json', encoding='utf-8')

        success, msg = self.config_mgr.import_config(str(broken_file))

        assert success is False
        assert "JSON" in msg
