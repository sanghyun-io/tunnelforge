"""
봇 인증 정보 관리 모듈

우선순위:
1. 환경변수 (테스트/개발용)
2. 암호화된 내장 토큰 (.exe 배포용)

환경변수:
- DATAFLARE_GITHUB_BOT_TOKEN: GitHub Personal Access Token
- DATAFLARE_GITHUB_REPO: 리포지토리 (owner/repo)
"""

import os
import base64
from typing import Tuple, Optional


class BotCredentials:
    """봇 인증 정보 관리"""

    # 환경변수 키
    ENV_TOKEN = "DATAFLARE_GITHUB_BOT_TOKEN"
    ENV_REPO = "DATAFLARE_GITHUB_REPO"

    # 내장 토큰 (난독화됨) - 빌드 시 설정
    # 실제 배포 시 build script에서 이 값을 설정
    _EMBEDDED_TOKEN: Optional[str] = None
    _EMBEDDED_REPO: Optional[str] = None

    # 난독화 키 (간단한 XOR)
    _OBFUSCATION_KEY = b"DataFlareTunnel2024"

    @classmethod
    def get_credentials(cls) -> Tuple[Optional[str], Optional[str]]:
        """
        봇 인증 정보 반환

        Returns:
            (token, repo) 튜플. 설정되지 않은 경우 None
        """
        token = cls._get_token()
        repo = cls._get_repo()
        return token, repo

    @classmethod
    def _get_token(cls) -> Optional[str]:
        """토큰 조회 (환경변수 우선)"""
        # 1. 환경변수 체크
        env_token = os.environ.get(cls.ENV_TOKEN)
        if env_token:
            return env_token

        # 2. 내장 토큰 체크
        if cls._EMBEDDED_TOKEN:
            return cls._deobfuscate(cls._EMBEDDED_TOKEN)

        return None

    @classmethod
    def _get_repo(cls) -> Optional[str]:
        """리포지토리 조회 (환경변수 우선)"""
        # 1. 환경변수 체크
        env_repo = os.environ.get(cls.ENV_REPO)
        if env_repo:
            return env_repo

        # 2. 내장 리포 체크
        if cls._EMBEDDED_REPO:
            return cls._deobfuscate(cls._EMBEDDED_REPO)

        return None

    @classmethod
    def is_configured(cls) -> bool:
        """봇 인증 정보가 설정되어 있는지 확인"""
        token, repo = cls.get_credentials()
        return bool(token and repo)

    @classmethod
    def _obfuscate(cls, plain_text: str) -> str:
        """
        문자열 난독화 (빌드 스크립트용)

        간단한 XOR + Base64 인코딩
        완벽한 보안은 아니지만 casual inspection 방지
        """
        key = cls._OBFUSCATION_KEY
        data = plain_text.encode('utf-8')
        obfuscated = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
        return base64.b64encode(obfuscated).decode('ascii')

    @classmethod
    def _deobfuscate(cls, obfuscated: str) -> str:
        """난독화된 문자열 복원"""
        try:
            key = cls._OBFUSCATION_KEY
            data = base64.b64decode(obfuscated.encode('ascii'))
            plain = bytes(d ^ key[i % len(key)] for i, d in enumerate(data))
            return plain.decode('utf-8')
        except Exception:
            return ""

    @classmethod
    def set_embedded_credentials(cls, token: str, repo: str):
        """
        내장 인증 정보 설정 (빌드 스크립트에서 호출)

        사용 예:
        ```python
        from src.core.bot_credentials import BotCredentials
        BotCredentials.set_embedded_credentials("ghp_xxx", "owner/repo")
        ```
        """
        cls._EMBEDDED_TOKEN = cls._obfuscate(token)
        cls._EMBEDDED_REPO = cls._obfuscate(repo)

    @classmethod
    def generate_embedded_code(cls, token: str, repo: str) -> str:
        """
        빌드 시 삽입할 코드 생성

        Returns:
            bot_credentials.py에 삽입할 코드 문자열
        """
        obf_token = cls._obfuscate(token)
        obf_repo = cls._obfuscate(repo)

        return f'''    # 빌드 시 자동 생성된 내장 인증 정보
    _EMBEDDED_TOKEN: Optional[str] = "{obf_token}"
    _EMBEDDED_REPO: Optional[str] = "{obf_repo}"
'''


def get_bot_credentials() -> Tuple[Optional[str], Optional[str]]:
    """봇 인증 정보 조회 (편의 함수)"""
    return BotCredentials.get_credentials()


def is_bot_configured() -> bool:
    """봇이 설정되어 있는지 확인 (편의 함수)"""
    return BotCredentials.is_configured()
