"""
scripts/github_app_secret_codec.py 단위 테스트

빌드타임 전용 난독화 코덱의 왕복(round-trip) 및 코드 생성 로직을 검증합니다.
"""
from scripts.github_app_secret_codec import (
    OBFUSCATION_KEY,
    obfuscate,
    deobfuscate,
    generate_embedded_code,
)


class TestObfuscationRoundtrip:
    """난독화/해독 왕복 검증"""

    def test_roundtrip(self):
        original = "test-secret-value-12345"
        obfuscated = obfuscate(original)
        assert obfuscated != original
        assert deobfuscate(obfuscated) == original

    def test_roundtrip_with_special_chars(self):
        original = "-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----"
        obfuscated = obfuscate(original)
        assert deobfuscate(obfuscated) == original

    def test_deobfuscate_invalid_returns_empty(self):
        assert deobfuscate("not-valid-base64!!!") == ""

    def test_key_matches_github_app_auth(self):
        """github_app_auth.GitHubAppAuth._OBFUSCATION_KEY와 동일한 키를 사용해야 런타임 복호화가 호환된다"""
        from src.core.github_app_auth import GitHubAppAuth
        assert OBFUSCATION_KEY == GitHubAppAuth._OBFUSCATION_KEY


class TestGenerateEmbeddedCode:
    """빌드 시 삽입할 코드 생성 검증"""

    def test_generates_all_four_fields(self):
        code = generate_embedded_code("123", "fake-pem", "456", "owner/repo")
        assert "_EMBEDDED_APP_ID" in code
        assert "_EMBEDDED_PRIVATE_KEY" in code
        assert "_EMBEDDED_INSTALLATION_ID" in code
        assert "_EMBEDDED_REPO" in code

    def test_generated_values_roundtrip(self):
        code = generate_embedded_code("123", "fake-pem", "456", "owner/repo")

        # 생성된 코드에서 난독화된 app_id 값을 추출해 왕복 검증
        obf_app_id = code.split('_EMBEDDED_APP_ID: Optional[str] = "')[1].split('"')[0]
        assert deobfuscate(obf_app_id) == "123"

    def test_compatible_with_github_app_auth_delegation(self):
        """GitHubAppAuth.generate_embedded_code가 위임한 결과와 동일해야 한다"""
        from src.core.github_app_auth import GitHubAppAuth
        direct = generate_embedded_code("123", "fake-pem", "456", "owner/repo")
        via_delegate = GitHubAppAuth.generate_embedded_code("123", "fake-pem", "456", "owner/repo")
        assert direct == via_delegate
