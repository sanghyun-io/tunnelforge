"""
GitHubAppAuth 테스트

Token 관리, JWT 생성, 캐시 갱신 로직을 검증합니다.
"""
import pytest
import time
from datetime import datetime, timedelta
from unittest.mock import patch, MagicMock, PropertyMock


# --- 모듈 로드 헬퍼 ---

def _make_auth(app_id="123", private_key="fake-pem", installation_id="456", repo="owner/repo"):
    """GitHubAppAuth 인스턴스 생성 헬퍼"""
    from src.core.github_app_auth import GitHubAppAuth
    return GitHubAppAuth(app_id, private_key, installation_id, repo)


# ============================================================
# check_available
# ============================================================

class TestCheckAvailable:
    """라이브러리 가용성 확인"""

    def test_available_when_both_installed(self):
        with patch('src.core.github_app_auth.HAS_JWT', True), \
             patch('src.core.github_app_auth.HAS_REQUESTS', True):
            from src.core.github_app_auth import GitHubAppAuth
            ok, msg = GitHubAppAuth.check_available()
            assert ok is True

    def test_unavailable_when_jwt_missing(self):
        with patch('src.core.github_app_auth.HAS_JWT', False), \
             patch('src.core.github_app_auth.HAS_REQUESTS', True):
            from src.core.github_app_auth import GitHubAppAuth
            ok, msg = GitHubAppAuth.check_available()
            assert ok is False
            assert 'PyJWT' in msg

    def test_unavailable_when_requests_missing(self):
        with patch('src.core.github_app_auth.HAS_JWT', True), \
             patch('src.core.github_app_auth.HAS_REQUESTS', False):
            from src.core.github_app_auth import GitHubAppAuth
            ok, msg = GitHubAppAuth.check_available()
            assert ok is False
            assert 'requests' in msg


# ============================================================
# JWT 생성
# ============================================================

class TestGenerateJWT:
    """JWT 토큰 생성 검증"""

    def test_jwt_payload_structure(self):
        auth = _make_auth()

        with patch('src.core.github_app_auth.HAS_JWT', True):
            mock_jwt = MagicMock()
            mock_jwt.encode.return_value = "fake-jwt-token"

            with patch('src.core.github_app_auth.jwt', mock_jwt):
                result = auth._generate_jwt()

            assert result == "fake-jwt-token"
            call_args = mock_jwt.encode.call_args
            payload = call_args[0][0]

            # iat은 현재 시간 - 60초
            assert 'iat' in payload
            assert 'exp' in payload
            assert 'iss' in payload
            assert payload['iss'] == "123"
            assert payload['exp'] - payload['iat'] == 10 * 60  # 10분 범위

    def test_jwt_raises_without_library(self):
        auth = _make_auth()

        with patch('src.core.github_app_auth.HAS_JWT', False):
            with pytest.raises(RuntimeError, match="PyJWT"):
                auth._generate_jwt()


# ============================================================
# Installation Token 발급 및 캐시
# ============================================================

class TestGetInstallationToken:
    """Installation Token 발급 및 캐시 동작 검증"""

    def _mock_token_response(self, token="inst-token-abc", expires_at="2099-12-31T23:59:59Z",
                              permissions=None):
        """토큰 응답 mock 생성"""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            'token': token,
            'expires_at': expires_at,
            'permissions': permissions or {'issues': 'write'}
        }
        return mock_response

    def test_first_call_fetches_token(self):
        auth = _make_auth()
        auth._cached_token = None
        auth._token_expires_at = None

        mock_response = self._mock_token_response()

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch('src.core.github_app_auth.requests') as mock_requests, \
             patch.object(auth, '_generate_jwt', return_value="fake-jwt"):
            mock_requests.post.return_value = mock_response

            token = auth.get_installation_token()

        assert token == "inst-token-abc"
        assert auth._cached_token == "inst-token-abc"

    def test_cached_token_returned_when_valid(self):
        auth = _make_auth()
        auth._cached_token = "cached-token"
        auth._token_expires_at = datetime.now() + timedelta(hours=1)

        with patch('src.core.github_app_auth.HAS_REQUESTS', True):
            # requests.post가 호출되지 않아야 함
            with patch('src.core.github_app_auth.requests') as mock_requests:
                token = auth.get_installation_token()

            assert token == "cached-token"
            mock_requests.post.assert_not_called()

    def test_expired_token_triggers_refresh(self):
        auth = _make_auth()
        auth._cached_token = "old-token"
        # 만료 4분 전 → 5분 임계값보다 적으므로 갱신 필요
        auth._token_expires_at = datetime.now() + timedelta(minutes=4)

        mock_response = self._mock_token_response(token="new-token")

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch('src.core.github_app_auth.requests') as mock_requests, \
             patch.object(auth, '_generate_jwt', return_value="fake-jwt"):
            mock_requests.post.return_value = mock_response

            token = auth.get_installation_token()

        assert token == "new-token"
        assert auth._cached_token == "new-token"

    def test_force_refresh_ignores_cache(self):
        auth = _make_auth()
        auth._cached_token = "still-valid-token"
        auth._token_expires_at = datetime.now() + timedelta(hours=1)

        mock_response = self._mock_token_response(token="force-refreshed")

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch('src.core.github_app_auth.requests') as mock_requests, \
             patch.object(auth, '_generate_jwt', return_value="fake-jwt"):
            mock_requests.post.return_value = mock_response

            token = auth.get_installation_token(force_refresh=True)

        assert token == "force-refreshed"

    def test_permissions_cached(self):
        auth = _make_auth()
        auth._cached_token = None
        auth._token_expires_at = None

        perms = {'issues': 'write', 'contents': 'read'}
        mock_response = self._mock_token_response(permissions=perms)

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch('src.core.github_app_auth.requests') as mock_requests, \
             patch.object(auth, '_generate_jwt', return_value="fake-jwt"):
            mock_requests.post.return_value = mock_response

            auth.get_installation_token()

        assert auth._cached_permissions == perms

    def test_returns_none_without_requests(self):
        auth = _make_auth()

        with patch('src.core.github_app_auth.HAS_REQUESTS', False):
            token = auth.get_installation_token()

        assert token is None

    def test_returns_none_on_api_error(self):
        auth = _make_auth()
        auth._cached_token = None
        auth._token_expires_at = None

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch('src.core.github_app_auth.requests') as mock_requests, \
             patch.object(auth, '_generate_jwt', return_value="fake-jwt"):
            mock_requests.post.side_effect = Exception("Network error")

            token = auth.get_installation_token()

        assert token is None


# ============================================================
# get_headers
# ============================================================

class TestGetHeaders:
    """API 요청 헤더 생성 검증"""

    def test_returns_headers_with_token(self):
        auth = _make_auth()

        with patch.object(auth, 'get_installation_token', return_value="test-token"):
            headers = auth.get_headers()

        assert headers['Authorization'] == 'token test-token'
        assert 'Accept' in headers
        assert headers['User-Agent'] == 'TunnelForge'

    def test_returns_empty_dict_when_no_token(self):
        auth = _make_auth()

        with patch.object(auth, 'get_installation_token', return_value=None):
            headers = auth.get_headers()

        assert headers == {}


# ============================================================
# test_connection
# ============================================================

class TestTestConnection:
    """연결 테스트 검증"""

    def test_success_with_write_permission(self):
        auth = _make_auth()
        auth._cached_permissions = {'issues': 'write'}

        mock_token_response = MagicMock()
        mock_repo_response = MagicMock()
        mock_repo_response.status_code = 200

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch.object(auth, 'get_installation_token', return_value="test-token"), \
             patch.object(auth, 'get_headers', return_value={'Authorization': 'token test-token'}), \
             patch('src.core.github_app_auth.requests') as mock_requests:
            mock_requests.get.return_value = mock_repo_response

            ok, msg = auth.test_connection()

        assert ok is True

    def test_failure_repo_not_found(self):
        auth = _make_auth()

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch.object(auth, 'get_installation_token', return_value="test-token"), \
             patch.object(auth, 'get_headers', return_value={'Authorization': 'token test-token'}), \
             patch('src.core.github_app_auth.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            ok, msg = auth.test_connection()

        assert ok is False
        assert '찾을 수 없습니다' in msg

    def test_failure_no_token(self):
        auth = _make_auth()

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch.object(auth, 'get_installation_token', return_value=None):
            ok, msg = auth.test_connection()

        assert ok is False
        assert 'Token 발급 실패' in msg

    def test_failure_no_issues_permission(self):
        auth = _make_auth()
        auth._cached_permissions = {'contents': 'read'}  # issues 권한 없음

        mock_response = MagicMock()
        mock_response.status_code = 200

        with patch('src.core.github_app_auth.HAS_REQUESTS', True), \
             patch.object(auth, 'get_installation_token', return_value="test-token"), \
             patch.object(auth, 'get_headers', return_value={'Authorization': 'token test-token'}), \
             patch('src.core.github_app_auth.requests') as mock_requests:
            mock_requests.get.return_value = mock_response

            ok, msg = auth.test_connection()

        assert ok is False
        assert '권한' in msg


# ============================================================
# 난독화 유틸리티
# ============================================================

class TestObfuscation:
    """난독화/해독 왕복 검증"""

    def test_roundtrip(self):
        from src.core.github_app_auth import GitHubAppAuth
        original = "test-secret-value-12345"
        obfuscated = GitHubAppAuth._obfuscate(original)
        assert obfuscated != original
        deobfuscated = GitHubAppAuth._deobfuscate(obfuscated)
        assert deobfuscated == original

    def test_deobfuscate_invalid_returns_empty(self):
        from src.core.github_app_auth import GitHubAppAuth
        result = GitHubAppAuth._deobfuscate("not-valid-base64!!!")
        assert result == ""

    def test_roundtrip_with_special_chars(self):
        from src.core.github_app_auth import GitHubAppAuth
        original = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        obfuscated = GitHubAppAuth._obfuscate(original)
        deobfuscated = GitHubAppAuth._deobfuscate(obfuscated)
        assert deobfuscated == original


# ============================================================
# from_env_or_embedded
# ============================================================

class TestFromEnvOrEmbedded:
    """환경변수/내장 값으로부터 인스턴스 생성"""

    def test_returns_none_when_no_config(self):
        from src.core.github_app_auth import GitHubAppAuth
        # 환경변수와 내장 값 모두 없는 경우
        GitHubAppAuth._env_loaded = False
        with patch.dict('os.environ', {}, clear=True), \
             patch.object(GitHubAppAuth, '_EMBEDDED_APP_ID', None), \
             patch.object(GitHubAppAuth, '_EMBEDDED_PRIVATE_KEY', None), \
             patch.object(GitHubAppAuth, '_EMBEDDED_INSTALLATION_ID', None), \
             patch.object(GitHubAppAuth, '_EMBEDDED_REPO', None), \
             patch('src.core.github_app_auth.HAS_DOTENV', False):
            result = GitHubAppAuth.from_env_or_embedded()

        assert result is None

    def test_creates_from_env_vars(self):
        from src.core.github_app_auth import GitHubAppAuth
        GitHubAppAuth._env_loaded = False

        env = {
            'GITHUB_APP_ID': '111',
            'GITHUB_APP_PRIVATE_KEY': '-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----',
            'GITHUB_APP_INSTALLATION_ID': '222',
            'GITHUB_REPO': 'test/repo',
        }

        with patch.dict('os.environ', env, clear=True), \
             patch('src.core.github_app_auth.HAS_DOTENV', False):
            result = GitHubAppAuth.from_env_or_embedded()

        assert result is not None
        assert result.app_id == '111'
        assert result.repo == 'test/repo'


# ============================================================
# 편의 함수
# ============================================================

class TestConvenienceFunctions:

    def test_get_github_app_auth_returns_instance_or_none(self):
        from src.core.github_app_auth import get_github_app_auth
        with patch('src.core.github_app_auth.GitHubAppAuth.from_env_or_embedded', return_value=None):
            result = get_github_app_auth()
        assert result is None

    def test_is_github_app_configured(self):
        from src.core.github_app_auth import is_github_app_configured
        with patch('src.core.github_app_auth.GitHubAppAuth.is_configured', return_value=False):
            assert is_github_app_configured() is False
