# DataFlare Tunnel Manager

SSH 터널 및 MySQL 데이터베이스 관리를 위한 PyQt6 GUI 애플리케이션입니다.

## 주요 기능

- **SSH 터널 관리**: Bastion 호스트를 통한 안전한 원격 데이터베이스 접속
- **직접 연결 모드**: 로컬 또는 외부 DB에 직접 연결
- **MySQL Shell Export**: 병렬 처리를 통한 빠른 스키마/테이블 Export
- **MySQL Shell Import**: Dump 파일 병렬 Import
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
pip install -r requirements.txt
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
│   │   ├── config_manager.py   # 설정 파일 관리
│   │   ├── tunnel_engine.py    # SSH 터널 엔진
│   │   └── db_connector.py     # MySQL 연결
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
├── pyproject.toml              # 패키지 설정
├── requirements.txt            # 의존성 목록
├── CLAUDE.md                   # Claude Code 가이드
└── .gitignore
```

## 설정 파일 위치

- **Windows**: `%LOCALAPPDATA%\DataFlareTunnel\config.json`
- **Linux/macOS**: `~/.config/dataflare_tunnel/config.json`

## 라이선스

MIT License
