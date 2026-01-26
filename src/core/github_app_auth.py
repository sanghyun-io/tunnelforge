"""
GitHub App 인증 모듈

GitHub App을 사용한 인증은 Personal Access Token보다 안전합니다:
- 세밀한 권한 제어 (이슈 생성만 허용 가능)
- Installation Token은 1시간 후 자동 만료
- 앱 이름으로 활동 (봇임이 명확)

필요한 정보:
- App ID: GitHub App 설정 페이지에서 확인
- Private Key: App 설정에서 생성/다운로드 (.pem 파일)
- Installation ID: App 설치 후 URL에서 확인

환경변수:
- DATAFLARE_GITHUB_APP_ID: App ID
- DATAFLARE_GITHUB_APP_PRIVATE_KEY: Private Key (PEM 내용 또는 파일 경로)
- DATAFLARE_GITHUB_APP_INSTALLATION_ID: Installation ID
- DATAFLARE_GITHUB_REPO: 리포지토리 (owner/repo)
"""

import os
import time
from typing import Optional, Tuple
from datetime import datetime, timedelta

try:
    import jwt
    HAS_JWT = True
except ImportError:
    HAS_JWT = False

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False


class GitHubAppAuth:
    """GitHub App 인증 관리"""

    GITHUB_API_BASE = "https://api.github.com"

    # 환경변수 키
    ENV_APP_ID = "DATAFLARE_GITHUB_APP_ID"
    ENV_PRIVATE_KEY = "DATAFLARE_GITHUB_APP_PRIVATE_KEY"
    ENV_INSTALLATION_ID = "DATAFLARE_GITHUB_APP_INSTALLATION_ID"
    ENV_REPO = "DATAFLARE_GITHUB_REPO"

    # 내장 값 (빌드 시 설정)
    _EMBEDDED_APP_ID: Optional[str] = None
    _EMBEDDED_PRIVATE_KEY: Optional[str] = None  # 난독화된 PEM
    _EMBEDDED_INSTALLATION_ID: Optional[str] = None
    _EMBEDDED_REPO: Optional[str] = None

    # 난독화 키
    _OBFUSCATION_KEY = b"DataFlareTunnelGitHubApp2024"

    # 캐시된 Installation Token
    _cached_token: Optional[str] = None
    _token_expires_at: Optional[datetime] = None

    def __init__(self, app_id: str, private_key: str, installation_id: str, repo: str):
        """
        Args:
            app_id: GitHub App ID
            private_key: Private Key (PEM 형식 문자열)
            installation_id: Installation ID
            repo: 리포지토리 (owner/repo)
        """
        self.app_id = app_id
        self.private_key = private_key
        self.installation_id = installation_id
        self.repo = repo

    @classmethod
    def check_available(cls) -> Tuple[bool, str]:
        """필요한 라이브러리 확인"""
        if not HAS_JWT:
            return False, "PyJWT 라이브러리가 필요합니다. pip install PyJWT"
        if not HAS_REQUESTS:
            return False, "requests 라이브러리가 필요합니다. pip install requests"
        return True, "GitHub App 인증 사용 가능"

    @classmethod
    def from_env_or_embedded(cls) -> Optional['GitHubAppAuth']:
        """환경변수 또는 내장 값에서 인스턴스 생성"""
        # 환경변수 우선
        app_id = os.environ.get(cls.ENV_APP_ID) or cls._get_embedded_value('app_id')
        private_key = cls._get_private_key()
        installation_id = os.environ.get(cls.ENV_INSTALLATION_ID) or cls._get_embedded_value('installation_id')
        repo = os.environ.get(cls.ENV_REPO) or cls._get_embedded_value('repo')

        if all([app_id, private_key, installation_id, repo]):
            return cls(app_id, private_key, installation_id, repo)
        return None

    @classmethod
    def _get_private_key(cls) -> Optional[str]:
        """Private Key 조회 (환경변수 또는 내장)"""
        env_key = os.environ.get(cls.ENV_PRIVATE_KEY)

        if env_key:
            # 파일 경로인 경우
            if os.path.isfile(env_key):
                try:
                    with open(env_key, 'r') as f:
                        return f.read()
                except Exception:
                    return None
            # PEM 내용인 경우
            if env_key.startswith('-----BEGIN'):
                return env_key
            # Base64로 인코딩된 경우
            try:
                import base64
                decoded = base64.b64decode(env_key).decode('utf-8')
                if decoded.startswith('-----BEGIN'):
                    return decoded
            except Exception:
                pass

        # 내장 키 사용
        if cls._EMBEDDED_PRIVATE_KEY:
            return cls._deobfuscate(cls._EMBEDDED_PRIVATE_KEY)

        return None

    @classmethod
    def _get_embedded_value(cls, key: str) -> Optional[str]:
        """내장 값 조회 (난독화 해제)"""
        embedded_map = {
            'app_id': cls._EMBEDDED_APP_ID,
            'installation_id': cls._EMBEDDED_INSTALLATION_ID,
            'repo': cls._EMBEDDED_REPO
        }
        value = embedded_map.get(key)
        if value:
            return cls._deobfuscate(value)
        return None

    @classmethod
    def is_configured(cls) -> bool:
        """GitHub App 설정 여부 확인"""
        return cls.from_env_or_embedded() is not None

    def _generate_jwt(self) -> str:
        """JWT 생성 (App 인증용)"""
        if not HAS_JWT:
            raise RuntimeError("PyJWT 라이브러리가 필요합니다")

        now = int(time.time())
        payload = {
            'iat': now - 60,  # 시계 오차 대비 60초 전
            'exp': now + (10 * 60),  # 10분 후 만료
            'iss': self.app_id
        }

        return jwt.encode(payload, self.private_key, algorithm='RS256')

    def get_installation_token(self, force_refresh: bool = False) -> Optional[str]:
        """
        Installation Token 발급 (캐시 사용)

        Installation Token은 1시간 후 만료되므로,
        만료 5분 전에 자동으로 갱신합니다.
        """
        if not HAS_REQUESTS:
            return None

        # 캐시된 토큰이 유효한지 확인
        if not force_refresh and self._cached_token and self._token_expires_at:
            # 만료 5분 전까지는 캐시 사용
            if datetime.now() < self._token_expires_at - timedelta(minutes=5):
                return self._cached_token

        try:
            # JWT로 App 인증
            app_jwt = self._generate_jwt()

            # Installation Token 요청
            url = f"{self.GITHUB_API_BASE}/app/installations/{self.installation_id}/access_tokens"
            headers = {
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github.v3+json"
            }

            response = requests.post(url, headers=headers, timeout=30)
            response.raise_for_status()

            data = response.json()
            self._cached_token = data.get('token')

            # 만료 시간 파싱
            expires_at_str = data.get('expires_at')  # ISO 8601 형식
            if expires_at_str:
                # "2024-01-01T12:00:00Z" 형식
                self._token_expires_at = datetime.fromisoformat(
                    expires_at_str.replace('Z', '+00:00')
                ).replace(tzinfo=None)

            return self._cached_token

        except Exception as e:
            print(f"Installation Token 발급 실패: {e}")
            return None

    def get_headers(self) -> dict:
        """API 요청용 헤더 반환"""
        token = self.get_installation_token()
        if not token:
            return {}

        return {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3+json",
            "User-Agent": "DataFlare-Tunnel-Manager"
        }

    # === 난독화 유틸리티 ===

    @classmethod
    def _obfuscate(cls, plain_text: str) -> str:
        """문자열 난독화"""
        import base64
        key = cls._OBFUSCATION_KEY
        data = plain_text.encode('utf-8')
        obfuscated = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
        return base64.b64encode(obfuscated).decode('ascii')

    @classmethod
    def _deobfuscate(cls, obfuscated: str) -> str:
        """난독화 해제"""
        try:
            import base64
            key = cls._OBFUSCATION_KEY
            data = base64.b64decode(obfuscated.encode('ascii'))
            plain = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
            return plain.decode('utf-8')
        except Exception:
            return ""

    @classmethod
    def generate_embedded_code(cls, app_id: str, private_key: str,
                                installation_id: str, repo: str) -> str:
        """빌드 시 삽입할 코드 생성"""
        obf_app_id = cls._obfuscate(app_id)
        obf_private_key = cls._obfuscate(private_key)
        obf_installation_id = cls._obfuscate(installation_id)
        obf_repo = cls._obfuscate(repo)

        return f'''    # 빌드 시 자동 생성된 GitHub App 인증 정보
    _EMBEDDED_APP_ID: Optional[str] = "{obf_app_id}"
    _EMBEDDED_PRIVATE_KEY: Optional[str] = "{obf_private_key}"
    _EMBEDDED_INSTALLATION_ID: Optional[str] = "{obf_installation_id}"
    _EMBEDDED_REPO: Optional[str] = "{obf_repo}"
'''


def get_github_app_auth() -> Optional[GitHubAppAuth]:
    """GitHub App 인증 인스턴스 반환 (편의 함수)"""
    return GitHubAppAuth.from_env_or_embedded()


def is_github_app_configured() -> bool:
    """GitHub App 설정 여부 확인 (편의 함수)"""
    return GitHubAppAuth.is_configured()
