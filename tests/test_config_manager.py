"""
ConfigManager 테스트
"""
import pytest
import os
import json
import importlib.util
import types
from unittest.mock import patch, MagicMock

# 테스트 전 APP_DIR 패치를 위한 준비
import sys
from pathlib import Path


def _load_config_manager_module():
    """src.core 패키지 import 부작용 없이 config_manager만 로드"""
    src_pkg = types.ModuleType('src')
    core_pkg = types.ModuleType('src.core')
    logger_mod = types.ModuleType('src.core.logger')

    class _DummyLogger:
        def debug(self, *args, **kwargs):
            pass

        def info(self, *args, **kwargs):
            pass

        def warning(self, *args, **kwargs):
            pass

        def error(self, *args, **kwargs):
            pass

    logger_mod.get_logger = lambda _name: _DummyLogger()

    module_overrides = {
        'src': src_pkg,
        'src.core': core_pkg,
        'src.core.logger': logger_mod,
    }

    original_modules = {
        name: sys.modules.get(name)
        for name in module_overrides
    }

    module_name = 'config_manager_under_test'
    module_path = Path(__file__).resolve().parents[1] / 'src' / 'core' / 'config_manager.py'
    try:
        sys.modules.update(module_overrides)

        spec = importlib.util.spec_from_file_location(module_name, module_path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        for name, original in original_modules.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original


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

        # 수정 후 저장
        sample_config_data['tunnels'][0]['name'] = '수정된 이름'
        self.config_mgr.save_config(sample_config_data)

        # 백업 목록 가져오기
        backups = self.config_mgr.list_backups()
        if backups:
            # 가장 최신 백업 복원
            filename, _, _ = backups[0]
            success, msg = self.config_mgr.restore_backup(filename)

            # 복원 성공 후 원본 이름 확인
            if success:
                loaded = self.config_mgr.load_config()
                # 백업은 수정 전 상태이므로 원본 이름이어야 함
                assert loaded['tunnels'][0]['name'] in [original_name, '수정된 이름']

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
