<#
.SYNOPSIS
    TunnelDB Manager - GitHub 최신 릴리스와 비교하여 스마트하게 릴리스를 생성합니다.

.DESCRIPTION
    GitHub API를 통해 원격 저장소의 최신 릴리스 버전을 확인하고 로컬 버전과 비교합니다.

    시나리오:
    1. 버전 동일: 사용자가 patch/minor/major 선택하여 버전 증가 후 릴리스
    2. 로컬 버전 높음: 확인 후 현재 버전으로 릴리스
    3. 원격 버전 높음: 경고 후 종료 (로컬 업데이트 필요)

    버전 업데이트부터 커밋, 태그 생성, GitHub Push까지 자동으로 처리하여
    GitHub Actions를 통한 자동 빌드 및 릴리스를 트리거합니다.

.PARAMETER DryRun
    실제로 파일을 변경하지 않고 미리보기만 수행합니다.
    어떤 버전으로 변경되는지, 어떤 명령어가 실행될지 확인할 수 있습니다.

.EXAMPLE
    .\scripts\smart-release.ps1

    GitHub 최신 릴리스와 비교하여 스마트하게 릴리스를 생성합니다.
    버전이 동일하면 증가 타입을 선택하고, 로컬이 높으면 확인 후 릴리스합니다.

.EXAMPLE
    .\scripts\smart-release.ps1 -DryRun

    실제 실행 없이 미리보기로 확인합니다.
    실제로 파일을 변경하지 않고 어떻게 바뀔지만 확인합니다.

.NOTES
    파일명: smart-release.ps1
    작성자: TunnelDB Manager Team
    요구사항: Git 설치 필요, GitHub 원격 저장소 설정 필요

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
    Write-Host "TunnelDB Manager - Smart Release" -ForegroundColor Cyan
    Write-Host "GitHub 최신 릴리스와 비교하여 스마트하게 릴리스를 생성합니다." -ForegroundColor Gray
    Write-Host ""
    Write-Host "사용법:" -ForegroundColor Yellow
    Write-Host "  .\scripts\smart-release.ps1 [-DryRun]" -ForegroundColor White
    Write-Host ""
    Write-Host "옵션:" -ForegroundColor Yellow
    Write-Host "  -DryRun          실제 실행 없이 미리보기" -ForegroundColor White
    Write-Host "  -Help, -h        이 도움말 출력" -ForegroundColor White
    Write-Host ""
    Write-Host "동작 방식:" -ForegroundColor Yellow
    Write-Host "  1. GitHub API로 최신 릴리스 버전 확인" -ForegroundColor Gray
    Write-Host "  2. 로컬 버전과 비교" -ForegroundColor Gray
    Write-Host ""
    Write-Host "  시나리오:" -ForegroundColor Gray
    Write-Host "  - 버전 동일: 증가 타입(patch/minor/major) 선택" -ForegroundColor Gray
    Write-Host "  - 로컬 높음: 확인 후 현재 버전으로 릴리스" -ForegroundColor Gray
    Write-Host "  - 원격 높음: 경고 후 종료" -ForegroundColor Gray
    Write-Host ""
    Write-Host "예제:" -ForegroundColor Yellow
    Write-Host "  # 스마트 릴리스 (권장)" -ForegroundColor Gray
    Write-Host "  .\scripts\smart-release.ps1" -ForegroundColor Green
    Write-Host ""
    Write-Host "  # 미리보기" -ForegroundColor Gray
    Write-Host "  .\scripts\smart-release.ps1 -DryRun" -ForegroundColor White
    Write-Host ""
    exit 0
}

# 버전 비교 함수
function Compare-Version {
    param(
        [string]$Version1,
        [string]$Version2
    )

    $v1 = $Version1 -replace '^v', '' -split '\.'
    $v2 = $Version2 -replace '^v', '' -split '\.'

    for ($i = 0; $i -lt 3; $i++) {
        $n1 = [int]$v1[$i]
        $n2 = [int]$v2[$i]

        if ($n1 -gt $n2) { return 1 }
        if ($n1 -lt $n2) { return -1 }
    }

    return 0
}

# 스크립트 위치 기준으로 프로젝트 루트 경로 설정
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " TunnelDB Manager - Smart Release" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# version.py 파일 경로
$versionPy = "src\version.py"
if (-not (Test-Path $versionPy)) {
    Write-Host "  [X] $versionPy 파일을 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

# 현재 버전 읽기
Write-Host "[1/6] 로컬 버전 읽기..." -ForegroundColor Yellow

$versionContent = Get-Content $versionPy -Raw
if ($versionContent -match '__version__\s*=\s*[''"]([^''"]+)[''"]') {
    $localVersion = $matches[1]
    Write-Host "  로컬 버전: $localVersion" -ForegroundColor White
} else {
    Write-Host "  [X] version.py에서 버전을 추출할 수 없습니다." -ForegroundColor Red
    exit 1
}

# 버전 파싱
if ($localVersion -match '^(\d+)\.(\d+)\.(\d+)$') {
    $major = [int]$matches[1]
    $minor = [int]$matches[2]
    $patch = [int]$matches[3]
} else {
    Write-Host "  [X] 버전 형식이 올바르지 않습니다: $localVersion" -ForegroundColor Red
    Write-Host "  예상 형식: major.minor.patch (예: 1.0.0)" -ForegroundColor Yellow
    exit 1
}
Write-Host ""

# GitHub 원격 저장소 정보 추출
Write-Host "[2/6] GitHub 정보 확인..." -ForegroundColor Yellow

$remoteUrl = git remote get-url origin 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [X] 원격 저장소(origin)가 설정되지 않았습니다." -ForegroundColor Red
    exit 1
}

# GitHub owner/repo 추출
if ($remoteUrl -match 'github\.com[:/]([^/]+)/(.+?)(\.git)?$') {
    $owner = $matches[1]
    $repo = $matches[2] -replace '\.git$', ''
    Write-Host "  Repository: $owner/$repo" -ForegroundColor White
} else {
    Write-Host "  [X] GitHub 저장소 URL을 파싱할 수 없습니다." -ForegroundColor Red
    Write-Host "  URL: $remoteUrl" -ForegroundColor Gray
    exit 1
}
Write-Host ""

# GitHub API로 최신 릴리스 확인
Write-Host "[3/6] GitHub 최신 릴리스 확인..." -ForegroundColor Yellow

$apiUrl = "https://api.github.com/repos/$owner/$repo/releases/latest"

try {
    $response = Invoke-RestMethod -Uri $apiUrl -Method Get -ErrorAction Stop
    $remoteVersion = $response.tag_name -replace '^v', ''
    Write-Host "  원격 버전: $remoteVersion" -ForegroundColor White
} catch {
    $statusCode = $_.Exception.Response.StatusCode.Value__

    if ($statusCode -eq 404) {
        Write-Host "  [!] GitHub에 릴리스가 없습니다." -ForegroundColor Yellow
        Write-Host "  첫 번째 릴리스를 생성합니다." -ForegroundColor Gray
        $remoteVersion = "0.0.0"
    } else {
        Write-Host "  [X] GitHub API 요청 실패 (HTTP $statusCode)" -ForegroundColor Red
        Write-Host "  $_" -ForegroundColor Gray
        exit 1
    }
}
Write-Host ""

# 버전 비교
Write-Host "[4/6] 버전 비교..." -ForegroundColor Yellow
Write-Host ""
Write-Host "  로컬:  $localVersion" -ForegroundColor Cyan
Write-Host "  원격:  $remoteVersion" -ForegroundColor Magenta
Write-Host ""

$comparison = Compare-Version -Version1 $localVersion -Version2 $remoteVersion

if ($comparison -eq 0) {
    # 동일: 어떻게 올릴지 선택
    Write-Host "  버전이 동일합니다. 새 릴리스를 만들려면 버전을 올려야 합니다." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "버전을 어떻게 올리시겠습니까?" -ForegroundColor Cyan
    Write-Host ""

    $parts = $localVersion -split '\.'
    $patchVersion = "$($parts[0]).$($parts[1]).$([int]$parts[2] + 1)"
    $minorVersion = "$($parts[0]).$([int]$parts[1] + 1).0"
    $majorVersion = "$([int]$parts[0] + 1).0.0"

    Write-Host "  [1] patch  ($localVersion -> $patchVersion)  - 버그 수정" -ForegroundColor White
    Write-Host "  [2] minor  ($localVersion -> $minorVersion)  - 기능 추가" -ForegroundColor White
    Write-Host "  [3] major  ($localVersion -> $majorVersion)  - 큰 변경" -ForegroundColor White
    Write-Host "  [0] 취소" -ForegroundColor DarkGray
    Write-Host ""

    $choice = Read-Host "선택 (0-3)"

    switch ($choice) {
        "1" {
            $newVersion = $patchVersion
            $bumpType = "patch"
        }
        "2" {
            $newVersion = $minorVersion
            $bumpType = "minor"
        }
        "3" {
            $newVersion = $majorVersion
            $bumpType = "major"
        }
        default {
            Write-Host ""
            Write-Host "취소되었습니다." -ForegroundColor Gray
            exit 0
        }
    }

    $needsBump = $true

} elseif ($comparison -gt 0) {
    # 로컬이 높음: 그대로 릴리스할지 확인
    Write-Host "  [OK] 로컬 버전이 앞섭니다." -ForegroundColor Green
    Write-Host ""
    Write-Host "로컬 버전 v$localVersion 으로 릴리스하시겠습니까?" -ForegroundColor Cyan
    Write-Host ""

    $confirm = Read-Host "(y/N)"

    if ($confirm -ne 'y' -and $confirm -ne 'Y') {
        Write-Host ""
        Write-Host "취소되었습니다." -ForegroundColor Gray
        exit 0
    }

    $newVersion = $localVersion
    $needsBump = $false

} else {
    # 원격이 높음: 경고
    Write-Host "  [X] 원격 버전이 로컬보다 높습니다!" -ForegroundColor Red
    Write-Host ""
    Write-Host "GitHub에 더 최신 릴리스($remoteVersion)가 있습니다." -ForegroundColor Yellow
    Write-Host "로컬 저장소를 업데이트하거나, src/version.py를 수정하세요." -ForegroundColor Yellow
    Write-Host ""
    exit 1
}

Write-Host ""

# Dry Run 모드
if ($DryRun) {
    Write-Host "[DRY RUN] 실제 실행 없이 미리보기 모드입니다." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "변경 사항:" -ForegroundColor Cyan

    if ($needsBump) {
        Write-Host "  $localVersion -> $newVersion ($bumpType)" -ForegroundColor White
        Write-Host ""
        Write-Host "업데이트될 파일:" -ForegroundColor Cyan
        Write-Host "  - $versionPy" -ForegroundColor Gray
    } else {
        Write-Host "  버전 변경 없음 (현재: $newVersion)" -ForegroundColor White
    }

    Write-Host ""
    Write-Host "실행될 명령어:" -ForegroundColor Cyan

    if ($needsBump) {
        Write-Host "  git add $versionPy" -ForegroundColor Gray
        Write-Host "  git commit -m `"Bump version to $newVersion`"" -ForegroundColor Gray
    }

    Write-Host "  git push origin main" -ForegroundColor Gray
    Write-Host "  git tag v$newVersion" -ForegroundColor Gray
    Write-Host "  git push origin v$newVersion" -ForegroundColor Gray
    Write-Host ""

    exit 0
}

# 버전 업데이트 (필요한 경우에만)
if ($needsBump) {
    Write-Host "[5/6] 버전 업데이트 중..." -ForegroundColor Yellow

    $newContent = $versionContent -replace "__version__\s*=\s*[`"']([^`"']+)[`"']", "__version__ = `"$newVersion`""
    Set-Content -Path $versionPy -Value $newContent -NoNewline

    Write-Host "  [OK] $versionPy 업데이트 완료" -ForegroundColor Green
    Write-Host ""

    # Git diff 표시
    git diff $versionPy
    Write-Host ""
} else {
    Write-Host "[5/6] 버전 업데이트 건너뛰기 (이미 v$newVersion)" -ForegroundColor Yellow
    Write-Host ""
}

# 자동 릴리스
Write-Host "[6/6] 자동 릴리스 시작..." -ForegroundColor Yellow
Write-Host ""

# Git 확인
$gitCheck = git --version 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Host "  [X] Git이 설치되지 않았습니다." -ForegroundColor Red
    exit 1
}

# 커밋 (버전 업데이트가 있는 경우에만)
if ($needsBump) {
    Write-Host "  [릴리스 1/3] 변경사항 커밋..." -ForegroundColor Cyan
    git add $versionPy
    git commit -m "Bump version to $newVersion"

    if ($LASTEXITCODE -ne 0) {
        Write-Host "    [X] 커밋 실패" -ForegroundColor Red
        exit 1
    }
    Write-Host "    [OK] 커밋 완료" -ForegroundColor Green
    Write-Host ""
}

# Push
Write-Host "  [릴리스 2/3] main 브랜치 push..." -ForegroundColor Cyan

if ($needsBump) {
    git push origin main

    if ($LASTEXITCODE -ne 0) {
        Write-Host "    [X] Push 실패" -ForegroundColor Red
        exit 1
    }
    Write-Host "    [OK] Push 완료" -ForegroundColor Green
} else {
    Write-Host "    [!] 변경사항 없음 - Push 건너뛰기" -ForegroundColor Yellow
}
Write-Host ""

# 태그 생성 및 Push
Write-Host "  [릴리스 3/3] 릴리스 태그 생성 및 push..." -ForegroundColor Cyan

$tag = "v$newVersion"

# 기존 태그 확인
$existingTag = git tag -l $tag 2>&1

if ($existingTag) {
    Write-Host "    [!] 태그 $tag 가 이미 존재합니다." -ForegroundColor Yellow
    Write-Host "    기존 태그를 덮어쓰시겠습니까? (y/N)" -ForegroundColor Yellow
    $overwrite = Read-Host

    if ($overwrite -ne 'y' -and $overwrite -ne 'Y') {
        Write-Host "    취소되었습니다." -ForegroundColor Gray
        exit 0
    }

    # 로컬 태그 삭제
    git tag -d $tag
    Write-Host "    [!] 로컬 태그 삭제됨" -ForegroundColor Yellow
}

git tag -a $tag -m "Release $tag"

if ($LASTEXITCODE -ne 0) {
    Write-Host "    [X] 태그 생성 실패" -ForegroundColor Red
    exit 1
}

git push origin $tag --force

if ($LASTEXITCODE -ne 0) {
    Write-Host "    [X] 태그 Push 실패" -ForegroundColor Red
    Write-Host ""
    Write-Host "로컬 태그를 삭제하려면:" -ForegroundColor Yellow
    Write-Host "  git tag -d $tag" -ForegroundColor Gray
    exit 1
}

Write-Host "    [OK] 릴리스 태그 push 완료" -ForegroundColor Green
Write-Host ""

# 완료 메시지
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " [OK] 스마트 릴리스 완료!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "버전: $newVersion" -ForegroundColor White
Write-Host "태그: $tag" -ForegroundColor White

if ($needsBump) {
    Write-Host "타입: $bumpType" -ForegroundColor White
}

Write-Host ""
Write-Host "GitHub Actions에서 빌드가 진행됩니다:" -ForegroundColor Cyan
Write-Host "  https://github.com/$owner/$repo/actions" -ForegroundColor White
Write-Host ""
Write-Host "빌드 완료 후 릴리스 확인:" -ForegroundColor Cyan
Write-Host "  https://github.com/$owner/$repo/releases/tag/$tag" -ForegroundColor White
Write-Host ""
Write-Host "빌드는 약 5-10분 소요됩니다." -ForegroundColor Gray
Write-Host ""
