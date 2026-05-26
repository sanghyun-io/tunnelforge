# TunnelForge - 빌드 가이드

이 문서는 TunnelForge를 Windows 실행 파일(`.exe`)로 빌드하는 방법을 설명합니다.

## 📋 목차

- [사전 요구사항](#사전-요구사항)
- [개발 환경 설정](#개발-환경-설정)
- [빌드 방법](#빌드-방법)
- [빌드 옵션](#빌드-옵션)
- [빌드 결과물](#빌드-결과물)
- [트러블슈팅](#트러블슈팅)

---

## 🔧 사전 요구사항

- Python 3.9 이상
- pip (Python 패키지 관리자)
- Rust toolchain (`cargo`) - `tunnelforge-core` DB service 빌드용
- Windows OS (다른 OS의 경우 PyInstaller 옵션 조정 필요)

---

## ⚙️ 개발 환경 설정

### 1. 가상 환경 생성 및 활성화

```bash
# 가상 환경 생성
python -m venv .venv

# 가상 환경 활성화 (Windows)
.venv\Scripts\activate

# 가상 환경 활성화 (Git Bash)
source .venv/Scripts/activate
```

### 2. 의존성 설치

```bash
# 기본 의존성 + 개발 의존성 설치 (PyInstaller 포함)
pip install -e ".[dev]"

# 또는 PyInstaller만 추가 설치
pip install pyinstaller>=5.0.0
```

---

## 🚀 빌드 방법

### 방법 1: PyInstaller Spec 파일 사용 (권장)

프로젝트에 포함된 `tunnel-manager.spec` 파일을 사용하여 빌드합니다.

```bash
# Spec 파일로 빌드
pyinstaller tunnel-manager.spec
```

### 방법 2: 명령줄 옵션 사용

Spec 파일 없이 명령줄 옵션으로 빌드할 수도 있습니다.

```bash
pyinstaller --name "TunnelForge" ^
            --onefile ^
            --windowed ^
            --icon "assets/icon.ico" ^
            --add-data "assets;assets" ^
            --add-binary "migration_core\target\release\tunnelforge-core.exe;." ^
            --hidden-import "PyQt6.QtCore" ^
            --hidden-import "PyQt6.QtGui" ^
            --hidden-import "PyQt6.QtWidgets" ^
            --hidden-import "sshtunnel" ^
            --hidden-import "paramiko" ^
            main.py
```

**옵션 설명:**
- `--name`: 생성될 실행 파일 이름
- `--onefile`: 단일 실행 파일로 생성 (모든 의존성 포함)
- `--windowed`: 콘솔 창 숨김 (GUI 전용)
- `--icon`: 실행 파일 아이콘
- `--add-data`: 리소스 파일 포함 (형식: `소스경로;대상경로`)
- `--add-binary`: Rust DB Core 실행 파일 포함
- `--hidden-import`: PyInstaller가 자동으로 감지하지 못하는 모듈 명시

---

## 🎛️ 빌드 옵션

### 단일 파일 vs 디렉터리

#### 단일 파일 모드 (`--onefile`)
- **장점**: 배포 간편 (실행 파일 1개)
- **단점**: 실행 시 압축 해제로 초기 로딩 시간 증가
- **사용 시나리오**: 간편한 배포가 중요한 경우

```bash
pyinstaller --onefile tunnel-manager.spec
```

#### 디렉터리 모드 (기본값)
- **장점**: 실행 속도 빠름
- **단점**: 여러 파일/폴더로 구성됨
- **사용 시나리오**: 성능이 중요한 경우

```bash
# Spec 파일에서 'onefile' 옵션 제거
pyinstaller tunnel-manager.spec
```

### 콘솔 창 표시 여부

- **`--windowed` (또는 `-w`)**: 콘솔 창 숨김 (GUI 애플리케이션용)
- **`--console` (또는 `-c`)**: 콘솔 창 표시 (디버깅 시 유용)

디버깅 시에는 콘솔 모드로 빌드하여 에러 메시지를 확인할 수 있습니다:

```bash
# 디버깅용: 콘솔 창 표시
pyinstaller --console main.py
```

---

## 📦 빌드 결과물

빌드가 완료되면 다음 디렉터리가 생성됩니다:

```
tunnel-manager/
├── build/                          # 임시 빌드 파일 (삭제 가능)
│   └── TunnelForge/
├── dist/                           # 최종 실행 파일 위치
│   └── TunnelForge.exe  # 배포용 실행 파일
└── tunnel-manager.spec             # PyInstaller 설정 파일
```

### 실행 파일 테스트

```bash
# dist 폴더로 이동
cd dist

# 실행 파일 실행
TunnelForge.exe
```

---

## 🐛 트러블슈팅

### 1. 모듈을 찾을 수 없다는 에러 (ImportError)

**증상:**
```
ImportError: No module named 'xxx'
```

**해결 방법:**
Spec 파일의 `hiddenimports`에 누락된 모듈 추가:

```python
a = Analysis(
    # ...
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'sshtunnel',
        'paramiko',
        'xxx',  # 누락된 모듈 추가
    ],
)
```

### 2. 리소스 파일(아이콘, 이미지)을 찾을 수 없는 에러

**증상:**
```
FileNotFoundError: [Errno 2] No such file or directory: 'assets/icon.ico'
```

**해결 방법:**
Spec 파일의 `datas`에 리소스 경로 추가:

```python
a = Analysis(
    # ...
    datas=[
        ('assets', 'assets'),  # (소스 경로, 대상 경로)
    ],
)
```

### 3. 실행 파일 크기가 너무 큼

**해결 방법:**
1. **UPX 압축 사용** (Spec 파일):
   ```python
   exe = EXE(
       # ...
       upx=True,  # 실행 파일 압축 (UPX 설치 필요)
   )
   ```

2. **불필요한 라이브러리 제외**:
   ```python
   a = Analysis(
       # ...
       excludes=['matplotlib', 'numpy'],  # 사용하지 않는 대용량 라이브러리
   )
   ```

### 4. PyQt6 관련 에러

**증상:**
```
Qt platform plugin error
```

**해결 방법:**
PyQt6 플러그인을 명시적으로 포함:

```python
a = Analysis(
    # ...
    hiddenimports=[
        'PyQt6.QtCore',
        'PyQt6.QtGui',
        'PyQt6.QtWidgets',
        'PyQt6.sip',  # 추가
    ],
)
```

### 5. 빌드 캐시 문제

**증상:**
이전 빌드의 설정이 남아있어 변경사항이 반영되지 않음

**해결 방법:**
빌드 디렉터리 삭제 후 재빌드:

```bash
# 빌드 디렉터리 삭제
rmdir /s /q build dist

# 재빌드
pyinstaller tunnel-manager.spec
```

---

## 🔍 추가 정보

### Spec 파일 커스터마이징

`tunnel-manager.spec` 파일을 수정하여 빌드 옵션을 변경할 수 있습니다:

- **애플리케이션 이름**: `exe = EXE(name='...')`
- **아이콘**: `exe = EXE(icon='...')`
- **콘솔 표시 여부**: `exe = EXE(console=True/False)`
- **단일 파일 여부**: `COLLECT` 섹션 주석 처리/해제

### PyInstaller 공식 문서

더 자세한 내용은 [PyInstaller 공식 문서](https://pyinstaller.org/en/stable/)를 참고하세요.

---

## ✅ 체크리스트

빌드 전 확인 사항:

- [ ] 가상 환경 활성화
- [ ] 개발 의존성 설치 (`pip install -e ".[dev]"`)
- [ ] 애플리케이션 정상 실행 확인 (`python main.py`)
- [ ] 리소스 파일(아이콘 등) 경로 확인
- [ ] Rust DB Core 빌드 확인 (`cargo build --manifest-path migration_core\Cargo.toml --release`)
- [ ] `tunnelforge-core.exe`가 PyInstaller/Installer에 포함되는지 확인
- [ ] Spec 파일 옵션 검토

빌드 후 확인 사항:

- [ ] `dist/TunnelForge.exe` 생성 확인
- [ ] 실행 파일 정상 동작 테스트
- [ ] SSH 터널 연결 테스트
- [ ] 데이터베이스 연결 테스트
- [ ] 설정 저장/불러오기 테스트

---

---

## 📦 Windows Installer 생성

PyInstaller로 빌드한 EXE 파일을 Inno Setup으로 패키징하여 Windows Installer를 생성합니다.

### 파일 구조 이해하기

프로젝트에는 2종류의 파일이 있습니다:

| 파일 | 타입 | 용도 |
|------|------|------|
| `installer/TunnelForge.iss` | 설정 파일 | Installer 빌드 방법을 정의 (직접 실행 ❌) |
| `scripts/build-installer.ps1` | 실행 스크립트 | 전체 빌드 과정을 자동화 (직접 실행 ✅) |

**간단히 말하면:**
- `.iss` 파일 = 레시피 (Installer에 무엇을 포함할지 정의)
- `.ps1` 스크립트 = 요리사 (레시피를 읽고 자동으로 Installer 생성)

**워크플로우:**
```
[build-installer.ps1 실행]
    ↓
    ├─→ Rust DB Core 빌드 → migration_core/target/release/tunnelforge-core.exe 생성
    ├─→ PyInstaller 실행 → dist/TunnelForge.exe 생성
    └─→ Inno Setup 실행 → TunnelForge.iss 읽기 → output/Installer.exe 생성
```

### 사전 요구사항

- **Inno Setup 6** 설치: https://jrsoftware.org/isinfo.php
  - 설치하지 않으면 자동화 스크립트가 에러 메시지와 함께 설치 안내를 표시합니다
- Python 가상환경 활성화 및 의존성 설치 완료

### 방법 1: 자동화 스크립트 사용 (권장) ⭐

**이 방법이 제일 쉽습니다!** 프로젝트에 포함된 빌드 스크립트가 모든 과정을 자동으로 처리합니다.

```powershell
# 이것만 실행하면 됩니다!
.\scripts\build-installer.ps1

# 또는 BAT 파일 (PowerShell과 동일한 기능)
.\scripts\build-installer.bat
```

**이 스크립트가 자동으로 하는 일:**
1. ✅ Rust DB Core `tunnelforge-core.exe` 빌드
2. ✅ PyInstaller로 `TunnelForge.exe` 빌드
3. ✅ Inno Setup으로 `TunnelForge-Setup-1.0.0.exe` 생성
4. ✅ 빌드 과정 상태를 실시간으로 표시
5. ✅ 에러 발생 시 명확한 해결 방법 안내

**스크립트 옵션:**

```powershell
# 이전 빌드 파일 정리 후 빌드
.\scripts\build-installer.ps1 -Clean

# PyInstaller 빌드 생략 (기존 EXE 사용)
.\scripts\build-installer.ps1 -SkipPyInstaller

# 옵션 조합
.\scripts\build-installer.ps1 -Clean -SkipPyInstaller
```

### 방법 2: 수동 빌드 (고급 사용자용)

자동화 스크립트를 사용하지 않고 직접 각 단계를 실행하는 방법입니다.

```powershell
# 1단계: Rust DB Core 빌드
cargo build --manifest-path migration_core\Cargo.toml --release
# → 결과: migration_core\target\release\tunnelforge-core.exe

# 2단계: PyInstaller로 EXE 빌드
pyinstaller tunnel-manager.spec
# → 결과: dist/TunnelForge.exe

# 3단계: Inno Setup 컴파일러로 .iss 파일을 읽어서 Installer 생성
"C:\Program Files (x86)\Inno Setup 6\ISCC.exe" installer\TunnelForge.iss
# → 결과: output/TunnelForge-Setup-1.0.0.exe
```

**참고:** `TunnelForge.iss`는 직접 실행하는 게 아니라 Inno Setup 컴파일러(ISCC.exe)가 읽는 설정 파일입니다.

### Installer 기능

생성된 Installer는 다음 기능을 제공합니다:

- ✅ 프로그램 추가/제거 지원
- ✅ 시작 메뉴 단축키 자동 생성
- ✅ 바탕화면 아이콘 (선택 옵션)
- ✅ 언인스톨러 자동 생성
- ✅ 업그레이드 시 이전 버전 자동 제거
- ✅ 한국어/영어 다국어 지원
- ✅ 관리자 권한 불필요

### 빌드 결과물

```
tunnel-manager/
├── dist/
│   └── TunnelForge.exe          # PyInstaller 빌드 결과
└── output/
    └── TunnelForge-Setup-1.0.0.exe  # Windows Installer
```

### Installer 테스트

```powershell
# Installer 실행
.\output\TunnelForge-Setup-1.0.0.exe

# 설치 후 확인사항:
# 1. 시작 메뉴에서 "TunnelForge" 검색
# 2. 프로그램 정상 실행 확인
# 3. 제어판 > 프로그램 추가/제거에서 확인
# 4. 제거 후 재설치 테스트
```

### Installer 커스터마이징

`installer/TunnelForge.iss` 파일을 수정하여 설정을 변경할 수 있습니다:

```iss
[Setup]
AppVersion=1.0.0                      ; 버전 번호
DefaultDirName={autopf}\...          ; 기본 설치 경로
Compression=lzma2/ultra64            ; 압축 설정

[Languages]
; 지원 언어 추가/제거
Name: "korean"; MessagesFile: "compiler:Languages\Korean.isl"
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
; 바탕화면 아이콘 기본 체크 여부
Name: "desktopicon"; ...; Flags: unchecked
```

---

## macOS App 생성

macOS에서는 PyInstaller `.app` 번들과 DMG/ZIP 배포물을 생성합니다. 실제 실행 검증은 macOS 기기에서 수행해야 합니다.
지원 범위와 최종 검증 체크리스트는 `docs/macos_support.md`를 기준으로 합니다.

### 사전 요구사항

- macOS 13 이상
- Python 3.9 이상
- Rust toolchain (`cargo`)
- PyInstaller 포함 개발 의존성 (`pip install -e ".[dev]"`)

### 앱 번들 빌드

```bash
bash scripts/build-macos.sh
```

이 스크립트는 다음을 수행합니다.

1. Rust DB Core `migration_core/target/release/tunnelforge-core` 빌드
2. `assets/icon.icns`가 없으면 `assets/icon_512.png`에서 생성
3. PyInstaller로 `dist/TunnelForge.app` 생성
4. `.app` 내부에 `tunnelforge-core`가 포함되어 있는지 확인

기본 최소 배포 대상은 `MACOSX_DEPLOYMENT_TARGET=13.0`입니다.

### DMG/ZIP 패키징

```bash
bash scripts/package-macos.sh
```

기본 결과물:

```text
dist/TunnelForge-macOS-{version}-{arm64|x86_64}.dmg
dist/TunnelForge-macOS-{version}-{arm64|x86_64}.zip
```

환경 변수가 설정된 경우 코드 서명과 노터라이즈도 수행합니다.

```bash
export APPLE_CODESIGN_IDENTITY="Developer ID Application: ..."
export APPLE_ID="apple-id@example.com"
export APPLE_TEAM_ID="TEAMID1234"
export APPLE_APP_SPECIFIC_PASSWORD="app-specific-password"
bash scripts/package-macos.sh
```

---

**문서 작성일:** 2026-01-27
