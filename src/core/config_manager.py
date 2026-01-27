import json
import os
import uuid
from cryptography.fernet import Fernet

# 운영체제별 설정 파일 저장 경로 지정
# Windows: C:\Users\User\AppData\Local\TunnelDB
# Mac/Linux: ~/.config/tunneldb
if os.name == 'nt':
    APP_DIR = os.path.join(os.environ['LOCALAPPDATA'], 'TunnelDB')
else:
    APP_DIR = os.path.join(os.path.expanduser('~'), '.config', 'tunneldb')

CONFIG_FILE = os.path.join(APP_DIR, 'config.json')
KEY_FILE = os.path.join(APP_DIR, '.encryption_key')


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
            print(f"설정 로드 오류: {e}")
            return {"tunnels": []}

    def save_config(self, data):
        """설정 데이터를 파일에 저장합니다."""
        with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        print(f"설정 저장 완료: {CONFIG_FILE}")

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