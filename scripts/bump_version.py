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

import argparse
import sys
from pathlib import Path


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

    args = parser.parse_args()

    # 프로젝트 루트 기준으로 경로 설정
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent

    version_file = project_root / args.version_file
    pyproject_file = project_root / args.pyproject_file

    # versioning 모듈 임포트 (scripts/ 디렉토리를 sys.path에 추가)
    sys.path.insert(0, str(script_dir))
    try:
        from versioning import read_version, write_version, sync_pyproject, bump_version
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
        try:
            write_version(version_file, new_version)
            print(f"[OK] {version_file} 업데이트 완료", file=sys.stderr)
        except (ValueError, OSError) as e:
            print(f"ERROR: version 파일 쓰기 실패: {e}", file=sys.stderr)
            return 1

        if pyproject_file.exists():
            try:
                sync_pyproject(pyproject_file, new_version)
                print(f"[OK] {pyproject_file} 업데이트 완료", file=sys.stderr)
            except (ValueError, OSError) as e:
                print(f"ERROR: pyproject.toml 쓰기 실패: {e}", file=sys.stderr)
                return 1
        else:
            print(f"[SKIP] {pyproject_file} 없음, 건너뜁니다.", file=sys.stderr)

    # $GITHUB_OUTPUT 호환 형식으로 stdout 출력
    print(f"new_version={new_version}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
