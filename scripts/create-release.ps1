<#
.SYNOPSIS
    src/version.py의 버전으로 Git 태그를 생성하고 GitHub Actions 릴리스를 트리거합니다.

.DESCRIPTION
    이 스크립트는 src/version.py에서 현재 버전을 읽어서 Git 태그(v{version})를 생성하고
    GitHub에 push하여 자동 빌드 및 릴리스를 시작합니다.

    주의: 이 스크립트는 버전을 변경하지 않습니다.
    src/version.py를 먼저 수정하거나 bump-version.ps1을 사용하세요.

    워크플로우:
    1. src/version.py에서 버전 읽기
    2. Git 상태 확인 (커밋되지 않은 변경사항 경고)
    3. 태그 중복 확인
    4. Git 태그 생성 (v{version})
    5. 태그를 GitHub에 push
    6. GitHub Actions 자동 트리거

.PARAMETER DryRun
    실제로 태그를 생성하거나 push하지 않고 미리보기만 수행합니다.
    어떤 태그가 생성될지, 어떤 명령어가 실행될지 확인할 수 있습니다.

.EXAMPLE
    .\scripts\create-release.ps1

    src/version.py의 버전으로 릴리스 태그를 생성하고 GitHub에 push합니다.
    예: __version__ = "1.0.1"이면 v1.0.1 태그 생성

.EXAMPLE
    .\scripts\create-release.ps1 -DryRun

    실제로 태그를 생성하지 않고 미리보기만 수행합니다.
    어떤 태그가 생성될지 확인할 수 있습니다.

.NOTES
    파일명: create-release.ps1
    작성자: TunnelDB Manager Team
    요구사항:
    - Git 설치 필요
    - Git 원격 저장소(origin) 설정 필요
    - src/version.py가 먼저 업데이트되어 있어야 함

    권장 사항:
    버전 증가와 릴리스를 동시에 하려면 bump-version.ps1 -AutoRelease를 사용하세요.

.LINK
    https://github.com/sanghyun-io/db-connector
#>

param(
    [Parameter(HelpMessage="실제 실행 없이 미리보기만 수행")]
    [switch]$DryRun = $false,

    [Alias("h")]
    [Parameter(HelpMessage="사용법 출력")]
    [switch]$Help = $false
)

# Help 출력
if ($Help) {
    Write-Host ""
    Write-Host "TunnelDB Manager - Release Creator" -ForegroundColor Cyan
    Write-Host "src/version.py의 버전으로 Git 태그를 생성하고 GitHub Actions를 트리거합니다." -ForegroundColor Gray
    Write-Host ""
    Write-Host "사용법:" -ForegroundColor Yellow
    Write-Host "  .\scripts\create-release.ps1 [-DryRun]" -ForegroundColor White
    Write-Host ""
    Write-Host "옵션:" -ForegroundColor Yellow
    Write-Host "  -DryRun          실제 실행 없이 미리보기" -ForegroundColor White
    Write-Host "  -Help, -h        이 도움말 출력" -ForegroundColor White
    Write-Host ""
    Write-Host "설명:" -ForegroundColor Yellow
    Write-Host "  이 스크립트는 버전을 변경하지 않습니다." -ForegroundColor Gray
    Write-Host "  src/version.py에 있는 버전으로 Git 태그를 생성합니다." -ForegroundColor Gray
    Write-Host ""
    Write-Host "  워크플로우:" -ForegroundColor Gray
    Write-Host "    1. src/version.py에서 버전 읽기" -ForegroundColor Gray
    Write-Host "    2. Git 태그 생성 (v{version})" -ForegroundColor Gray
    Write-Host "    3. GitHub에 태그 push" -ForegroundColor Gray
    Write-Host "    4. GitHub Actions 자동 트리거" -ForegroundColor Gray
    Write-Host ""
    Write-Host "예제:" -ForegroundColor Yellow
    Write-Host "  # 릴리스 생성" -ForegroundColor Gray
    Write-Host "  .\scripts\create-release.ps1" -ForegroundColor Green
    Write-Host ""
    Write-Host "  # 미리보기" -ForegroundColor Gray
    Write-Host "  .\scripts\create-release.ps1 -DryRun" -ForegroundColor White
    Write-Host ""
    Write-Host "권장:" -ForegroundColor Yellow
    Write-Host "  버전 증가와 릴리스를 한 번에:" -ForegroundColor Gray
    Write-Host "  .\scripts\bump-version.ps1 -Type patch -AutoRelease" -ForegroundColor Green
    Write-Host ""
    exit 0
}

# 스크립트 위치 기준으로 프로젝트 루트 경로 설정
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " TunnelDB Manager - Release Creator" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Git 설치 확인
$gitCheck = git --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "❌ Git이 설치되지 않았습니다." -ForegroundColor Red
    exit 1
}
Write-Host "Git: $gitCheck" -ForegroundColor Gray
Write-Host ""

# version.py에서 버전 추출
Write-Host "[1/5] 버전 정보 읽기..." -ForegroundColor Yellow

$versionPy = "src\version.py"
if (-not (Test-Path $versionPy)) {
    Write-Host "  ❌ $versionPy 파일을 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

$versionContent = Get-Content $versionPy -Raw
if ($versionContent -match '__version__\s*=\s*[''"]([^''"]+)[''"]') {
    $version = $matches[1]
    Write-Host "  ✅ 현재 버전: $version" -ForegroundColor Green
} else {
    Write-Host "  ❌ version.py에서 버전을 추출할 수 없습니다." -ForegroundColor Red
    exit 1
}

$tag = "v$version"
Write-Host "  ✅ 생성할 태그: $tag" -ForegroundColor Green
Write-Host ""

# Git 상태 확인
Write-Host "[2/5] Git 상태 확인..." -ForegroundColor Yellow

# 변경사항 확인
$gitStatus = git status --porcelain
if ($gitStatus) {
    Write-Host "  ⚠️  커밋되지 않은 변경사항이 있습니다:" -ForegroundColor Yellow
    Write-Host ""
    git status --short
    Write-Host ""

    $response = Read-Host "계속하시겠습니까? (y/N)"
    if ($response -ne 'y' -and $response -ne 'Y') {
        Write-Host "  취소되었습니다." -ForegroundColor Gray
        exit 0
    }
    Write-Host ""
}

# 현재 브랜치 확인
$currentBranch = git rev-parse --abbrev-ref HEAD
Write-Host "  현재 브랜치: $currentBranch" -ForegroundColor Gray

# 원격 저장소 확인
$remoteUrl = git remote get-url origin 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 원격 저장소(origin)가 설정되지 않았습니다." -ForegroundColor Red
    exit 1
}
Write-Host "  원격 저장소: $remoteUrl" -ForegroundColor Gray
Write-Host ""

# 태그 중복 확인
Write-Host "[3/5] 태그 중복 확인..." -ForegroundColor Yellow

$existingTag = git tag -l $tag
if ($existingTag) {
    Write-Host "  ❌ 태그 '$tag'가 이미 존재합니다." -ForegroundColor Red
    Write-Host "  src/version.py의 버전을 업데이트하거나, 기존 태그를 삭제하세요." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "기존 태그 삭제 방법:" -ForegroundColor Gray
    Write-Host "  git tag -d $tag" -ForegroundColor Gray
    Write-Host "  git push origin :refs/tags/$tag" -ForegroundColor Gray
    exit 1
}
Write-Host "  ✅ 태그 사용 가능" -ForegroundColor Green
Write-Host ""

# 최종 확인
Write-Host "[4/5] 릴리스 정보 확인" -ForegroundColor Yellow
Write-Host ""
Write-Host "  버전: $version" -ForegroundColor White
Write-Host "  태그: $tag" -ForegroundColor White
Write-Host "  브랜치: $currentBranch" -ForegroundColor White
Write-Host "  원격: $remoteUrl" -ForegroundColor White
Write-Host ""
Write-Host "이 태그를 생성하고 push하면 GitHub Actions가 자동으로:" -ForegroundColor Cyan
Write-Host "  1. Windows Installer 빌드" -ForegroundColor Gray
Write-Host "  2. GitHub Release 생성" -ForegroundColor Gray
Write-Host "  3. Installer를 Release에 첨부" -ForegroundColor Gray
Write-Host ""

if ($DryRun) {
    Write-Host "[DRY RUN] 실제 실행 없이 미리보기 모드입니다." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "실행될 명령어:" -ForegroundColor Cyan
    Write-Host "  git tag -a $tag -m ""Release $tag""" -ForegroundColor Gray
    Write-Host "  git push origin $tag" -ForegroundColor Gray
    Write-Host ""
    exit 0
}

$confirm = Read-Host "계속 진행하시겠습니까? (y/N)"
if ($confirm -ne 'y' -and $confirm -ne 'Y') {
    Write-Host "  취소되었습니다." -ForegroundColor Gray
    exit 0
}
Write-Host ""

# 태그 생성 및 Push
Write-Host "[5/5] 태그 생성 및 Push..." -ForegroundColor Yellow

# 태그 생성
git tag -a $tag -m "Release $tag"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 태그 생성 실패" -ForegroundColor Red
    exit 1
}
Write-Host "  ✅ 로컬 태그 생성 완료" -ForegroundColor Green

# 태그 Push
git push origin $tag
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 태그 Push 실패" -ForegroundColor Red
    Write-Host ""
    Write-Host "로컬 태그를 삭제하려면:" -ForegroundColor Yellow
    Write-Host "  git tag -d $tag" -ForegroundColor Gray
    exit 1
}
Write-Host "  ✅ 태그 Push 완료" -ForegroundColor Green
Write-Host ""

# 완료 메시지
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ✅ 릴리스 프로세스 시작!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "GitHub Actions에서 빌드가 진행됩니다:" -ForegroundColor Cyan
Write-Host "  $remoteUrl/actions" -ForegroundColor White
Write-Host ""
Write-Host "빌드 완료 후 릴리스 확인:" -ForegroundColor Cyan
Write-Host "  $remoteUrl/releases/tag/$tag" -ForegroundColor White
Write-Host ""
Write-Host "빌드는 약 5-10분 소요됩니다." -ForegroundColor Gray
Write-Host ""
