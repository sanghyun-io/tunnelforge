#!/usr/bin/env python3
"""
TunnelForge - bump_version CLI

GitHub Actions 워크플로에서 버전을 bump하는 CLI 스크립트.
scripts/versioning.py의 함수를 사용한다.

Usage:
    python scripts/bump_version.py --bump-type patch
    python scripts/bump_version.py --bump-type minor --dry-run
    python scripts/bump_version.py --bump-type major

Output (stdout, $GITHUB_OUTPUT 호환):
    new_version=X.Y.Z

Logging (stderr):
    현재 버전, bump 정보 등

에러 시:
    exit code 1 + stderr 에러 메시지
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Callable


def _apply_sync(
    sync_fn: Callable[[Path, str], None],
    path: Path,
    new_version: str,
    label: str,
    required: bool,
) -> int | None:
    """sync_fn(path, new_version)을 실행하고 결과를 stderr로 보고한다.

    write_version/sync_pyproject/sync_installer는 모두 (path, new_version)
    시그니처를 공유하므로 이 헬퍼 하나로 세 파일의 동기화를 처리한다.

    Args:
        sync_fn: 실행할 동기화 함수
        path: 대상 파일 경로
        new_version: 새 버전 문자열
        label: 실패 시 ERROR 메시지에 쓸 레이블
        required: False이고 path가 존재하지 않으면 건너뛴다

    Returns:
        실패 시 1 (호출자는 즉시 return해야 함), 성공/스킵 시 None
    """
    if not required and not path.exists():
        print(f"[SKIP] {path} 없음, 건너뜁니다.", file=sys.stderr)
        return None

    try:
        sync_fn(path, new_version)
        print(f"[OK] {path} 업데이트 완료", file=sys.stderr)
        return None
    except (ValueError, OSError) as e:
        print(f"ERROR: {label} 쓰기 실패: {e}", file=sys.stderr)
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description='TunnelForge 버전 bump CLI',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
예제:
  # 실제 bump (CI에서 사용)
  python scripts/bump_version.py --bump-type patch

  # dry-run (테스트용, 파일 수정 없음)
  python scripts/bump_version.py --bump-type patch --dry-run

  # $GITHUB_OUTPUT 연동
  python scripts/bump_version.py --bump-type minor >> $GITHUB_OUTPUT
        """
    )
    parser.add_argument(
        '--bump-type',
        required=True,
        choices=['major', 'minor', 'patch'],
        help='버전 증가 타입: major, minor, patch'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='파일 수정 없이 결과만 출력'
    )
    parser.add_argument(
        '--version-file',
        default='src/version.py',
        help='버전 파일 경로 (기본값: src/version.py)'
    )
    parser.add_argument(
        '--pyproject-file',
        default='pyproject.toml',
        help='pyproject.toml 경로 (기본값: pyproject.toml)'
    )
    parser.add_argument(
        '--installer-file',
        default='installer/TunnelForge.iss',
        help='Inno Setup 스크립트 경로 (기본값: installer/TunnelForge.iss)'
    )

    args = parser.parse_args()

    # 프로젝트 루트 기준으로 경로 설정
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    version_file = project_root / args.version_file
    pyproject_file = project_root / args.pyproject_file
    installer_file = project_root / args.installer_file

    # versioning 모듈 임포트 (scripts/ 디렉토리를 sys.path에 추가)
    sys.path.insert(0, str(script_dir))
    try:
        from versioning import (
            read_version,
            write_version,
            sync_pyproject,
            sync_installer,
            bump_version,
        )
    except ImportError as e:
        print(f"ERROR: versioning.py 임포트 실패: {e}", file=sys.stderr)
        return 1

    # 현재 버전 읽기
    if not version_file.exists():
        print(f"ERROR: 버전 파일을 찾을 수 없습니다: {version_file}", file=sys.stderr)
        return 1

    try:
        current_version = read_version(version_file)
    except ValueError as e:
        print(f"ERROR: 버전 파싱 실패: {e}", file=sys.stderr)
        return 1

    # 새 버전 계산
    try:
        new_version = bump_version(current_version, args.bump_type)
    except ValueError as e:
        print(f"ERROR: 버전 bump 실패: {e}", file=sys.stderr)
        return 1

    # 로그는 stderr로
    print(f"현재 버전: {current_version}", file=sys.stderr)
    print(f"bump 타입: {args.bump_type}", file=sys.stderr)
    print(f"새 버전:   {new_version}", file=sys.stderr)

    if args.dry_run:
        print(f"[DRY RUN] 파일 수정 없이 종료합니다.", file=sys.stderr)
    else:
        # 실제 파일 수정
        rc = _apply_sync(write_version, version_file, new_version, 'version 파일', required=True)
        if rc is not None:
            return rc

        rc = _apply_sync(sync_pyproject, pyproject_file, new_version, 'pyproject.toml', required=False)
        if rc is not None:
            return rc

        # Inno Setup 인스톨러 버전 동기화 (test_release_version_files_are_in_sync 요구사항)
        rc = _apply_sync(sync_installer, installer_file, new_version, 'installer .iss', required=False)
        if rc is not None:
            return rc

    # $GITHUB_OUTPUT 호환 형식으로 stdout 출력
    print(f"new_version={new_version}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
