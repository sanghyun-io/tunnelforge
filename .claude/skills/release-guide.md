---
skill_name: release-guide
description: TunnelDB Manager 릴리스 프로세스 가이드 및 버전 관리 설명
version: 1.0.0
tags: [release, versioning, github-actions, automation]
---

# TunnelDB Manager - Release Guide

이 스킬은 TunnelDB Manager의 릴리스 프로세스와 버전 관리 방법을 설명합니다.

## 📋 버전 관리 원칙

### Single Source of Truth
- **`src/version.py`** - 유일한 버전 정보 소스
- 모든 버전 참조는 이 파일에서 가져옴
- Semantic Versioning 사용 (major.minor.patch)

### 버전 타입
- **patch** (1.0.0 → 1.0.1): 버그 수정
- **minor** (1.0.0 → 1.1.0): 새 기능 추가 (하위 호환)
- **major** (1.0.0 → 2.0.0): 큰 변경사항 (Breaking Changes)

---

## 🚀 릴리스 워크플로우

### 자동 릴리스 (권장)

**PowerShell / CMD:**
```powershell
# 패치 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type patch -AutoRelease

# 마이너 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type minor -AutoRelease

# 메이저 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type major -AutoRelease
```

**Git Bash / Linux / macOS:**
```bash
# 패치 버전 증가 + 자동 릴리스
./scripts/bump-version -Type patch -AutoRelease

# 마이너 버전 증가 + 자동 릴리스
./scripts/bump-version -Type minor -AutoRelease

# 메이저 버전 증가 + 자동 릴리스
./scripts/bump-version -Type major -AutoRelease
```

**이 명령 하나로:**
1. ✅ 버전 자동 증가
2. ✅ src/version.py 업데이트
3. ✅ Git 커밋
4. ✅ main 브랜치 Push
5. ✅ 태그 생성 (v{version})
6. ✅ 태그 Push
7. ✅ GitHub Actions 트리거
8. ✅ 자동 빌드 & 릴리스

---

## 🔍 미리보기 (DryRun)

실제 실행 전에 무엇이 바뀔지 확인:

**PowerShell:**
```powershell
.\scripts\bump-version.ps1 -Type patch -DryRun
```

**Bash:**
```bash
./scripts/bump-version -Type patch -DryRun
```

---

## 📦 GitHub Actions 자동화

### 트리거 조건
- `v*` 태그가 push될 때 자동 실행 (예: v1.0.1)

### 자동 프로세스
1. **버전 검증**: src/version.py ↔ Git 태그 일치 확인
2. **빌드**: PyInstaller로 EXE 생성
3. **인스톨러**: Inno Setup으로 Windows Installer 생성
4. **릴리스 생성**: GitHub Release 자동 생성
5. **파일 첨부**: TunnelDBManager-Setup-{version}.exe 첨부

### 확인
- **빌드 진행**: https://github.com/sanghyun-io/db-connector/actions
- **릴리스**: https://github.com/sanghyun-io/db-connector/releases

---

## 📝 수동 릴리스 (고급)

자동화를 원하지 않는 경우:

### 1단계: 버전 증가만
```bash
# PowerShell
.\scripts\bump-version.ps1 -Type patch

# Bash
./scripts/bump-version -Type patch
```

### 2단계: 커밋 & Push
```bash
git add src/version.py
git commit -m "Bump version to x.x.x"
git push origin main
```

### 3단계: 릴리스 생성
```bash
# PowerShell
.\scripts\create-release.ps1

# Bash
./scripts/create-release
```

---

## 🛠️ 로컬 빌드 (테스트용)

GitHub Actions를 거치지 않고 로컬에서 빌드:

```bash
# PowerShell
.\scripts\build-installer.ps1

# Bash
./scripts/build-installer
```

**요구사항:**
- Python 3.9+
- PyInstaller: `pip install -e ".[dev]"`
- Inno Setup 6: https://jrsoftware.org/isinfo.php

---

## ❓ 도움말

각 스크립트의 상세 사용법:

```bash
# PowerShell
.\scripts\bump-version.ps1 -Help
.\scripts\create-release.ps1 -Help
.\scripts\build-installer.ps1 -Help

# Bash (영어 도움말)
./scripts/bump-version -h
./scripts/create-release -h
./scripts/build-installer -h

# PowerShell 상세 도움말
Get-Help .\scripts\bump-version.ps1 -Detailed
Get-Help .\scripts\bump-version.ps1 -Examples
```

---

## 🐛 트러블슈팅

### Git 태그가 이미 존재
```bash
# 로컬 태그 삭제
git tag -d v1.0.1

# 원격 태그 삭제
git push origin :refs/tags/v1.0.1
```

### 버전 불일치
src/version.py의 버전이 태그와 일치하지 않으면 GitHub Actions가 실패합니다.
- src/version.py 수정 후 다시 시도

### 빌드 실패
- GitHub Actions 로그 확인
- 로컬에서 `./scripts/build-installer`로 테스트

---

## 📚 관련 파일

- `src/version.py` - 버전 정보
- `scripts/bump-version.ps1` - 버전 증가 스크립트
- `scripts/create-release.ps1` - 릴리스 생성 스크립트
- `scripts/build-installer.ps1` - 로컬 빌드 스크립트
- `.github/workflows/release.yml` - GitHub Actions 워크플로우
- `installer/TunnelDBManager.iss` - Inno Setup 설정

---

## 🎯 빠른 참조

| 작업 | 명령어 (PowerShell) | 명령어 (Bash) |
|------|-------------------|--------------|
| 패치 릴리스 | `.\scripts\bump-version.ps1 -Type patch -AutoRelease` | `./scripts/bump-version -Type patch -AutoRelease` |
| 마이너 릴리스 | `.\scripts\bump-version.ps1 -Type minor -AutoRelease` | `./scripts/bump-version -Type minor -AutoRelease` |
| 메이저 릴리스 | `.\scripts\bump-version.ps1 -Type major -AutoRelease` | `./scripts/bump-version -Type major -AutoRelease` |
| 미리보기 | `.\scripts\bump-version.ps1 -Type patch -DryRun` | `./scripts/bump-version -Type patch -DryRun` |
| 도움말 | `.\scripts\bump-version.ps1 -Help` | `./scripts/bump-version -h` |
| 로컬 빌드 | `.\scripts\build-installer.ps1` | `./scripts/build-installer` |
