<#
#############################################################################
# ⚠️  DO NOT DELETE - GitHub Actions 및 로컬 검증 스크립트
#
# 이 파일은 .github/workflows/release.yml에서 사용됩니다.
# Windows Installer 빌드를 위해 반드시 필요합니다.
#
# 실제 태그와 릴리스는 보호된 수동 GitHub Actions 워크플로를 사용합니다.
#############################################################################

.SYNOPSIS
    TunnelForge Windows Installer를 빌드하고 로컬 릴리스 후보를 검증합니다.

.DESCRIPTION
    이 스크립트는 PyInstaller와 Inno Setup을 사용하여 Windows 설치 프로그램을 생성합니다.

    빌드 프로세스:
    1. 세 릴리스 버전 파일의 일치 검증
    2. Rust tunnelforge-core DB service 빌드
    3. PyInstaller로 실행 파일(.exe) 빌드
    4. WebSetup bootstrapper 빌드 및 frozen self-check
    5. Inno Setup으로 Windows Installer(.exe) 생성

    출력 파일:
    - dist\TunnelForge\TunnelForge.exe (실행 파일)
    - dist\TunnelForge-WebSetup.exe (온라인 설치 및 복구 프로그램)
    - output\TunnelForge-Setup-{version}.exe (설치 프로그램)

.PARAMETER Clean
    빌드 전에 이전 빌드 디렉토리(build, dist, output)를 삭제합니다.
    깨끗한 상태에서 빌드를 시작하려면 이 옵션을 사용하세요.

.PARAMETER SkipPyInstaller
    PyInstaller 빌드를 건너뛰고 기존 본체 EXE와 WebSetup으로 Installer만 생성합니다.

.EXAMPLE
    .\scripts\build-installer.ps1

    기본 빌드를 수행합니다. PyInstaller로 EXE를 빌드한 후 Installer를 생성합니다.

.EXAMPLE
    .\scripts\build-installer.ps1 -Clean

    이전 빌드 파일을 모두 삭제한 후 깨끗한 상태에서 빌드합니다.

.EXAMPLE
    .\scripts\build-installer.ps1 -SkipPyInstaller

    PyInstaller 빌드를 건너뛰고 기존 EXE로 Installer만 생성합니다.
    본체 EXE와 dist\TunnelForge-WebSetup.exe가 이미 있어야 합니다.

.NOTES
    파일명: build-installer.ps1
    작성자: TunnelForge Team
    요구사항:
    - Python 3.9+
    - PyInstaller (pip install -e ".[dev]"로 설치)
    - Inno Setup 6 (https://jrsoftware.org/isinfo.php)

    참고:
    - 이 명령은 로컬 릴리스 후보 검증에도 사용합니다.
    - 태그 생성과 draft 릴리스는 보호된 수동 GitHub Actions 워크플로에서 수행합니다.

.LINK
    https://github.com/sanghyun-io/tunnelforge
#>

param(
    [Parameter(HelpMessage="빌드 전에 이전 빌드 디렉토리 삭제")]
    [switch]$Clean = $false,

    [Parameter(HelpMessage="PyInstaller 빌드를 건너뛰고 Installer만 생성")]
    [switch]$SkipPyInstaller = $false,

    [Alias("h")]
    [Parameter(HelpMessage="사용법 출력")]
    [switch]$Help = $false
)

# Help 출력
if ($Help) {
    Write-Host ""
    Write-Host "TunnelForge - Installer Builder" -ForegroundColor Cyan
    Write-Host "Windows Installer를 로컬에서 빌드합니다." -ForegroundColor Gray
    Write-Host ""
    Write-Host "사용법:" -ForegroundColor Yellow
    Write-Host "  .\scripts\build-installer.ps1 [-Clean] [-SkipPyInstaller]" -ForegroundColor White
    Write-Host ""
    Write-Host "옵션:" -ForegroundColor Yellow
    Write-Host "  -Clean           빌드 전에 이전 빌드 디렉토리 삭제" -ForegroundColor White
    Write-Host "  -SkipPyInstaller PyInstaller 빌드 건너뛰고 Installer만 생성" -ForegroundColor White
    Write-Host "  -Help, -h        이 도움말 출력" -ForegroundColor White
    Write-Host ""
    Write-Host "빌드 프로세스:" -ForegroundColor Yellow
    Write-Host "  1. 세 릴리스 버전 파일 일치 검증" -ForegroundColor Gray
    Write-Host "  2. Rust tunnelforge-core DB service 빌드" -ForegroundColor Gray
    Write-Host "  3. PyInstaller로 EXE 빌드" -ForegroundColor Gray
    Write-Host "  4. WebSetup bootstrapper 빌드 및 frozen self-check" -ForegroundColor Gray
    Write-Host "  5. Inno Setup으로 Windows Installer 생성" -ForegroundColor Gray
    Write-Host ""
    Write-Host "출력:" -ForegroundColor Yellow
    Write-Host "  - dist\TunnelForge\TunnelForge.exe                    (실행 파일)" -ForegroundColor Gray
    Write-Host "  - dist\TunnelForge-WebSetup.exe                       (온라인 설치 및 복구)" -ForegroundColor Gray
    Write-Host "  - output\TunnelForge-Setup-{version}.exe  (설치 프로그램)" -ForegroundColor Gray
    Write-Host ""
    Write-Host "예제:" -ForegroundColor Yellow
    Write-Host "  # 기본 빌드" -ForegroundColor Gray
    Write-Host "  .\scripts\build-installer.ps1" -ForegroundColor Green
    Write-Host ""
    Write-Host "  # 깨끗한 빌드" -ForegroundColor Gray
    Write-Host "  .\scripts\build-installer.ps1 -Clean" -ForegroundColor White
    Write-Host ""
    Write-Host "  # Installer만 다시 생성" -ForegroundColor Gray
    Write-Host "  .\scripts\build-installer.ps1 -SkipPyInstaller" -ForegroundColor White
    Write-Host ""
    Write-Host "요구사항:" -ForegroundColor Yellow
    Write-Host "  - Python 3.9+" -ForegroundColor Gray
    Write-Host "  - PyInstaller (pip install -e "".[dev]"")" -ForegroundColor Gray
    Write-Host "  - Inno Setup 6 (https://jrsoftware.org/isinfo.php)" -ForegroundColor Gray
    Write-Host ""
    exit 0
}

# 스크립트 위치 기준으로 프로젝트 루트 경로 설정
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $PSCommandPath)
Set-Location $ProjectRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host " TunnelForge - Installer Builder" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Clean 옵션: 빌드 디렉토리 삭제
if ($Clean) {
    Write-Host "[1/7] 이전 빌드 파일 정리 중..." -ForegroundColor Yellow

    if (Test-Path "build") {
        Remove-Item -Path "build" -Recurse -Force
        Write-Host "  ✅ build/ 디렉토리 삭제 완료" -ForegroundColor Green
    }

    if (Test-Path "dist") {
        Remove-Item -Path "dist" -Recurse -Force
        Write-Host "  ✅ dist/ 디렉토리 삭제 완료" -ForegroundColor Green
    }

    if (Test-Path "output") {
        Remove-Item -Path "output" -Recurse -Force
        Write-Host "  ✅ output/ 디렉토리 삭제 완료" -ForegroundColor Green
    }

    Write-Host ""
}

# Rust helper 빌드
if (-not $SkipPyInstaller) {
    Write-Host "[2/7] Rust tunnelforge-core DB service 빌드 중..." -ForegroundColor Yellow

    $cargoCheck = cargo --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ Cargo가 설치되지 않았습니다." -ForegroundColor Red
        Write-Host "  Rust 설치 후 다시 실행하세요: https://rustup.rs/" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  Cargo 버전: $cargoCheck" -ForegroundColor Gray
    cargo build --manifest-path migration_core\Cargo.toml --release

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ tunnelforge-core 빌드 실패" -ForegroundColor Red
        exit 1
    }

    if (-not (Test-Path "migration_core\target\release\tunnelforge-core.exe")) {
        Write-Host "  ❌ tunnelforge-core.exe 파일을 찾을 수 없습니다." -ForegroundColor Red
        exit 1
    }

    Write-Host "  ✅ tunnelforge-core DB service 빌드 완료" -ForegroundColor Green
    Write-Host ""

    Write-Host "[3/7] PyInstaller로 실행 파일 빌드 중..." -ForegroundColor Yellow

    # PyInstaller 설치 확인
    $pyinstallerCheck = python -m PyInstaller --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ PyInstaller가 설치되지 않았습니다." -ForegroundColor Red
        Write-Host "  설치: pip install -e "".[dev]""" -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  PyInstaller 버전: $pyinstallerCheck" -ForegroundColor Gray

    # PyInstaller 실행
    python -m PyInstaller tunnel-manager.spec

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ PyInstaller 빌드 실패" -ForegroundColor Red
        exit 1
    }

    # EXE 파일 존재 확인
    if (-not (Test-Path "dist\TunnelForge\TunnelForge.exe")) {
        Write-Host "  ❌ dist\TunnelForge\TunnelForge.exe 파일을 찾을 수 없습니다." -ForegroundColor Red
        exit 1
    }

    $exeSize = (Get-Item "dist\TunnelForge\TunnelForge.exe").Length / 1MB
    Write-Host "  ✅ EXE 빌드 완료: dist\TunnelForge\TunnelForge.exe ($($exeSize.ToString('0.0')) MB)" -ForegroundColor Green
    Write-Host ""

    Write-Host "[4/7] 온라인 설치 bootstrapper 빌드 중..." -ForegroundColor Yellow
    & "$PSScriptRoot\build-bootstrapper.ps1"

    if ($LASTEXITCODE -ne 0) {
        Write-Host "  ❌ TunnelForge-WebSetup.exe 빌드 실패" -ForegroundColor Red
        exit 1
    }
} else {
    Write-Host "[2-4/7] Rust helper/PyInstaller/bootstrapper 빌드 건너뛰기 (-SkipPyInstaller)" -ForegroundColor Gray

    # EXE 파일 존재 확인
    if (-not (Test-Path "dist\TunnelForge\TunnelForge.exe")) {
        Write-Host "  ❌ dist\TunnelForge\TunnelForge.exe 파일을 찾을 수 없습니다." -ForegroundColor Red
        Write-Host "  먼저 PyInstaller로 빌드하거나 -SkipPyInstaller 옵션을 제거하세요." -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  기존 EXE 사용: dist\TunnelForge\TunnelForge.exe" -ForegroundColor Gray
    Write-Host ""
}

$webSetupPath = "dist\TunnelForge-WebSetup.exe"
if (-not (Test-Path $webSetupPath)) {
    Write-Host "  ❌ $webSetupPath 파일을 찾을 수 없습니다." -ForegroundColor Red
    Write-Host "  먼저 전체 빌드를 실행하거나 -SkipPyInstaller 옵션을 제거하세요." -ForegroundColor Yellow
    exit 1
}

$webSetupSelfCheck = (& ".\$webSetupPath" --self-check 2>&1 | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or $webSetupSelfCheck -ne "TUNNELFORGE_WEBSETUP_SELF_CHECK_OK") {
    Write-Host "  ❌ TunnelForge-WebSetup.exe self-check 실패" -ForegroundColor Red
    exit 1
}
Write-Host "  ✅ TunnelForge-WebSetup.exe self-check 통과" -ForegroundColor Green
Write-Host ""

# Inno Setup 경로 찾기
Write-Host "[5/7] Inno Setup 확인 중..." -ForegroundColor Yellow

$InnoSetupPaths = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
    # Chocolatey 설치 경로 (GitHub Actions)
    "C:\ProgramData\chocolatey\lib\innosetup\tools\ISCC.exe",
    "$env:ChocolateyInstall\lib\innosetup\tools\ISCC.exe"
)

$ISCC = $null
foreach ($path in $InnoSetupPaths) {
    if (Test-Path $path) {
        $ISCC = $path
        break
    }
}

if (-not $ISCC) {
    Write-Host "  ❌ Inno Setup을 찾을 수 없습니다." -ForegroundColor Red
    Write-Host "  Inno Setup 6을 설치하세요: https://jrsoftware.org/isinfo.php" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  설치 후 다음 경로 중 하나에 위치해야 합니다:" -ForegroundColor Yellow
    foreach ($path in $InnoSetupPaths) {
        Write-Host "    - $path" -ForegroundColor Gray
    }
    exit 1
}

$innoVersion = & $ISCC /? 2>&1 | Select-String "Inno Setup" | Select-Object -First 1
Write-Host "  ✅ Inno Setup 발견: $ISCC" -ForegroundColor Green
Write-Host "  버전: $innoVersion" -ForegroundColor Gray
Write-Host ""

# 세 릴리스 버전 파일 일치 검증
Write-Host "[6/7] 버전 정보 검증 중..." -ForegroundColor Yellow

# version.py에서 버전 추출
$versionPy = "src\version.py"
if (-not (Test-Path $versionPy)) {
    Write-Host "  ❌ $versionPy 파일을 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

$versionContent = Get-Content $versionPy -Raw
if ($versionContent -match '__version__\s*=\s*[''"]([^''"]+)[''"]') {
    $version = $matches[1]
    Write-Host "  ✅ version.py에서 버전 추출: $version" -ForegroundColor Green
} else {
    Write-Host "  ❌ version.py에서 버전을 추출할 수 없습니다." -ForegroundColor Red
    exit 1
}

$projectContent = Get-Content "pyproject.toml" -Raw
if ($projectContent -match '(?m)^version\s*=\s*"([^"]+)"\s*$') {
    $projectVersion = $matches[1]
} else {
    Write-Host "  ❌ pyproject.toml 에서 project version을 추출할 수 없습니다." -ForegroundColor Red
    exit 1
}

# TunnelForge.iss 버전 검증
if (-not (Test-Path "installer\TunnelForge.iss")) {
    Write-Host "  ❌ installer\TunnelForge.iss 파일을 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

$issContent = Get-Content "installer\TunnelForge.iss" -Raw
if ($issContent -match '#define\s+MyAppVersion\s+"([^"]+)"') {
    $installerVersion = $matches[1]
} else {
    Write-Host "  ❌ installer\TunnelForge.iss 에서 MyAppVersion을 추출할 수 없습니다." -ForegroundColor Red
    exit 1
}

if ($projectVersion -ne $version -or $installerVersion -ne $version) {
    Write-Host "  ❌ 릴리스 버전 불일치: version.py=$version pyproject=$projectVersion installer=$installerVersion" -ForegroundColor Red
    Write-Host "  scripts\bump_version.py로 세 버전 파일을 먼저 동기화하세요." -ForegroundColor Yellow
    exit 1
}

Write-Host "  ✅ 릴리스 버전 일치: $version" -ForegroundColor Green
Write-Host ""

# Inno Setup으로 Installer 컴파일
Write-Host "[7/7] Windows Installer 생성 중..." -ForegroundColor Yellow

$crashLog = "dist\TunnelForge\crash.log"
if (Test-Path $crashLog) {
    Remove-Item -LiteralPath $crashLog -Force
    Write-Host "  ✅ 이전 crash.log 제거 완료" -ForegroundColor Green
}

& $ISCC "installer\TunnelForge.iss"

if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ Installer 빌드 실패" -ForegroundColor Red
    exit 1
}

# Installer 파일 확인
$installerPath = "output\TunnelForge-Setup-$version.exe"
$installerFile = Get-Item $installerPath -ErrorAction SilentlyContinue

if (-not $installerFile) {
    Write-Host "  ❌ Installer 파일을 찾을 수 없습니다: $installerPath" -ForegroundColor Red
    exit 1
}

$installerSize = $installerFile.Length / 1MB
Write-Host "  ✅ Installer 생성 완료: $($installerFile.Name) ($($installerSize.ToString('0.0')) MB)" -ForegroundColor Green
Write-Host ""

# 완료 메시지
Write-Host "========================================" -ForegroundColor Cyan
Write-Host " ✅ 빌드 완료!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "출력 파일:" -ForegroundColor Cyan
Write-Host "  📦 EXE: dist\TunnelForge\TunnelForge.exe" -ForegroundColor White
Write-Host "  📦 Installer: $($installerFile.FullName)" -ForegroundColor White
Write-Host ""
Write-Host "설치 프로그램 테스트:" -ForegroundColor Cyan
Write-Host "  $($installerFile.FullName)" -ForegroundColor Gray
Write-Host ""
