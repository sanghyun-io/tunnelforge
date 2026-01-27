<#
.SYNOPSIS
    TunnelDB Manager Windows Installer를 로컬에서 빌드합니다.

.DESCRIPTION
    이 스크립트는 PyInstaller와 Inno Setup을 사용하여 Windows 설치 프로그램을 생성합니다.

    빌드 프로세스:
    1. src/version.py에서 버전 읽기
    2. PyInstaller로 실행 파일(.exe) 빌드
    3. 버전을 installer/TunnelDBManager.iss에 동기화
    4. Inno Setup으로 Windows Installer(.exe) 생성

    출력 파일:
    - dist\TunnelDBManager.exe (실행 파일)
    - output\TunnelDBManager-Setup-{version}.exe (설치 프로그램)

.PARAMETER Clean
    빌드 전에 이전 빌드 디렉토리(build, dist, output)를 삭제합니다.
    깨끗한 상태에서 빌드를 시작하려면 이 옵션을 사용하세요.

.PARAMETER SkipPyInstaller
    PyInstaller 빌드를 건너뛰고 기존 EXE로 Installer만 생성합니다.
    EXE가 이미 빌드되어 있고 Installer만 다시 만들 때 유용합니다.

.EXAMPLE
    .\scripts\build-installer.ps1

    기본 빌드를 수행합니다. PyInstaller로 EXE를 빌드한 후 Installer를 생성합니다.

.EXAMPLE
    .\scripts\build-installer.ps1 -Clean

    이전 빌드 파일을 모두 삭제한 후 깨끗한 상태에서 빌드합니다.

.EXAMPLE
    .\scripts\build-installer.ps1 -SkipPyInstaller

    PyInstaller 빌드를 건너뛰고 기존 EXE로 Installer만 생성합니다.
    EXE가 이미 dist\TunnelDBManager.exe에 있어야 합니다.

.EXAMPLE
    .\scripts\build-installer.ps1 -Clean -SkipPyInstaller

    이전 output 디렉토리를 삭제하고 기존 EXE로 Installer만 생성합니다.

.NOTES
    파일명: build-installer.ps1
    작성자: TunnelDB Manager Team
    요구사항:
    - Python 3.9+
    - PyInstaller (pip install -e ".[dev]"로 설치)
    - Inno Setup 6 (https://jrsoftware.org/isinfo.php)

    참고:
    - GitHub Actions는 자동으로 빌드를 수행하므로 로컬 테스트용으로만 사용하세요.
    - 릴리스는 bump-version.ps1 -AutoRelease로 자동화할 수 있습니다.

.LINK
    https://github.com/sanghyun-io/db-connector
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
    Write-Host "TunnelDB Manager - Installer Builder" -ForegroundColor Cyan
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
    Write-Host "  1. src/version.py에서 버전 읽기" -ForegroundColor Gray
    Write-Host "  2. PyInstaller로 EXE 빌드" -ForegroundColor Gray
    Write-Host "  3. Inno Setup으로 Windows Installer 생성" -ForegroundColor Gray
    Write-Host ""
    Write-Host "출력:" -ForegroundColor Yellow
    Write-Host "  - dist\TunnelDBManager.exe                    (실행 파일)" -ForegroundColor Gray
    Write-Host "  - output\TunnelDBManager-Setup-{version}.exe  (설치 프로그램)" -ForegroundColor Gray
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
Write-Host " TunnelDB Manager - Installer Builder" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Clean 옵션: 빌드 디렉토리 삭제
if ($Clean) {
    Write-Host "[1/5] 이전 빌드 파일 정리 중..." -ForegroundColor Yellow

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

# PyInstaller로 EXE 빌드
if (-not $SkipPyInstaller) {
    Write-Host "[2/5] PyInstaller로 실행 파일 빌드 중..." -ForegroundColor Yellow

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
    if (-not (Test-Path "dist\TunnelDBManager.exe")) {
        Write-Host "  ❌ dist\TunnelDBManager.exe 파일을 찾을 수 없습니다." -ForegroundColor Red
        exit 1
    }

    $exeSize = (Get-Item "dist\TunnelDBManager.exe").Length / 1MB
    Write-Host "  ✅ EXE 빌드 완료: dist\TunnelDBManager.exe ($($exeSize.ToString('0.0')) MB)" -ForegroundColor Green
    Write-Host ""
} else {
    Write-Host "[2/5] PyInstaller 빌드 건너뛰기 (-SkipPyInstaller)" -ForegroundColor Gray

    # EXE 파일 존재 확인
    if (-not (Test-Path "dist\TunnelDBManager.exe")) {
        Write-Host "  ❌ dist\TunnelDBManager.exe 파일을 찾을 수 없습니다." -ForegroundColor Red
        Write-Host "  먼저 PyInstaller로 빌드하거나 -SkipPyInstaller 옵션을 제거하세요." -ForegroundColor Yellow
        exit 1
    }

    Write-Host "  기존 EXE 사용: dist\TunnelDBManager.exe" -ForegroundColor Gray
    Write-Host ""
}

# Inno Setup 경로 찾기
Write-Host "[3/5] Inno Setup 확인 중..." -ForegroundColor Yellow

$InnoSetupPaths = @(
    "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    "C:\Program Files\Inno Setup 6\ISCC.exe",
    "$env:ProgramFiles\Inno Setup 6\ISCC.exe",
    "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe"
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

# version.py에서 버전 추출 및 동기화
Write-Host "[4/4] 버전 정보 동기화 중..." -ForegroundColor Yellow

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

# TunnelDBManager.iss 파일 업데이트
$issFile = "installer\TunnelDBManager.iss"
if (-not (Test-Path $issFile)) {
    Write-Host "  ❌ $issFile 파일을 찾을 수 없습니다." -ForegroundColor Red
    exit 1
}

$issContent = Get-Content $issFile -Raw
$issContent = $issContent -replace '#define MyAppVersion ".*"', "#define MyAppVersion `"$version`""
Set-Content -Path $issFile -Value $issContent -NoNewline

Write-Host "  ✅ $issFile 버전 업데이트 완료: $version" -ForegroundColor Green
Write-Host ""

# Inno Setup으로 Installer 컴파일
Write-Host "[5/5] Windows Installer 생성 중..." -ForegroundColor Yellow

& $ISCC "installer\TunnelDBManager.iss"

if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ Installer 빌드 실패" -ForegroundColor Red
    exit 1
}

# Installer 파일 확인
$installerPattern = "output\TunnelDBManager-Setup-*.exe"
$installerFile = Get-Item $installerPattern -ErrorAction SilentlyContinue | Select-Object -First 1

if (-not $installerFile) {
    Write-Host "  ❌ Installer 파일을 찾을 수 없습니다: $installerPattern" -ForegroundColor Red
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
Write-Host "  📦 EXE: dist\TunnelDBManager.exe" -ForegroundColor White
Write-Host "  📦 Installer: $($installerFile.FullName)" -ForegroundColor White
Write-Host ""
Write-Host "설치 프로그램 테스트:" -ForegroundColor Cyan
Write-Host "  .\$($installerFile.FullName)" -ForegroundColor Gray
Write-Host ""
