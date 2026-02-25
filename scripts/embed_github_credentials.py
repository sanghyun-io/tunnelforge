"""
빌드 시 GitHub App 인증 정보를 소스 코드에 난독화 임베딩하는 스크립트

환경변수에서 인증 정보를 읽어 src/core/github_app_auth.py의
_EMBEDDED_* 변수를 난독화된 값으로 교체합니다.

사용법:
    python scripts/embed_github_credentials.py           # 실제 교체
    python scripts/embed_github_credentials.py --dry-run # 미리보기 (파일 수정 없음)

필요한 환경변수:
    GITHUB_APP_ID              - GitHub App ID
    GITHUB_APP_PRIVATE_KEY     - Private Key .pem 파일 경로
    GITHUB_APP_INSTALLATION_ID - Installation ID
    GITHUB_REPO                - 리포지토리 (owner/repo)
"""

import argparse
import base64
import io
import os
import re
import sys
from pathlib import Path

# Windows cp949 인코딩 문제 방지
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

# 난독화 키 (github_app_auth.py의 _OBFUSCATION_KEY와 동일)
OBFUSCATION_KEY = b"TunnelForgeGitHubApp2024"

# 교체 대상 파일
TARGET_FILE = Path(__file__).resolve().parent.parent / "src" / "core" / "github_app_auth.py"

# 교체 대상 변수 패턴
EMBEDDED_VARS = [
    "_EMBEDDED_APP_ID",
    "_EMBEDDED_PRIVATE_KEY",
    "_EMBEDDED_INSTALLATION_ID",
    "_EMBEDDED_REPO",
]


def obfuscate(plain_text: str) -> str:
    """문자열 난독화 (GitHubAppAuth._obfuscate와 동일한 로직)"""
    data = plain_text.encode("utf-8")
    obfuscated = bytes(d ^ OBFUSCATION_KEY[i % len(OBFUSCATION_KEY)] for i, d in enumerate(data))
    return base64.b64encode(obfuscated).decode("ascii")


def read_credentials() -> dict:
    """환경변수에서 인증 정보 읽기"""
    errors = []

    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    if not app_id:
        errors.append("GITHUB_APP_ID 환경변수가 설정되지 않았습니다")

    installation_id = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()
    if not installation_id:
        errors.append("GITHUB_APP_INSTALLATION_ID 환경변수가 설정되지 않았습니다")

    repo = os.environ.get("GITHUB_REPO", "").strip()
    if not repo:
        errors.append("GITHUB_REPO 환경변수가 설정되지 않았습니다")

    private_key = ""
    pem_path = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").strip()
    if not pem_path:
        errors.append("GITHUB_APP_PRIVATE_KEY 환경변수가 설정되지 않았습니다")
    elif not os.path.isfile(pem_path):
        errors.append(f"Private Key 파일을 찾을 수 없습니다: {pem_path}")
    else:
        try:
            with open(pem_path, "r") as f:
                private_key = f.read().strip()
            if not private_key.startswith("-----BEGIN"):
                errors.append(f"유효하지 않은 PEM 파일입니다: {pem_path}")
        except Exception as e:
            errors.append(f"Private Key 파일 읽기 실패: {e}")

    if errors:
        return {"errors": errors}

    return {
        "app_id": app_id,
        "private_key": private_key,
        "installation_id": installation_id,
        "repo": repo,
    }


def replace_embedded_vars(source: str, obfuscated: dict) -> str:
    """소스 코드에서 _EMBEDDED_* 변수를 난독화된 값으로 교체"""
    var_map = {
        "_EMBEDDED_APP_ID": obfuscated["app_id"],
        "_EMBEDDED_PRIVATE_KEY": obfuscated["private_key"],
        "_EMBEDDED_INSTALLATION_ID": obfuscated["installation_id"],
        "_EMBEDDED_REPO": obfuscated["repo"],
    }

    result = source
    replaced_count = 0

    for var_name, obf_value in var_map.items():
        # 패턴: _EMBEDDED_XXX: Optional[str] = None (주석 포함 가능)
        pattern = rf"({var_name}: Optional\[str\] = )None(.*)"
        replacement = rf'\1"{obf_value}"\2'

        new_result, count = re.subn(pattern, replacement, result)
        if count > 0:
            result = new_result
            replaced_count += count

    return result, replaced_count


def main():
    parser = argparse.ArgumentParser(description="GitHub App 인증 정보를 소스 코드에 난독화 임베딩")
    parser.add_argument("--dry-run", action="store_true", help="파일 수정 없이 미리보기")
    args = parser.parse_args()

    print("🔐 GitHub App 인증 정보 임베딩 스크립트")
    print(f"   대상 파일: {TARGET_FILE}")
    print()

    # 1. 인증 정보 읽기
    creds = read_credentials()
    if "errors" in creds:
        print("❌ 환경변수 오류:")
        for err in creds["errors"]:
            print(f"   - {err}")
        sys.exit(1)

    print("✅ 인증 정보 로드 완료:")
    print(f"   APP_ID: {creds['app_id']}")
    print(f"   INSTALLATION_ID: {creds['installation_id']}")
    print(f"   REPO: {creds['repo']}")
    print(f"   PRIVATE_KEY: ({len(creds['private_key'])} bytes)")
    print()

    # 2. 난독화
    obfuscated = {
        "app_id": obfuscate(creds["app_id"]),
        "private_key": obfuscate(creds["private_key"]),
        "installation_id": obfuscate(creds["installation_id"]),
        "repo": obfuscate(creds["repo"]),
    }

    if args.dry_run:
        print("🔍 [DRY-RUN] 난독화 결과 미리보기:")
        for key, value in obfuscated.items():
            preview = value[:40] + "..." if len(value) > 40 else value
            print(f"   {key}: {preview}")
        print()
        print("ℹ️  --dry-run 모드: 파일을 수정하지 않았습니다")
        return

    # 3. 소스 파일 읽기
    if not TARGET_FILE.exists():
        print(f"❌ 대상 파일을 찾을 수 없습니다: {TARGET_FILE}")
        sys.exit(1)

    source = TARGET_FILE.read_text(encoding="utf-8")

    # 4. 변수 교체
    modified, count = replace_embedded_vars(source, obfuscated)

    if count == 0:
        print("⚠️  교체할 변수를 찾지 못했습니다. 이미 임베딩된 상태일 수 있습니다.")
        sys.exit(1)

    if count != len(EMBEDDED_VARS):
        print(f"⚠️  {len(EMBEDDED_VARS)}개 중 {count}개만 교체되었습니다")

    # 5. 파일 쓰기
    TARGET_FILE.write_text(modified, encoding="utf-8")

    print(f"✅ {count}개 변수를 난독화된 값으로 교체했습니다")
    print("   임베딩 완료! PyInstaller 빌드를 진행하세요.")


if __name__ == "__main__":
    main()
