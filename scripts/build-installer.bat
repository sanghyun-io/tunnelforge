@echo off
REM TunnelDB Manager - Windows Installer 빌드 스크립트 (BAT 래퍼)
REM
REM 이 스크립트는 PowerShell 스크립트를 실행합니다.
REM
REM 사용 방법:
REM   .\scripts\build-installer.bat
REM   .\scripts\build-installer.bat -Clean
REM   .\scripts\build-installer.bat -SkipPyInstaller

setlocal

REM 프로젝트 루트 디렉토리로 이동
cd /d "%~dp0\.."

REM PowerShell 스크립트 실행
powershell -ExecutionPolicy Bypass -File "scripts\build-installer.ps1" %*

endlocal
