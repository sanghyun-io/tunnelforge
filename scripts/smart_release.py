#!/usr/bin/env python3
"""
TunnelForge - Smart Release Script

GitHub 최신 릴리스와 비교하여 스마트하게 릴리스를 생성합니다.

Usage:
    python scripts/smart_release.py [--dry-run] [--help]

시나리오:
    1. 버전 동일: 사용자가 patch/minor/major 선택하여 버전 증가 후 릴리스
    2. 로컬 버전 높음: 확인 후 현재 버전으로 릴리스
    3. 원격 버전 높음: 경고 후 종료 (로컬 업데이트 필요)
"""

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError, URLError


class Colors:
    """ANSI color codes for terminal output."""
    CYAN = '\033[96m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    RED = '\033[91m'
    MAGENTA = '\033[95m'
    WHITE = '\033[97m'
    GRAY = '\033[90m'
    RESET = '\033[0m'


def print_color(text: str, color: str = Colors.WHITE) -> None:
    """Print colored text."""
    print(f"{color}{text}{Colors.RESET}")


def run_command(cmd: list[str], capture: bool = True) -> tuple[int, str, str]:
    """Run a shell command and return exit code, stdout, stderr."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=capture,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except Exception as e:
        return 1, "", str(e)


def compare_versions(v1: str, v2: str) -> int:
    """Compare two semantic versions. Returns: 1 if v1>v2, -1 if v1<v2, 0 if equal."""
    def parse(v: str) -> list[int]:
        return [int(x) for x in v.lstrip('v').split('.')]

    p1, p2 = parse(v1), parse(v2)
    for a, b in zip(p1, p2):
        if a > b:
            return 1
        if a < b:
            return -1
    return 0


def get_local_version(version_file: Path) -> tuple[str, str]:
    """Read version from version.py. Returns (version, file_content)."""
    content = version_file.read_text(encoding='utf-8')
    match = re.search(r'__version__\s*=\s*[\'"]([^\'"]+)[\'"]', content)
    if not match:
        raise ValueError(f"Cannot extract version from {version_file}")
    return match.group(1), content


def get_github_info() -> tuple[str, str]:
    """Extract owner and repo from git remote URL."""
    code, stdout, _ = run_command(['git', 'remote', 'get-url', 'origin'])
    if code != 0:
        raise RuntimeError("No git remote 'origin' configured")

    # Match both HTTPS and SSH URLs
    match = re.search(r'github\.com[:/]([^/]+)/(.+?)(?:\.git)?$', stdout)
    if not match:
        raise ValueError(f"Cannot parse GitHub URL: {stdout}")

    return match.group(1), match.group(2).rstrip('.git')


def get_remote_version(owner: str, repo: str) -> str:
    """Get latest release version from GitHub API."""
    url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    req = Request(url, headers={'User-Agent': 'TunnelForge-Release-Script'})

    try:
        with urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode('utf-8'))
            return data['tag_name'].lstrip('v')
    except HTTPError as e:
        if e.code == 404:
            return "0.0.0"  # No releases yet
        raise RuntimeError(f"GitHub API error: HTTP {e.code}")
    except URLError as e:
        raise RuntimeError(f"Network error: {e.reason}")


def bump_version(version: str, bump_type: str) -> str:
    """Increment version based on bump type."""
    parts = [int(x) for x in version.split('.')]

    if bump_type == 'major':
        return f"{parts[0] + 1}.0.0"
    elif bump_type == 'minor':
        return f"{parts[0]}.{parts[1] + 1}.0"
    else:  # patch
        return f"{parts[0]}.{parts[1]}.{parts[2] + 1}"


def update_version_file(version_file: Path, content: str, new_version: str) -> None:
    """Update version in version.py."""
    new_content = re.sub(
        r'__version__\s*=\s*[\'"][^\'"]+[\'"]',
        f'__version__ = "{new_version}"',
        content
    )
    version_file.write_text(new_content, encoding='utf-8')


def main() -> int:
    parser = argparse.ArgumentParser(
        description='TunnelForge - Smart Release',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
시나리오:
  - 버전 동일: 증가 타입(patch/minor/major) 선택
  - 로컬 높음: 확인 후 현재 버전으로 릴리스
  - 원격 높음: 경고 후 종료

예제:
  python scripts/smart_release.py           # 스마트 릴리스
  python scripts/smart_release.py --dry-run # 미리보기
        """
    )
    parser.add_argument('--dry-run', '-n', action='store_true',
                        help='미리보기만 수행 (실제 변경 없음)')
    args = parser.parse_args()

    # Find project root (where src/version.py exists)
    script_dir = Path(__file__).resolve().parent
    project_root = script_dir.parent
    os.chdir(project_root)

    version_file = Path('src/version.py')

    print_color("========================================", Colors.CYAN)
    print_color(" TunnelForge - Smart Release", Colors.CYAN)
    print_color("========================================", Colors.CYAN)
    print()

    # Step 1: Read local version
    print_color("[1/6] 로컬 버전 읽기...", Colors.YELLOW)

    if not version_file.exists():
        print_color(f"  [X] {version_file} 파일을 찾을 수 없습니다.", Colors.RED)
        return 1

    try:
        local_version, version_content = get_local_version(version_file)
        print_color(f"  로컬 버전: {local_version}", Colors.WHITE)
    except ValueError as e:
        print_color(f"  [X] {e}", Colors.RED)
        return 1
    print()

    # Step 2: Get GitHub info
    print_color("[2/6] GitHub 정보 확인...", Colors.YELLOW)

    try:
        owner, repo = get_github_info()
        print_color(f"  Repository: {owner}/{repo}", Colors.WHITE)
    except (RuntimeError, ValueError) as e:
        print_color(f"  [X] {e}", Colors.RED)
        return 1
    print()

    # Step 3: Get remote version
    print_color("[3/6] GitHub 최신 릴리스 확인...", Colors.YELLOW)

    try:
        remote_version = get_remote_version(owner, repo)
        if remote_version == "0.0.0":
            print_color("  [!] GitHub에 릴리스가 없습니다.", Colors.YELLOW)
            print_color("  첫 번째 릴리스를 생성합니다.", Colors.GRAY)
        else:
            print_color(f"  원격 버전: {remote_version}", Colors.WHITE)
    except RuntimeError as e:
        print_color(f"  [X] {e}", Colors.RED)
        return 1
    print()

    # Step 4: Compare versions
    print_color("[4/6] 버전 비교...", Colors.YELLOW)
    print()
    print_color(f"  로컬:  {local_version}", Colors.CYAN)
    print_color(f"  원격:  {remote_version}", Colors.MAGENTA)
    print()

    comparison = compare_versions(local_version, remote_version)
    needs_bump = False
    bump_type = None

    if comparison == 0:
        # Same version - ask for bump type
        print_color("  버전이 동일합니다. 새 릴리스를 만들려면 버전을 올려야 합니다.", Colors.YELLOW)
        print()
        print_color("버전을 어떻게 올리시겠습니까?", Colors.CYAN)
        print()

        parts = [int(x) for x in local_version.split('.')]
        patch_ver = f"{parts[0]}.{parts[1]}.{parts[2] + 1}"
        minor_ver = f"{parts[0]}.{parts[1] + 1}.0"
        major_ver = f"{parts[0] + 1}.0.0"

        print_color(f"  [1] patch  ({local_version} -> {patch_ver})  - 버그 수정", Colors.WHITE)
        print_color(f"  [2] minor  ({local_version} -> {minor_ver})  - 기능 추가", Colors.WHITE)
        print_color(f"  [3] major  ({local_version} -> {major_ver})  - 큰 변경", Colors.WHITE)
        print_color("  [0] 취소", Colors.GRAY)
        print()

        try:
            choice = input("선택 (0-3): ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            print_color("취소되었습니다.", Colors.GRAY)
            return 0

        if choice == '1':
            new_version = patch_ver
            bump_type = 'patch'
        elif choice == '2':
            new_version = minor_ver
            bump_type = 'minor'
        elif choice == '3':
            new_version = major_ver
            bump_type = 'major'
        else:
            print()
            print_color("취소되었습니다.", Colors.GRAY)
            return 0

        needs_bump = True

    elif comparison > 0:
        # Local is higher - confirm release
        print_color("  [OK] 로컬 버전이 앞섭니다.", Colors.GREEN)
        print()
        print_color(f"로컬 버전 v{local_version} 으로 릴리스하시겠습니까?", Colors.CYAN)
        print()

        try:
            confirm = input("(y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print_color("취소되었습니다.", Colors.GRAY)
            return 0

        if confirm != 'y':
            print()
            print_color("취소되었습니다.", Colors.GRAY)
            return 0

        new_version = local_version
        needs_bump = False

    else:
        # Remote is higher - error
        print_color("  [X] 원격 버전이 로컬보다 높습니다!", Colors.RED)
        print()
        print_color(f"GitHub에 더 최신 릴리스({remote_version})가 있습니다.", Colors.YELLOW)
        print_color("로컬 저장소를 업데이트하거나, src/version.py를 수정하세요.", Colors.YELLOW)
        print()
        return 1

    print()

    # Dry run mode
    if args.dry_run:
        print_color("[DRY RUN] 실제 실행 없이 미리보기 모드입니다.", Colors.YELLOW)
        print()
        print_color("변경 사항:", Colors.CYAN)

        if needs_bump:
            print_color(f"  {local_version} -> {new_version} ({bump_type})", Colors.WHITE)
            print()
            print_color("업데이트될 파일:", Colors.CYAN)
            print_color(f"  - {version_file}", Colors.GRAY)
        else:
            print_color(f"  버전 변경 없음 (현재: {new_version})", Colors.WHITE)

        print()
        print_color("실행될 명령어:", Colors.CYAN)

        if needs_bump:
            print_color(f"  git add {version_file}", Colors.GRAY)
            print_color(f'  git commit -m "Bump version to {new_version}"', Colors.GRAY)

        print_color("  git push origin main", Colors.GRAY)
        print_color(f"  git tag v{new_version}", Colors.GRAY)
        print_color(f"  git push origin v{new_version}", Colors.GRAY)
        print()
        return 0

    # Step 5: Update version file (if needed)
    if needs_bump:
        print_color("[5/6] 버전 업데이트 중...", Colors.YELLOW)

        update_version_file(version_file, version_content, new_version)
        print_color(f"  [OK] {version_file} 업데이트 완료", Colors.GREEN)
        print()

        # Show git diff
        run_command(['git', 'diff', str(version_file)], capture=False)
        print()
    else:
        print_color(f"[5/6] 버전 업데이트 건너뛰기 (이미 v{new_version})", Colors.YELLOW)
        print()

    # Step 6: Auto release
    print_color("[6/6] 자동 릴리스 시작...", Colors.YELLOW)
    print()

    # Commit (if version was bumped)
    if needs_bump:
        print_color("  [릴리스 1/3] 변경사항 커밋...", Colors.CYAN)

        run_command(['git', 'add', str(version_file)])
        code, _, stderr = run_command(['git', 'commit', '-m', f'Bump version to {new_version}'])

        if code != 0:
            print_color(f"    [X] 커밋 실패: {stderr}", Colors.RED)
            return 1

        print_color("    [OK] 커밋 완료", Colors.GREEN)
        print()

    # Push
    print_color("  [릴리스 2/3] main 브랜치 push...", Colors.CYAN)

    if needs_bump:
        code, _, stderr = run_command(['git', 'push', 'origin', 'main'])

        if code != 0:
            print_color(f"    [X] Push 실패: {stderr}", Colors.RED)
            return 1

        print_color("    [OK] Push 완료", Colors.GREEN)
    else:
        print_color("    [!] 변경사항 없음 - Push 건너뛰기", Colors.YELLOW)
    print()

    # Create and push tag
    print_color("  [릴리스 3/3] 릴리스 태그 생성 및 push...", Colors.CYAN)

    tag = f"v{new_version}"

    # Check if tag exists
    code, existing_tag, _ = run_command(['git', 'tag', '-l', tag])

    if existing_tag:
        print_color(f"    [!] 태그 {tag} 가 이미 존재합니다.", Colors.YELLOW)

        try:
            overwrite = input("    기존 태그를 덮어쓰시겠습니까? (y/N): ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            print_color("    취소되었습니다.", Colors.GRAY)
            return 0

        if overwrite != 'y':
            print_color("    취소되었습니다.", Colors.GRAY)
            return 0

        # Delete local tag
        run_command(['git', 'tag', '-d', tag])
        print_color("    [!] 로컬 태그 삭제됨", Colors.YELLOW)

    # Create tag
    code, _, stderr = run_command(['git', 'tag', '-a', tag, '-m', f'Release {tag}'])

    if code != 0:
        print_color(f"    [X] 태그 생성 실패: {stderr}", Colors.RED)
        return 1

    # Push tag
    code, _, stderr = run_command(['git', 'push', 'origin', tag, '--force'])

    if code != 0:
        print_color(f"    [X] 태그 Push 실패: {stderr}", Colors.RED)
        print()
        print_color("로컬 태그를 삭제하려면:", Colors.YELLOW)
        print_color(f"  git tag -d {tag}", Colors.GRAY)
        return 1

    print_color("    [OK] 릴리스 태그 push 완료", Colors.GREEN)
    print()

    # Done
    print_color("========================================", Colors.CYAN)
    print_color(" [OK] 스마트 릴리스 완료!", Colors.GREEN)
    print_color("========================================", Colors.CYAN)
    print()
    print_color(f"버전: {new_version}", Colors.WHITE)
    print_color(f"태그: {tag}", Colors.WHITE)

    if needs_bump:
        print_color(f"타입: {bump_type}", Colors.WHITE)

    print()
    print_color("GitHub Actions에서 빌드가 진행됩니다:", Colors.CYAN)
    print_color(f"  https://github.com/{owner}/{repo}/actions", Colors.WHITE)
    print()
    print_color("빌드 완료 후 릴리스 확인:", Colors.CYAN)
    print_color(f"  https://github.com/{owner}/{repo}/releases/tag/{tag}", Colors.WHITE)
    print()
    print_color("빌드는 약 5-10분 소요됩니다.", Colors.GRAY)
    print()

    return 0


if __name__ == '__main__':
    sys.exit(main())
