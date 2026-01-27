<#
.SYNOPSIS
    TunnelDB Manager 부트스트래퍼 빌드 스크립트

.DESCRIPTION
    경량 온라인 설치 프로그램(부트스트래퍼)을 PyInstaller로 빌드합니다.
    출력: dist/TunnelDBManager-WebSetup.exe (~5-8MB)

.PARAMETER Clean
    빌드 전 이전 빌드 파일 정리

.EXAMPLE
    .\scripts\build-bootstrapper.ps1
    .\scripts\build-bootstrapper.ps1 -Clean
#>

param(
    [switch]$Clean
)

$ErrorActionPreference = "Stop"

# 프로젝트 루트 디렉토리
$ProjectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  TunnelDB Manager Bootstrapper Build" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# 작업 디렉토리 변경
Push-Location $ProjectRoot

try {
    # Clean 옵션
    if ($Clean) {
        Write-Host "[1/4] Cleaning previous build..." -ForegroundColor Yellow

        if (Test-Path "build") {
            Remove-Item -Recurse -Force "build"
            Write-Host "  - Removed build/" -ForegroundColor Gray
        }
        if (Test-Path "dist/TunnelDBManager-WebSetup.exe") {
            Remove-Item -Force "dist/TunnelDBManager-WebSetup.exe"
            Write-Host "  - Removed dist/TunnelDBManager-WebSetup.exe" -ForegroundColor Gray
        }
        Write-Host ""
    }

    # 가상환경 확인
    Write-Host "[2/4] Checking environment..." -ForegroundColor Yellow

    if (-not (Test-Path ".venv\Scripts\python.exe")) {
        Write-Host "  ERROR: Virtual environment not found!" -ForegroundColor Red
        Write-Host "  Run: python -m venv .venv && .venv\Scripts\activate && pip install -e .[dev]" -ForegroundColor Gray
        exit 1
    }

    $Python = ".venv\Scripts\python.exe"

    # PyInstaller 확인
    $PyInstallerCheck = & $Python -c "import PyInstaller; print('OK')" 2>$null
    if ($PyInstallerCheck -ne "OK") {
        Write-Host "  Installing PyInstaller..." -ForegroundColor Gray
        & $Python -m pip install pyinstaller --quiet
    }
    Write-Host "  - Python: $Python" -ForegroundColor Gray
    Write-Host ""

    # 의존성 확인
    Write-Host "[3/4] Checking dependencies..." -ForegroundColor Yellow
    & $Python -c "import requests; print('  - requests: OK')"
    Write-Host ""

    # PyInstaller 빌드
    Write-Host "[4/4] Building bootstrapper..." -ForegroundColor Yellow
    Write-Host ""

    & $Python -m PyInstaller bootstrapper/bootstrapper.spec --noconfirm

    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller build failed!"
    }

    Write-Host ""

    # 결과 확인
    $OutputFile = "dist\TunnelDBManager-WebSetup.exe"
    if (Test-Path $OutputFile) {
        $FileInfo = Get-Item $OutputFile
        $FileSizeMB = [math]::Round($FileInfo.Length / 1MB, 2)

        Write-Host "======================================" -ForegroundColor Green
        Write-Host "  Build Successful!" -ForegroundColor Green
        Write-Host "======================================" -ForegroundColor Green
        Write-Host ""
        Write-Host "  Output: $OutputFile" -ForegroundColor White
        Write-Host "  Size:   $FileSizeMB MB" -ForegroundColor White
        Write-Host ""
    }
    else {
        throw "Output file not found: $OutputFile"
    }
}
catch {
    Write-Host ""
    Write-Host "ERROR: $_" -ForegroundColor Red
    exit 1
}
finally {
    Pop-Location
}
