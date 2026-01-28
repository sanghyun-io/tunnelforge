#!/bin/bash
#
# TunnelForge - Smart Release Script (Bash version)
#
# GitHub 최신 릴리스와 비교하여 스마트하게 릴리스를 생성합니다.
#
# Usage:
#     ./scripts/smart-release.sh [--dry-run] [--help]
#
# 시나리오:
#     1. 버전 동일: 사용자가 patch/minor/major 선택하여 버전 증가 후 릴리스
#     2. 로컬 버전 높음: 확인 후 현재 버전으로 릴리스
#     3. 원격 버전 높음: 경고 후 종료 (로컬 업데이트 필요)
#

set -e

# Colors
CYAN='\033[96m'
GREEN='\033[92m'
YELLOW='\033[93m'
RED='\033[91m'
MAGENTA='\033[95m'
WHITE='\033[97m'
GRAY='\033[90m'
RESET='\033[0m'

# Globals
DRY_RUN=false
PROJECT_ROOT=""
VERSION_FILE=""
LOCAL_VERSION=""
REMOTE_VERSION=""
NEW_VERSION=""
NEEDS_BUMP=false
BUMP_TYPE=""
OWNER=""
REPO=""

print_color() {
    local color="$1"
    local text="$2"
    echo -e "${color}${text}${RESET}"
}

show_help() {
    cat << 'EOF'
TunnelForge - Smart Release (Bash)

Usage:
    ./scripts/smart-release.sh [OPTIONS]

Options:
    --dry-run, -n    미리보기만 수행 (실제 변경 없음)
    --help, -h       이 도움말 출력

시나리오:
    - 버전 동일: 증가 타입(patch/minor/major) 선택
    - 로컬 높음: 확인 후 현재 버전으로 릴리스
    - 원격 높음: 경고 후 종료

예제:
    ./scripts/smart-release.sh           # 스마트 릴리스
    ./scripts/smart-release.sh --dry-run # 미리보기

EOF
    exit 0
}

# Compare semantic versions
# Returns: 1 if v1>v2, 255 (-1) if v1<v2, 0 if equal
compare_versions() {
    local v1="$1"
    local v2="$2"

    # Remove 'v' prefix
    v1="${v1#v}"
    v2="${v2#v}"

    IFS='.' read -r v1_major v1_minor v1_patch <<< "$v1"
    IFS='.' read -r v2_major v2_minor v2_patch <<< "$v2"

    if (( v1_major > v2_major )); then echo 1; return; fi
    if (( v1_major < v2_major )); then echo -1; return; fi
    if (( v1_minor > v2_minor )); then echo 1; return; fi
    if (( v1_minor < v2_minor )); then echo -1; return; fi
    if (( v1_patch > v2_patch )); then echo 1; return; fi
    if (( v1_patch < v2_patch )); then echo -1; return; fi

    echo 0
}

# Bump version
bump_version() {
    local version="$1"
    local type="$2"

    IFS='.' read -r major minor patch <<< "$version"

    case "$type" in
        major)
            echo "$((major + 1)).0.0"
            ;;
        minor)
            echo "${major}.$((minor + 1)).0"
            ;;
        patch)
            echo "${major}.${minor}.$((patch + 1))"
            ;;
    esac
}

# Parse arguments
parse_args() {
    while [[ $# -gt 0 ]]; do
        case "$1" in
            --dry-run|-n)
                DRY_RUN=true
                shift
                ;;
            --help|-h)
                show_help
                ;;
            *)
                print_color "$RED" "Unknown option: $1"
                exit 1
                ;;
        esac
    done
}

# Main function
main() {
    parse_args "$@"

    # Find project root
    SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
    cd "$PROJECT_ROOT"

    VERSION_FILE="src/version.py"

    print_color "$CYAN" "========================================"
    print_color "$CYAN" " TunnelForge - Smart Release"
    print_color "$CYAN" "========================================"
    echo

    # Step 1: Read local version
    print_color "$YELLOW" "[1/6] 로컬 버전 읽기..."

    if [[ ! -f "$VERSION_FILE" ]]; then
        print_color "$RED" "  [X] $VERSION_FILE 파일을 찾을 수 없습니다."
        exit 1
    fi

    LOCAL_VERSION=$(grep -oP '__version__\s*=\s*["\x27]\K[^"\x27]+' "$VERSION_FILE" 2>/dev/null || true)

    if [[ -z "$LOCAL_VERSION" ]]; then
        print_color "$RED" "  [X] version.py에서 버전을 추출할 수 없습니다."
        exit 1
    fi

    print_color "$WHITE" "  로컬 버전: $LOCAL_VERSION"
    echo

    # Step 2: Get GitHub info
    print_color "$YELLOW" "[2/6] GitHub 정보 확인..."

    REMOTE_URL=$(git remote get-url origin 2>/dev/null || true)

    if [[ -z "$REMOTE_URL" ]]; then
        print_color "$RED" "  [X] 원격 저장소(origin)가 설정되지 않았습니다."
        exit 1
    fi

    # Extract owner/repo from URL
    if [[ "$REMOTE_URL" =~ github\.com[:/]([^/]+)/([^/]+)(\.git)?$ ]]; then
        OWNER="${BASH_REMATCH[1]}"
        REPO="${BASH_REMATCH[2]}"
        REPO="${REPO%.git}"
    else
        print_color "$RED" "  [X] GitHub 저장소 URL을 파싱할 수 없습니다."
        print_color "$GRAY" "  URL: $REMOTE_URL"
        exit 1
    fi

    print_color "$WHITE" "  Repository: $OWNER/$REPO"
    echo

    # Step 3: Get remote version
    print_color "$YELLOW" "[3/6] GitHub 최신 릴리스 확인..."

    API_URL="https://api.github.com/repos/$OWNER/$REPO/releases/latest"
    API_RESPONSE=$(curl -s -w "\n%{http_code}" "$API_URL" 2>/dev/null || echo -e "\n000")

    HTTP_CODE=$(echo "$API_RESPONSE" | tail -n1)
    JSON_BODY=$(echo "$API_RESPONSE" | sed '$d')

    if [[ "$HTTP_CODE" == "404" ]]; then
        print_color "$YELLOW" "  [!] GitHub에 릴리스가 없습니다."
        print_color "$GRAY" "  첫 번째 릴리스를 생성합니다."
        REMOTE_VERSION="0.0.0"
    elif [[ "$HTTP_CODE" != "200" ]]; then
        print_color "$RED" "  [X] GitHub API 요청 실패 (HTTP $HTTP_CODE)"
        exit 1
    else
        REMOTE_VERSION=$(echo "$JSON_BODY" | grep -oP '"tag_name"\s*:\s*"\K[^"]+' | sed 's/^v//')
        print_color "$WHITE" "  원격 버전: $REMOTE_VERSION"
    fi
    echo

    # Step 4: Compare versions
    print_color "$YELLOW" "[4/6] 버전 비교..."
    echo
    print_color "$CYAN" "  로컬:  $LOCAL_VERSION"
    print_color "$MAGENTA" "  원격:  $REMOTE_VERSION"
    echo

    COMPARISON=$(compare_versions "$LOCAL_VERSION" "$REMOTE_VERSION")

    if [[ "$COMPARISON" == "0" ]]; then
        # Same version - ask for bump type
        print_color "$YELLOW" "  버전이 동일합니다. 새 릴리스를 만들려면 버전을 올려야 합니다."
        echo
        print_color "$CYAN" "버전을 어떻게 올리시겠습니까?"
        echo

        PATCH_VER=$(bump_version "$LOCAL_VERSION" "patch")
        MINOR_VER=$(bump_version "$LOCAL_VERSION" "minor")
        MAJOR_VER=$(bump_version "$LOCAL_VERSION" "major")

        print_color "$WHITE" "  [1] patch  ($LOCAL_VERSION -> $PATCH_VER)  - 버그 수정"
        print_color "$WHITE" "  [2] minor  ($LOCAL_VERSION -> $MINOR_VER)  - 기능 추가"
        print_color "$WHITE" "  [3] major  ($LOCAL_VERSION -> $MAJOR_VER)  - 큰 변경"
        print_color "$GRAY" "  [0] 취소"
        echo

        read -r -p "선택 (0-3): " choice

        case "$choice" in
            1)
                NEW_VERSION="$PATCH_VER"
                BUMP_TYPE="patch"
                ;;
            2)
                NEW_VERSION="$MINOR_VER"
                BUMP_TYPE="minor"
                ;;
            3)
                NEW_VERSION="$MAJOR_VER"
                BUMP_TYPE="major"
                ;;
            *)
                echo
                print_color "$GRAY" "취소되었습니다."
                exit 0
                ;;
        esac

        NEEDS_BUMP=true

    elif [[ "$COMPARISON" == "1" ]]; then
        # Local is higher - confirm release
        print_color "$GREEN" "  [OK] 로컬 버전이 앞섭니다."
        echo
        print_color "$CYAN" "로컬 버전 v$LOCAL_VERSION 으로 릴리스하시겠습니까?"
        echo

        read -r -p "(y/N): " confirm

        if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
            echo
            print_color "$GRAY" "취소되었습니다."
            exit 0
        fi

        NEW_VERSION="$LOCAL_VERSION"
        NEEDS_BUMP=false

    else
        # Remote is higher - error
        print_color "$RED" "  [X] 원격 버전이 로컬보다 높습니다!"
        echo
        print_color "$YELLOW" "GitHub에 더 최신 릴리스($REMOTE_VERSION)가 있습니다."
        print_color "$YELLOW" "로컬 저장소를 업데이트하거나, src/version.py를 수정하세요."
        echo
        exit 1
    fi

    echo

    # Dry run mode
    if [[ "$DRY_RUN" == "true" ]]; then
        print_color "$YELLOW" "[DRY RUN] 실제 실행 없이 미리보기 모드입니다."
        echo
        print_color "$CYAN" "변경 사항:"

        if [[ "$NEEDS_BUMP" == "true" ]]; then
            print_color "$WHITE" "  $LOCAL_VERSION -> $NEW_VERSION ($BUMP_TYPE)"
            echo
            print_color "$CYAN" "업데이트될 파일:"
            print_color "$GRAY" "  - $VERSION_FILE"
        else
            print_color "$WHITE" "  버전 변경 없음 (현재: $NEW_VERSION)"
        fi

        echo
        print_color "$CYAN" "실행될 명령어:"

        if [[ "$NEEDS_BUMP" == "true" ]]; then
            print_color "$GRAY" "  git add $VERSION_FILE"
            print_color "$GRAY" "  git commit -m \"Bump version to $NEW_VERSION\""
        fi

        print_color "$GRAY" "  git push origin main"
        print_color "$GRAY" "  git tag v$NEW_VERSION"
        print_color "$GRAY" "  git push origin v$NEW_VERSION"
        echo
        exit 0
    fi

    # Step 5: Update version file (if needed)
    if [[ "$NEEDS_BUMP" == "true" ]]; then
        print_color "$YELLOW" "[5/6] 버전 업데이트 중..."

        # Update version.py
        sed -i "s/__version__\s*=\s*[\"'][^\"']*[\"']/__version__ = \"$NEW_VERSION\"/" "$VERSION_FILE"

        print_color "$GREEN" "  [OK] $VERSION_FILE 업데이트 완료"
        echo

        # Show git diff
        git diff "$VERSION_FILE"
        echo
    else
        print_color "$YELLOW" "[5/6] 버전 업데이트 건너뛰기 (이미 v$NEW_VERSION)"
        echo
    fi

    # Step 6: Auto release
    print_color "$YELLOW" "[6/6] 자동 릴리스 시작..."
    echo

    # Commit (if version was bumped)
    if [[ "$NEEDS_BUMP" == "true" ]]; then
        print_color "$CYAN" "  [릴리스 1/3] 변경사항 커밋..."

        git add "$VERSION_FILE"

        if ! git commit -m "Bump version to $NEW_VERSION"; then
            print_color "$RED" "    [X] 커밋 실패"
            exit 1
        fi

        print_color "$GREEN" "    [OK] 커밋 완료"
        echo
    fi

    # Push
    print_color "$CYAN" "  [릴리스 2/3] main 브랜치 push..."

    if [[ "$NEEDS_BUMP" == "true" ]]; then
        if ! git push origin main; then
            print_color "$RED" "    [X] Push 실패"
            exit 1
        fi
        print_color "$GREEN" "    [OK] Push 완료"
    else
        print_color "$YELLOW" "    [!] 변경사항 없음 - Push 건너뛰기"
    fi
    echo

    # Create and push tag
    print_color "$CYAN" "  [릴리스 3/3] 릴리스 태그 생성 및 push..."

    TAG="v$NEW_VERSION"

    # Check if tag exists
    if git tag -l "$TAG" | grep -q "$TAG"; then
        print_color "$YELLOW" "    [!] 태그 $TAG 가 이미 존재합니다."

        read -r -p "    기존 태그를 덮어쓰시겠습니까? (y/N): " overwrite

        if [[ "$overwrite" != "y" && "$overwrite" != "Y" ]]; then
            print_color "$GRAY" "    취소되었습니다."
            exit 0
        fi

        # Delete local tag
        git tag -d "$TAG"
        print_color "$YELLOW" "    [!] 로컬 태그 삭제됨"
    fi

    # Create tag
    if ! git tag -a "$TAG" -m "Release $TAG"; then
        print_color "$RED" "    [X] 태그 생성 실패"
        exit 1
    fi

    # Push tag
    if ! git push origin "$TAG" --force; then
        print_color "$RED" "    [X] 태그 Push 실패"
        echo
        print_color "$YELLOW" "로컬 태그를 삭제하려면:"
        print_color "$GRAY" "  git tag -d $TAG"
        exit 1
    fi

    print_color "$GREEN" "    [OK] 릴리스 태그 push 완료"
    echo

    # Done
    print_color "$CYAN" "========================================"
    print_color "$GREEN" " [OK] 스마트 릴리스 완료!"
    print_color "$CYAN" "========================================"
    echo
    print_color "$WHITE" "버전: $NEW_VERSION"
    print_color "$WHITE" "태그: $TAG"

    if [[ "$NEEDS_BUMP" == "true" ]]; then
        print_color "$WHITE" "타입: $BUMP_TYPE"
    fi

    echo
    print_color "$CYAN" "GitHub Actions에서 빌드가 진행됩니다:"
    print_color "$WHITE" "  https://github.com/$OWNER/$REPO/actions"
    echo
    print_color "$CYAN" "빌드 완료 후 릴리스 확인:"
    print_color "$WHITE" "  https://github.com/$OWNER/$REPO/releases/tag/$TAG"
    echo
    print_color "$GRAY" "빌드는 약 5-10분 소요됩니다."
    echo
}

main "$@"
