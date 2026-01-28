---
command_name: bump
description: src/version.py의 버전만 증가 (Git 작업 없음, 수동 커밋용)
tags: [versioning]
---

당신은 TunnelForge 프로젝트의 버전 관리를 도와주는 전문가입니다.

## 목표

src/version.py의 버전만 증가시킵니다. Git 커밋이나 Push는 수동으로 진행합니다.

## 워크플로우

### 1단계: 현재 버전 확인

`src/version.py`를 읽어서 현재 버전을 확인합니다.

```python
# src/version.py
__version__ = "x.x.x"
```

### 2단계: 버전 증가 타입 선택

사용자에게 버전 증가 타입을 선택하게 합니다:

- **patch** (x.x.X): 버그 수정 - 1.0.0 → 1.0.1
- **minor** (x.X.0): 새 기능 추가 - 1.0.1 → 1.1.0
- **major** (X.0.0): 큰 변경사항 - 1.1.0 → 2.0.0

AskUserQuestion 도구를 사용합니다.

### 3단계: 버전 증가 실행

**PowerShell 환경:**
```powershell
.\scripts\bump-version.ps1 -Type <선택된타입>
```

**Git Bash 환경:**
```bash
./scripts/bump-version -Type <선택된타입>
```

`-AutoRelease` 플래그를 **사용하지 않으므로** 파일만 업데이트됩니다.

### 4단계: 변경사항 확인

스크립트 실행 후 변경사항을 확인합니다:

```bash
git diff src/version.py
```

### 5단계: 다음 단계 안내

사용자에게 다음 단계를 안내합니다:

```
✅ 버전이 x.x.x → y.y.y로 업데이트되었습니다!

다음 단계:
1. 변경사항 확인:
   git status
   git diff src/version.py

2. 커밋:
   git add src/version.py
   git commit -m "Bump version to y.y.y"
   git push origin main

3. 릴리스 생성:
   # PowerShell
   .\scripts\create-release.ps1

   # Git Bash
   ./scripts/create-release

또는 자동 릴리스를 원하시면:
   /release 명령어를 사용하세요.
```

## 환경 감지

- Windows PowerShell/CMD: `.ps1` 스크립트
- Git Bash/WSL/Linux/macOS: 확장자 없는 bash 래퍼

## /bump vs /release 차이

| 명령어 | 버전 증가 | Git 커밋 | Git Push | 태그 생성 | GitHub Actions |
|--------|----------|---------|---------|----------|----------------|
| `/bump` | ✅ | ❌ | ❌ | ❌ | ❌ |
| `/release` | ✅ | ✅ | ✅ | ✅ | ✅ |

**언제 `/bump`를 사용하나요?**
- 버전만 올리고 나중에 수동으로 커밋하고 싶을 때
- 여러 변경사항과 함께 커밋하고 싶을 때
- Git 작업을 직접 제어하고 싶을 때

**언제 `/release`를 사용하나요?**
- 원클릭으로 전체 릴리스 프로세스를 완료하고 싶을 때 (권장)
- 빠르게 릴리스하고 싶을 때

## 에러 처리

스크립트 실행 실패 시:
- 에러 메시지 표시
- 환경 확인 (PowerShell vs Bash)
- 스크립트 경로 확인

## 참고 문서

자세한 내용은 `/release-guide` 스킬을 참조하세요.

## 예제 대화

```
User: /bump