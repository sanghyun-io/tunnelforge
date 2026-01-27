---
command_name: release
description: TunnelDB Manager 새 버전 릴리스 생성 (버전 증가 + GitHub Actions 트리거)
tags: [release, versioning, automation]
---

당신은 TunnelDB Manager 프로젝트의 릴리스 프로세스를 도와주는 전문가입니다.

## 목표

스마트 릴리스 스크립트를 실행하여 사용자가 새 버전을 릴리스할 수 있도록 합니다.

## 실행 방법

**Python 스크립트 사용 (권장):**

```bash
python scripts/smart_release.py
```

**Python이 없을 때 Bash 사용:**

```bash
./scripts/smart-release.sh
```

## 스마트 릴리스 동작 방식

스크립트가 자동으로:

1. `src/version.py`에서 로컬 버전 읽기
2. GitHub API로 원격 최신 릴리스 버전 확인
3. 버전 비교 후 시나리오별 처리:
   - **버전 동일**: patch/minor/major 선택 → 버전 증가 → 릴리스
   - **로컬이 높음**: 확인 후 현재 버전으로 릴리스
   - **원격이 높음**: 경고 후 종료
4. Git 커밋, 태그 생성, Push
5. GitHub Actions 자동 트리거

## 미리보기 (Dry Run)

실제 실행 전에 무엇이 바뀔지 확인:

```bash
python scripts/smart_release.py --dry-run
```

## 워크플로우

1. **미리보기 실행**: 먼저 `--dry-run`으로 확인
2. **사용자 확인**: 결과를 보여주고 진행 여부 확인
3. **실제 릴리스 실행**: 확인 후 실행
4. **결과 안내**: GitHub Actions 링크 제공

## 결과 안내

릴리스가 성공하면:

1. 새 버전 번호 표시
2. GitHub Actions 링크 제공: https://github.com/sanghyun-io/db-connector/actions
3. 릴리스 페이지 링크 제공: https://github.com/sanghyun-io/db-connector/releases

"빌드는 약 5-10분 소요됩니다. GitHub Actions에서 진행 상황을 확인하세요."

## 에러 처리

- Git 태그가 이미 존재하면: 덮어쓰기 여부 확인
- 원격 버전이 더 높으면: 로컬 업데이트 안내
- 네트워크 오류: GitHub API 접속 확인 안내

## 스크립트 파일

| 파일 | 설명 |
|------|------|
| `scripts/smart_release.py` | Python 버전 (권장) |
| `scripts/smart-release.sh` | Bash 버전 (Python 없을 때) |
| `scripts/smart-release.ps1` | PowerShell 버전 (레거시) |

## 중요 주의사항

- 반드시 DryRun으로 먼저 확인한 후 실제 실행
- main 브랜치에서만 릴리스 수행
- 릴리스 후에는 되돌릴 수 없으므로 신중하게 진행
