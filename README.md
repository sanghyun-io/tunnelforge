# TunnelDB Manager

SSH 터널 및 MySQL 데이터베이스 관리를 위한 PyQt6 GUI 애플리케이션입니다.

## 주요 기능

- **SSH 터널 관리**: Bastion 호스트를 통한 안전한 원격 데이터베이스 접속
- **직접 연결 모드**: 로컬 또는 외부 DB에 직접 연결
- **MySQL Shell Export**: 병렬 처리를 통한 빠른 스키마/테이블 Export
- **MySQL Shell Import**: Dump 파일 병렬 Import
- **GitHub 이슈 자동 보고**: Export/Import 오류 시 자동으로 GitHub 이슈 생성
- **시스템 트레이**: 백그라운드 실행 지원

## 설치

### 요구사항

- Python 3.9+
- MySQL Shell (Export/Import 기능 사용 시)

### 설치 방법

```bash
# 가상환경 생성
python -m venv .venv

# 가상환경 활성화
# Windows
.venv\Scripts\activate
# Linux/macOS
source .venv/bin/activate

# 의존성 설치
pip install -e .

# 개발 의존성 포함 설치 (PyInstaller 등)
pip install -e ".[dev]"
```

### GitHub 이슈 자동 보고 설정 (선택사항)

Export/Import 오류 시 자동으로 GitHub 이슈를 생성하려면:

1. `.env.example`을 복사하여 `.env` 파일 생성:
   ```bash
   cp .env.example .env
   ```

2. GitHub App 설정 (자세한 내용은 [GITHUB_APP_SETUP.md](GITHUB_APP_SETUP.md) 참고)

3. Private Key를 `secrets/` 디렉토리에 배치:
   ```bash
   cp ~/Downloads/your-app.private-key.pem secrets/github-app-private-key.pem
   ```

4. `.env` 파일 설정:
   ```bash
   GITHUB_APP_ID=123456
   GITHUB_APP_PRIVATE_KEY=secrets/github-app-private-key.pem
   GITHUB_APP_INSTALLATION_ID=12345678
   GITHUB_REPO=your-org/your-repo
   ```

## 실행

```bash
python main.py
```

## 프로젝트 구조

```
tunnel-manager/
├── main.py                     # Entry point
├── src/
│   ├── __init__.py
│   ├── core/                   # 핵심 비즈니스 로직
│   │   ├── __init__.py
│   │   ├── config_manager.py       # 설정 파일 관리
│   │   ├── tunnel_engine.py        # SSH 터널 엔진
│   │   ├── db_connector.py         # MySQL 연결
│   │   ├── github_app_auth.py      # GitHub App 인증
│   │   └── github_issue_reporter.py # GitHub 이슈 자동 보고
│   ├── exporters/              # DB Export/Import
│   │   ├── __init__.py
│   │   └── mysqlsh_exporter.py # MySQL Shell 기반 Export/Import
│   └── ui/                     # PyQt6 UI
│       ├── __init__.py
│       ├── main_window.py      # 메인 윈도우
│       ├── dialogs/
│       │   ├── __init__.py
│       │   ├── tunnel_config.py    # 터널 설정 다이얼로그
│       │   ├── settings.py         # 설정 다이얼로그
│       │   └── db_dialogs.py       # DB Export/Import 다이얼로그
│       └── workers/
│           ├── __init__.py
│           └── mysql_worker.py     # MySQL Shell 작업 스레드
├── assets/                     # 리소스 파일
│   ├── icon.ico
│   ├── icon.png
│   ├── icon.svg
│   └── icon_512.png
├── secrets/                    # GitHub App Private Key (Git 제외)
│   ├── README.md
│   └── github-app-private-key.pem.example
├── .env.example                # 환경변수 템플릿
├── pyproject.toml              # 패키지 설정 및 의존성 목록
├── CLAUDE.md                   # Claude Code 가이드
├── GITHUB_APP_SETUP.md         # GitHub App 설정 가이드
└── .gitignore
```

## 설정 파일 위치

- **Windows**: `%LOCALAPPDATA%\TunnelDB\config.json`
- **Linux/macOS**: `~/.config/tunneldb/config.json`

## 개발 및 빌드

### Windows Installer 빌드

로컬에서 Windows Installer를 빌드하려면:

```bash
# 의존성 설치 (개발 도구 포함)
pip install -e ".[dev]"

# Installer 빌드
.\scripts\build-installer.ps1

# 빌드 파일 정리 후 빌드
.\scripts\build-installer.ps1 -Clean
```

**요구사항:**
- [Inno Setup 6](https://jrsoftware.org/isinfo.php) 설치 필요

빌드된 파일:
- `dist\TunnelDBManager.exe` - 실행 파일
- `output\TunnelDBManager-Setup-{version}.exe` - Windows Installer

### 릴리스 프로세스

이 프로젝트는 **GitHub Actions를 통한 자동 빌드 및 릴리스**를 사용합니다.

#### 버전 관리 원칙

- **Single Source of Truth**: `src/version.py`의 `__version__`만 관리
- Git 태그를 push하면 자동으로 빌드 및 릴리스 생성

#### 새 버전 릴리스 방법

**방법 1: 자동 버전 증가 (권장)**

<table>
<tr>
<td width="50%"><b>PowerShell / CMD</b></td>
<td width="50%"><b>Git Bash / Linux / macOS</b></td>
</tr>
<tr>
<td>

```powershell
# 패치 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type patch -AutoRelease

# 마이너 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type minor -AutoRelease

# 메이저 버전 증가 + 자동 릴리스
.\scripts\bump-version.ps1 -Type major -AutoRelease
```

</td>
<td>

```bash
# 패치 버전 증가 + 자동 릴리스
./scripts/bump-version -Type patch -AutoRelease

# 마이너 버전 증가 + 자동 릴리스
./scripts/bump-version -Type minor -AutoRelease

# 메이저 버전 증가 + 자동 릴리스
./scripts/bump-version -Type major -AutoRelease
```

</td>
</tr>
</table>

✨ **이 명령 하나로:**
- 버전 자동 증가
- 파일 업데이트
- 커밋 & Push
- 태그 생성 & Push
- GitHub Actions 트리거

**방법 2: 수동 버전 관리**

```bash
# 1. src/version.py에서 버전 수동 업데이트
# __version__ = "1.0.1"  → "1.0.2"로 변경

# 2. 변경사항 커밋
git add .
git commit -m "Bump version to 1.0.2"
git push origin main

# 3. 릴리스 스크립트 실행
# PowerShell / CMD
.\scripts\create-release.ps1

# Git Bash / Linux / macOS
./scripts/create-release
```

**미리보기 (DryRun)**

<table>
<tr>
<td width="50%"><b>PowerShell / CMD</b></td>
<td width="50%"><b>Git Bash / Linux / macOS</b></td>
</tr>
<tr>
<td>

```powershell
.\scripts\bump-version.ps1 -Type patch -DryRun
.\scripts\create-release.ps1 -DryRun
```

</td>
<td>

```bash
./scripts/bump-version -Type patch -DryRun
./scripts/create-release -DryRun
```

</td>
</tr>
</table>

**스크립트 도움말 보기**

<table>
<tr>
<td width="50%"><b>PowerShell / CMD</b></td>
<td width="50%"><b>Git Bash / Linux / macOS</b></td>
</tr>
<tr>
<td>

```powershell
# 간단한 도움말
.\scripts\bump-version.ps1 -Help
.\scripts\bump-version.ps1 -h

# 상세 도움말
Get-Help .\scripts\bump-version.ps1 -Detailed
```

</td>
<td>

```bash
# 도움말 보기
./scripts/bump-version -h
./scripts/create-release -h
./scripts/build-installer -h
```

</td>
</tr>
</table>

#### 자동화 프로세스

`v*` 태그가 push되면 GitHub Actions가 자동으로:

1. ✅ 버전 일치 검증 (`src/version.py` ↔ Git 태그)
2. 🔨 PyInstaller로 EXE 빌드
3. 📦 Inno Setup으로 Windows Installer 생성
4. 🚀 GitHub Release 생성
5. 📎 Installer를 Release에 첨부

**빌드 진행 상황 확인:**
- https://github.com/sanghyun-io/db-connector/actions

**릴리스 확인:**
- https://github.com/sanghyun-io/db-connector/releases

#### Dry Run (미리보기)

실제 릴리스 전 미리보기:

```bash
.\scripts\create-release.ps1 -DryRun
```

## Claude Code 명령어

이 프로젝트는 Claude Code 명령어를 제공하여 릴리스 프로세스를 더욱 쉽게 만듭니다.

### 사용 가능한 명령어

#### `/release` - 자동 릴리스

버전 증가부터 GitHub Actions 트리거까지 원클릭 릴리스:

```
/release
```

대화형으로 다음을 수행:
1. 현재 버전 확인
2. 버전 타입 선택 (patch/minor/major)
3. DryRun으로 미리보기
4. 자동 릴리스 실행

#### `/bump` - 버전만 증가

src/version.py의 버전만 증가 (수동 커밋용):

```
/bump
```

대화형으로:
1. 현재 버전 확인
2. 버전 타입 선택
3. 파일만 업데이트
4. Git 작업은 수동으로 진행

#### `/release-guide` - 릴리스 가이드

릴리스 프로세스 전체 가이드 보기:

```
/release-guide
```

포함 내용:
- 버전 관리 원칙
- 릴리스 워크플로우
- GitHub Actions 자동화
- 트러블슈팅

### 명령어 비교

| 명령어 | 버전 증가 | Git 작업 | 릴리스 | 사용 시기 |
|--------|----------|---------|--------|----------|
| `/release` | ✅ | ✅ 자동 | ✅ | 원클릭 릴리스 (권장) |
| `/bump` | ✅ | ❌ 수동 | ❌ | 버전만 증가, 나중에 커밋 |
| `/release-guide` | ❌ | ❌ | ❌ | 가이드 문서 보기 |

## 라이선스

이 프로젝트는 MIT 라이선스로 배포됩니다. 자세한 내용은 [LICENSE](LICENSE) 파일을 참고하세요.
