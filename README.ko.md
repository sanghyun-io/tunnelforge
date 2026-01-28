# TunnelForge

SSH 터널을 통한 안전한 데이터베이스 관리 GUI 애플리케이션

[English](README.md)

![Python](https://img.shields.io/badge/Python-3.9+-blue.svg)
![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

## TunnelForge란?

TunnelForge는 SSH 터널을 통해 원격 데이터베이스에 안전하게 접근할 수 있는 데스크톱 애플리케이션입니다. 복잡한 커맨드라인 설정 없이 Bastion 호스트를 통해 MySQL 데이터베이스에 쉽게 연결하세요.

### 주요 기능

- **원클릭 SSH 터널** - 저장된 설정으로 Bastion 호스트를 통해 원격 DB에 간편하게 연결
- **직접 연결** - 로컬 또는 접근 가능한 데이터베이스에 직접 연결 지원
- **빠른 Export/Import** - MySQL Shell의 병렬 처리로 빠른 데이터 전송
- **시스템 트레이** - 백그라운드에서 조용히 실행, 필요할 때 바로 사용

## 다운로드

**[최신 버전 다운로드](https://github.com/sanghyun-io/tunnelforge/releases/latest/download/TunnelForge-Setup-latest.exe)**

또는 [Releases](https://github.com/sanghyun-io/tunnelforge/releases)에서 모든 버전을 확인하세요.

## 빠른 시작

### 1. 설치

다운로드한 설치 파일을 실행하고 설치 마법사를 따라 진행하세요.

### 2. 터널 추가

1. **"터널 추가"** 버튼 클릭
2. 연결 정보 입력:
   - **터널 이름**: 구분하기 쉬운 이름 (예: "운영 DB")
   - **Bastion 호스트**: SSH 점프 서버 주소
   - **SSH 키**: 개인 키 파일 경로
   - **데이터베이스 호스트**: 대상 DB 서버 (Bastion에서 본 주소)
   - **데이터베이스 인증 정보**: 사용자명과 비밀번호

3. **저장** 클릭

### 3. 연결

1. 목록에서 터널 선택
2. **"연결"** 클릭
3. 연결되면 데이터베이스 도구 사용:
   - **Export** - 스키마 또는 테이블 백업
   - **Import** - 백업 파일에서 복원

## 사용 팁

### 여러 환경 관리

각 환경(개발, 스테이징, 운영)별로 명확한 이름으로 터널 설정을 생성하세요.

### Export 모범 사례

- 구조 백업에는 **스키마 전용 Export** 사용
- 필요한 것만 내보내려면 **테이블 선택** 사용
- Export는 병렬로 실행되어 빠르게 완료됨

### 시스템 트레이

- 트레이로 최소화하면 터널이 백그라운드에서 계속 실행
- 트레이 아이콘 더블클릭으로 창 복원
- 우클릭으로 빠른 동작 메뉴

## 요구 사항

- Windows 10 이상
- [MySQL Shell](https://dev.mysql.com/downloads/shell/) (Export/Import 기능 사용 시)

## 설정 파일 위치

`%LOCALAPPDATA%\TunnelForge\config.json`

## 기여하기

개발 환경 설정 및 가이드라인은 [CONTRIBUTING.md](CONTRIBUTING.md)를 참고하세요.

## 라이선스

MIT 라이선스 - 자세한 내용은 [LICENSE](LICENSE) 파일을 참고하세요.
