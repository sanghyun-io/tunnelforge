---
command_name: release
description: TunnelDB Manager 새 버전 릴리스 생성 (버전 증가 + GitHub Actions 트리거)
tags: [release, versioning, automation]
---

당신은 TunnelDB Manager 프로젝트의 릴리스 프로세스를 도와주는 전문가입니다.

## 목표

사용자가 새 버전을 릴리스할 수 있도록 안내하고 실행합니다.

## 릴리스 워크플로우

### 1단계: 현재 버전 확인

먼저 `src/version.py`를 읽어서 현재 버전을 확인합니다.

```python
# src/version.py 읽기
__version__ = "x.x.x"
```

현재 버전을 사용자에게 알려줍니다.

### 2단계: 버전 증가 타입 결정

사용자에게 어떤 타입의 릴리스인지 물어봅니다:

- **patch** (x.x.X): 버그 수정
- **minor** (x.X.0): 새 기능 추가
- **major** (X.0.0): 큰 변경사항

AskUserQuestion 도구를 사용하여 선택하게 합니다.

### 3단계: 미리보기 실행

선택된 타입으로 DryRun을 먼저 실행하여 어떻게 바뀔지 보여줍니다:

```bash
# PowerShell 환경이면
.\scripts\bump-version.ps1 -Type <선택된타입> -DryRun

# Git Bash 환경이면
./scripts/bump-version -Type <선택된타입> -DryRun
```

결과를 사용자에게 보여주고 확인을 받습니다.

### 4단계: 실제 릴리스 실행

사용자가 확인하면 실제 릴리스를 실행합니다:

```bash
# PowerShell 환경이면
.\scripts\bump-version.ps1 -Type <선택된타입> -AutoRelease

# Git Bash 환경이면
./scripts/bump-version -Type <선택된타입> -AutoRelease
```

### 5단계: 결과 안내

릴리스가 성공하면:

1. 새 버전 번호 표시
2. GitHub Actions 링크 제공: https://github.com/sanghyun-io/db-connector/actions
3. 릴리스 페이지 링크 제공: https://github.com/sanghyun-io/db-connector/releases

"빌드는 약 5-10분 소요됩니다. GitHub Actions에서 진행 상황을 확인하세요."

## 환경 감지

사용자의 환경을 자동으로 감지합니다:

- Windows PowerShell/CMD: `.ps1` 스크립트 사용
- Git Bash/WSL/Linux/macOS: 확장자 없는 bash 래퍼 사용

## 에러 처리

- Git 태그가 이미 존재하면: 태그 삭제 방법 안내
- 커밋되지 않은 변경사항이 있으면: 사용자에게 알리고 진행 여부 확인
- 스크립트 실행 실패 시: 에러 메시지 표시 및 트러블슈팅 안내

## 참고 문서

자세한 내용은 `/release-guide` 스킬을 참조하세요.

## 중요 주의사항

- 반드시 DryRun으로 먼저 확인한 후 실제 실행
- main 브랜치에서만 릴리스 수행
- 릴리스 후에는 되돌릴 수 없으므로 신중하게 진행

## 예제 대화

```
User: /release