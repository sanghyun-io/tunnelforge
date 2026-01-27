<#
.SYNOPSIS
    TunnelDB Manager 버전을 자동으로 증가시키고 선택적으로 릴리스를 생성합니다.

.DESCRIPTION
    src/version.py의 버전을 Semantic Versioning 규칙에 따라 자동으로 증가시킵니다.
    -AutoRelease 옵션을 사용하면 버전 업데이트, 커밋, 태그 생성, GitHub Push까지
    한 번에 처리하여 GitHub Actions를 통한 자동 빌드 및 릴리스를 트리거합니다.

.PARAMETER Type
    버전 증가 타입을 지정합니다.
    - patch: 패치 버전 증가 (1.0.0 → 1.0.1) - 버그 수정
    - minor: 마이너 버전 증가 (1.0.0 → 1.1.0) - 새 기능 추가
    - major: 메이저 버전 증가 (1.0.0 → 2.0.0) - 큰 변경사항

.PARAMETER AutoRelease
    버전 증가 후 자동으로 Git 커밋, Push, 태그 생성 및 릴리스를 진행합니다.
    이 옵션을 사용하면 GitHub Actions가 자동으로 트리거되어 빌드 및 릴리스가 생성됩니다.

.PARAMETER DryRun
    실제로 파일을 변경하지 않고 미리보기만 수행합니다.
    어떤 버전으로 변경되는지, 어떤 명령어가 실행될지 확인할 수 있습니다.

.EXAMPLE
    .\scripts\bump-version.ps1 -Type patch

    패치 버전을 증가시킵니다 (1.0.0 → 1.0.1).
    파일만 업데이트하고 Git 작업은 수동으로 진행해야 합니다.

.EXAMPLE
    .\scripts\bump-version.ps1 -Type patch -AutoRelease

    패치 버전을 증가시키고 자동으로 릴리스를 생성합니다.
    버전 증가, 커밋, Push, 태그 생성까지 모두 자동으로 처리됩니다.

.EXAMPLE
    .\scripts\bump-version.ps1 -Type minor -AutoRelease

    마이너 버전을 증가시키고 (1.0.1 → 1.1.0) 자동으로 릴리스를 생성합니다.

.EXAMPLE
    .\scripts\bump-version.ps1 -Type major -DryRun

    메이저 버전 증가를 미리보기로 확인합니다.
    실제로 파일을 변경하지 않고 어떻게 바뀔지만 확인합니다.

.NOTES
    파일명: bump-version.ps1
    작성자: TunnelDB Manager Team
    요구사항: Git 설치 필요

.LINK
    https://github.com/sanghyun-io/db-connector
#>

param(
    [Parameter(HelpMessage="버전 증가 타입 (major, minor, patch)")]
    [ValidateSet("major", "minor", "patch")]
    [string]$Type,

    [Parameter(HelpMessage="버전 증가 후 자동으로 커밋, Push, 릴리스 생성")]
    [switch]$AutoRelease = $false,

    [Parameter(HelpMessage="실제 실행 없이 미리보기만 수행")]
    [switch]$DryRun = $false,

    [Alias("h")]
    [Parameter(HelpMessage="사용법 출력")]
    [switch]$Help = $false
)

# Help 출력
if ($Help -or -not $Type) {
    Write-Host ""
    Write-Host "TunnelDB Manager - Version Bump" -ForegroundColor Cyan
    Write-Host "버전을 자동으로 증가시키고 선택적으로 릴리스를 생성합니다." -ForegroundColor Gray
    Write-Host ""
    Write-Host "사용법:" -ForegroundColor Yellow
    Write-Host "  .\scripts\bump-version.ps1 -Type <patch|minor|major> [-AutoRelease] [-DryRun]" -ForegroundColor White
    Write-Host ""
    Write-Host "옵션:" -ForegroundColor Yellow
    Write-Host "  -Type <type>     버전 증가 타입 (필수)" -ForegroundColor White
    Write-Host "                   - patch: 1.0.0 → 1.0.1 (버그 수정)" -ForegroundColor Gray
    Write-Host "                   - minor: 1.0.0 → 1.1.0 (기능 추가)" -ForegroundColor Gray
    Write-Host "                   - major: 1.0.0 → 2.0.0 (큰 변경)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  -AutoRelease     버전 증가 + 커밋 + Push + 릴리스 (자동화)" -ForegroundColor White
    Write-Host "  -DryRun          실제 실행 없이 미리보기" -ForegroundColor White
    Write-Host "  -Help, -h        이 도움말 출력" -ForegroundColor White
    Write-Host ""
    Write-Host "예제:" -ForegroundColor Yellow
    Write-Host "  # 패치 버전 증가 + 자동 릴리스 (권장)" -ForegroundColor Gray
    Write-Host "  .\scripts\bump-version.ps1 -Type patch -AutoRelease" -ForegroundColor Green
    Write-Host ""
    Write-Host "  # 마이너 버전 증가 (파일만 업데이트)" -ForegroundColor Gray
    Write-Host "  .\scripts\bump-version.ps1 -Type minor" -ForegroundColor White
    Write-Host ""
    Write-Host "  # 미리보기" -ForegroundColor Gray
    Write-Host "  .\scripts\bump-version.ps1 -Type patch -DryRun" -ForegroundColor White
    Write-Host ""
    exit 0
}

# 스크립트 위치 기준으로 프로젝트 루트 경로 설정
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " TunnelDB Manager - Version Bump" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# version.py 파일 경로
$versionPy = "src\version.py"
if (-not (Test-Path $versionPy)) {
    Write-Host "❌ $versionPy 파일을 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

# 현재 버전 읽기
Write-Host "[1/4] 현재 버전 읽기..." -ForegroundColor Yellow

$versionContent = Get-Content $versionPy -Raw
if ($versionContent -match '__version__\s*=\s*[''"]([^''"]+)[''"]') {
    $currentVersion = $matches[1]
    Write-Host "  현재 버전: $currentVersion" -ForegroundColor Gray
} else {
    Write-Host "  ❌ version.py에서 버전을 추출할 수 없습니다." -ForegroundColor Red
    exit 1
}

# 버전 파싱
if ($currentVersion -match '^(\d+)\.(\d+)\.(\d+)$') {
    $major = [int]$matches[1]
    $minor = [int]$matches[2]
    $patch = [int]$matches[3]
} else {
    Write-Host "  ❌ 버전 형식이 올바르지 않습니다: $currentVersion" -ForegroundColor Red
    Write-Host "  예상 형식: major.minor.patch (예: 1.0.0)" -ForegroundColor Yellow
    exit 1
}

# 새 버전 계산
Write-Host ""
Write-Host "[2/4] 새 버전 계산..." -ForegroundColor Yellow

switch ($Type) {
    "major" {
        $major++
        $minor = 0
        $patch = 0
    }
    "minor" {
        $minor++
        $patch = 0
    }
    "patch" {
        $patch++
    }
}

$newVersion = "$major.$minor.$patch"
Write-Host "  새 버전: $newVersion" -ForegroundColor Green
Write-Host "  변경 타입: $Type" -ForegroundColor Gray
Write-Host ""

# Dry Run 모드
if ($DryRun) {
    Write-Host "[DRY RUN] 실제 실행 없이 미리보기 모드입니다." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "변경 사항:" -ForegroundColor Cyan
    Write-Host "  $currentVersion → $newVersion" -ForegroundColor White
    Write-Host ""
    Write-Host "업데이트될 파일:" -ForegroundColor Cyan
    Write-Host "  - $versionPy" -ForegroundColor Gray
    Write-Host ""

    if ($AutoRelease) {
        Write-Host "실행될 명령어:" -ForegroundColor Cyan
        Write-Host "  git add $versionPy" -ForegroundColor Gray
        Write-Host "  git commit -m ""Bump version to $newVersion""" -ForegroundColor Gray
        Write-Host "  git push origin main" -ForegroundColor Gray
        Write-Host "  git tag v$newVersion" -ForegroundColor Gray
        Write-Host "  git push origin v$newVersion" -ForegroundColor Gray
        Write-Host ""
    }

    exit 0
}

# 버전 업데이트 확인
Write-Host "[3/4] 버전 업데이트 확인" -ForegroundColor Yellow
Write-Host ""
Write-Host "  현재: $currentVersion" -ForegroundColor White
Write-Host "  새 버전: $newVersion" -ForegroundColor Green
Write-Host "  타입: $Type" -ForegroundColor White
Write-Host ""

$confirm = Read-Host "버전을 업데이트하시겠습니까? (y/N)"
if ($confirm -ne 'y' -and $confirm -ne 'Y') {
    Write-Host "  취소되었습니다." -ForegroundColor Gray
    exit 0
}
Write-Host ""

# 파일 업데이트
Write-Host "[4/4] 파일 업데이트 중..." -ForegroundColor Yellow

$newContent = $versionContent -replace '__version__\s*=\s*[''"]([^''"]+)[''"]', "__version__ = `"$newVersion`""
Set-Content -Path $versionPy -Value $newContent -NoNewline

Write-Host "  ✅ $versionPy 업데이트 완료" -ForegroundColor Green
Write-Host ""

# Git 상태 표시
git diff $versionPy

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ✅ 버전 업데이트 완료!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "업데이트된 버전: $newVersion" -ForegroundColor White
Write-Host ""

# 자동 릴리스
if ($AutoRelease) {
    Write-Host "자동 릴리스 프로세스를 시작합니다..." -ForegroundColor Cyan
    Write-Host ""

    # Git 확인
    $gitCheck = git --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ Git이 설치되지 않았습니다." -ForegroundColor Red
        exit 1
    }

    # 커밋
    Write-Host "[릴리스 1/3] 변경사항 커밋..." -ForegroundColor Yellow
    git add $versionPy
    git commit -m "Bump version to $newVersion"

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ 커밋 실패" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ 커밋 완료" -ForegroundColor Green
    Write-Host ""

    # Push
    Write-Host "[릴리스 2/3] main 브랜치 push..." -ForegroundColor Yellow
    git push origin main

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ Push 실패" -ForegroundColor Red
        exit 1
    }
    Write-Host "  ✅ Push 완료" -ForegroundColor Green
    Write-Host ""

    # 태그 생성 및 Push
    Write-Host "[릴리스 3/3] 릴리스 태그 생성 및 push..." -ForegroundColor Yellow

    $tag = "v$newVersion"
    git tag -a $tag -m "Release $tag"

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ 태그 생성 실패" -ForegroundColor Red
        exit 1
    }

    git push origin $tag

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ 태그 Push 실패" -ForegroundColor Red
        Write-Host ""
        Write-Host "로컬 태그를 삭제하려면:" -ForegroundColor Yellow
        Write-Host "  git tag -d $tag" -ForegroundColor Gray
        exit 1
    }

    Write-Host "  ✅ 릴리스 태그 push 완료" -ForegroundColor Green
    Write-Host ""

    # 완료 메시지
    $remoteUrl = git remote get-url origin 2>&1

    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host " ✅ 자동 릴리스 완료!" -ForegroundColor Green
    Write-Host "========================================" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "버전: $newVersion" -ForegroundColor White
    Write-Host "태그: $tag" -ForegroundColor White
    Write-Host ""
    Write-Host "GitHub Actions에서 빌드가 진행됩니다:" -ForegroundColor Cyan
    Write-Host "  $remoteUrl/actions" -ForegroundColor White
    Write-Host ""
    Write-Host "빌드 완료 후 릴리스 확인:" -ForegroundColor Cyan
    Write-Host "  $remoteUrl/releases/tag/$tag" -ForegroundColor White
    Write-Host ""
    Write-Host "빌드는 약 5-10분 소요됩니다." -ForegroundColor Gray
    Write-Host ""
} else {
    Write-Host "다음 단계:" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "1. 변경사항 확인:" -ForegroundColor White
    Write-Host "   git status" -ForegroundColor Gray
    Write-Host ""
    Write-Host "2. 커밋 및 Push:" -ForegroundColor White
    Write-Host "   git add $versionPy" -ForegroundColor Gray
    Write-Host "   git commit -m ""Bump version to $newVersion""" -ForegroundColor Gray
    Write-Host "   git push origin main" -ForegroundColor Gray
    Write-Host ""
    Write-Host "3. 릴리스 생성:" -ForegroundColor White
    Write-Host "   .\scripts\create-release.ps1" -ForegroundColor Gray
    Write-Host ""
    Write-Host "또는 자동 릴리스:" -ForegroundColor White
    Write-Host "   .\scripts\bump-version.ps1 -Type $Type -AutoRelease" -ForegroundColor Gray
    Write-Host ""
}
