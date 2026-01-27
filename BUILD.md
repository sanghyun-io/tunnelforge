# TunnelDB Manager - 빌드 가이드

이 문서는 TunnelDB Manager를 Windows 실행 파일(`.exe`)로 빌드하는 방법을 설명합니다.

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
pyinstaller --name "TunnelDBManager" ^
            --onefile ^
            --windowed ^
            --icon "assets/icon.ico" ^
            --add-data "assets;assets" ^
            --hidden-import "PyQt6.QtCore" ^
            --hidden-import "PyQt6.QtGui" ^
            --hidden-import "PyQt6.QtWidgets" ^
            --hidden-import "sshtunnel" ^
            --hidden-import "paramiko" ^
            --hidden-import "pymysql" ^
            main.py
```

**옵션 설명:**
- `--name`: 생성될 실행 파일 이름
- `--onefile`: 단일 실행 파일로 생성 (모든 의존성 포함)
- `--windowed`: 콘솔 창 숨김 (GUI 전용)
- `--icon`: 실행 파일 아이콘
- `--add-data`: 리소스 파일 포함 (형식: `소스경로;대상경로`)
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
│   └── TunnelDBManager/
├── dist/                           # 최종 실행 파일 위치
│   └── TunnelDBManager.exe  # 배포용 실행 파일
└── tunnel-manager.spec             # PyInstaller 설정 파일
```

### 실행 파일 테스트

```bash
# dist 폴더로 이동
cd dist

# 실행 파일 실행
TunnelDBManager.exe
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
        'pymysql',
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
- [ ] Spec 파일 옵션 검토

빌드 후 확인 사항:

- [ ] `dist/TunnelDBManager.exe` 생성 확인
- [ ] 실행 파일 정상 동작 테스트
- [ ] SSH 터널 연결 테스트
- [ ] 데이터베이스 연결 테스트
- [ ] 설정 저장/불러오기 테스트

---

**문서 작성일:** 2026-01-27
