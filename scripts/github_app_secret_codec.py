"""GitHub App 자격증명 난독화 코덱 (빌드타임 전용)

src/core/github_app_auth.py의 GitHubAppAuth._obfuscate/generate_embedded_code가
이 모듈에 위임한다. 런타임 경로인 GitHubAppAuth._deobfuscate는 패키징된 exe에
scripts/가 포함되지 않으므로 이 모듈을 참조하지 않고 github_app_auth.py 내부에
그대로 유지한다.

scripts/embed_github_credentials.py도 동일한 난독화 로직 사본을 갖고 있다
(빌드 파이프라인 독립성을 위한 의도적 중복 — 이 모듈로의 통합은 범위 밖).
"""
import base64

# github_app_auth.py의 GitHubAppAuth._OBFUSCATION_KEY와 동일해야 한다.
OBFUSCATION_KEY = b"TunnelForgeGitHubApp2024"


def obfuscate(plain_text: str) -> str:
    """문자열 난독화"""
    key = OBFUSCATION_KEY
    data = plain_text.encode('utf-8')
    obfuscated = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
    return base64.b64encode(obfuscated).decode('ascii')


def deobfuscate(obfuscated: str) -> str:
    """난독화 해제"""
    try:
        key = OBFUSCATION_KEY
        data = base64.b64decode(obfuscated.encode('ascii'))
        plain = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
        return plain.decode('utf-8')
    except Exception:
        return ""


def generate_embedded_code(app_id: str, private_key: str,
                            installation_id: str, repo: str) -> str:
    """빌드 시 GitHubAppAuth에 삽입할 코드 생성"""
    obf_app_id = obfuscate(app_id)
    obf_private_key = obfuscate(private_key)
    obf_installation_id = obfuscate(installation_id)
    obf_repo = obfuscate(repo)

    return f'''    # 빌드 시 자동 생성된 GitHub App 인증 정보
    _EMBEDDED_APP_ID: Optional[str] = "{obf_app_id}"
    _EMBEDDED_PRIVATE_KEY: Optional[str] = "{obf_private_key}"
    _EMBEDDED_INSTALLATION_ID: Optional[str] = "{obf_installation_id}"
    _EMBEDDED_REPO: Optional[str] = "{obf_repo}"
'''
