#!/usr/bin/env python3
"""
TunnelForge - 공유 버저닝 모듈

bump_version.py와 smart_release.py에서 공유하는 핵심 버저닝 함수 모음.
버전 파싱, 계산, 파일 쓰기 로직을 단일 모듈에서 관리한다.
"""

import re
from pathlib import Path


def read_version(version_file: Path) -> str:
    """src/version.py에서 __version__ 값을 읽어 반환한다.

    Args:
        version_file: src/version.py 경로

    Returns:
        버전 문자열 (예: "1.11.0")

    Raises:
        FileNotFoundError: 파일이 존재하지 않을 때
        ValueError: __version__ 파싱 실패 시
    """
    content = version_file.read_text(encoding='utf-8')
    match = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)[\'"]', content)
    if not match:
        raise ValueError(f"__version__ 을 찾을 수 없습니다: {version_file}")
    return match.group(1).strip()


def write_version(version_file: Path, new_version: str) -> None:
    """src/version.py의 __version__ 라인만 교체한다. 나머지 내용은 보존된다.

    Args:
        version_file: src/version.py 경로
        new_version: 새 버전 문자열 (예: "1.11.1")

    Raises:
        FileNotFoundError: 파일이 존재하지 않을 때
        ValueError: __version__ 라인을 찾을 수 없을 때
    """
    content = version_file.read_text(encoding='utf-8')
    new_content, count = re.subn(
        r'__version__\s*=\s*[\'"][^\'"]+[\'"]',
        f'__version__ = "{new_version}"',
        content
    )
    if count == 0:
        raise ValueError(f"__version__ 라인을 찾을 수 없습니다: {version_file}")
    version_file.write_text(new_content, encoding='utf-8')


def sync_pyproject(pyproject_file: Path, new_version: str) -> None:
    """pyproject.toml의 [project] 섹션 version 필드를 업데이트한다.

    [project] 섹션의 version 필드만 수정하며 다른 설정은 보존된다.

    Args:
        pyproject_file: pyproject.toml 경로
        new_version: 새 버전 문자열 (예: "1.11.1")

    Raises:
        FileNotFoundError: 파일이 존재하지 않을 때
        ValueError: [project] 섹션의 version 필드를 찾을 수 없을 때
    """
    content = pyproject_file.read_text(encoding='utf-8')

    # 라인별로 [project] 섹션을 탐색하여 version 필드만 교체
    lines = content.splitlines(keepends=True)
    in_project_section = False
    new_lines = []
    replaced = False

    for line in lines:
        if re.match(r'^\[project\]', line):
            in_project_section = True
        elif re.match(r'^\[', line):
            in_project_section = False

        if in_project_section and not replaced and re.match(r'^version\s*=\s*"', line):
            line = re.sub(r'version\s*=\s*"[^"]*"', f'version = "{new_version}"', line)
            replaced = True

        new_lines.append(line)

    if not replaced:
        raise ValueError(f"[project] 섹션의 version 필드를 찾을 수 없습니다: {pyproject_file}")

    pyproject_file.write_text(''.join(new_lines), encoding='utf-8')


def bump_version(version: str, bump_type: str) -> str:
    """시맨틱 버전을 bump_type에 따라 증가시킨다.

    Args:
        version: 현재 버전 문자열 (예: "1.11.0")
        bump_type: "major", "minor", "patch" 중 하나

    Returns:
        새 버전 문자열

    Raises:
        ValueError: 잘못된 bump_type 또는 버전 형식
    """
    if bump_type not in ('major', 'minor', 'patch'):
        raise ValueError(f"유효하지 않은 bump_type: {bump_type!r}. 'major', 'minor', 'patch' 중 하나여야 합니다.")

    try:
        parts = [int(x) for x in version.split('.')]
    except (ValueError, AttributeError) as e:
        raise ValueError(f"유효하지 않은 버전 형식: {version!r}") from e

    if len(parts) != 3:
        raise ValueError(f"버전은 X.Y.Z 형식이어야 합니다: {version!r}")

    if bump_type == 'major':
        return f"{parts[0] + 1}.0.0"
    elif bump_type == 'minor':
        return f"{parts[0]}.{parts[1] + 1}.0"
    else:  # patch
        return f"{parts[0]}.{parts[1]}.{parts[2] + 1}"


def compare_versions(v1: str, v2: str) -> int:
    """두 시맨틱 버전을 비교한다.

    Args:
        v1: 첫 번째 버전 (예: "1.11.0" 또는 "v1.11.0")
        v2: 두 번째 버전

    Returns:
        1  v1 > v2
        0  v1 == v2
        -1 v1 < v2
    """
    def parse(v: str) -> list:
        return [int(x) for x in v.lstrip('v').split('.')]

    p1, p2 = parse(v1), parse(v2)
    for a, b in zip(p1, p2):
        if a > b:
            return 1
        if a < b:
            return -1
    return 0
