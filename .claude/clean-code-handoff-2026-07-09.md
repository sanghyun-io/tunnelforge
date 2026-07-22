# TunnelForge Clean Code 리팩토링 — 세션 핸드오프 (2026-07-09)

> 이 문서는 다른 세션에서 이 작업을 그대로 이어가기 위한 인계 문서다.
> Round 1, 2는 **완료·머지·검증까지 끝났다**. Round 3부터 이어서 진행하면 된다.

---

## 0. 한 줄 요약

TunnelForge Clean Code 전수조사(255건 발견) → 3라운드 28 WP 마스터플랜 → **Round 1(8 WP), Round 2(12 WP) 완료·머지·통합검증·Codex 리뷰까지 끝남** → Round 3(8 WP, UI 다이얼로그/메인윈도우)만 남음.

새 세션에서 이어가려면: 이 문서 정독 → `.claude/clean-code-master-plan-2026-07-09.md`에서 Round 3 WP 8개 스펙 확인 → 아래 "패턴 플레이북" 그대로 재사용해서 team-launch → 완료 후 Codex 순차 리뷰.

---

## 1. 배경 — 이 작업이 왜 시작됐는가

- 사용자가 "Clean Code 미비한 코드 전수조사 진행 (병렬 필수)" 요청 → Workflow 기반 19그룹 병렬 조사로 **255건** 발견 (HIGH 40 / MEDIUM 134 / LOW 81), 검증 단계 기각 0건.
- 조사 결과: `.claude/investigation-clean-code-audit-2026-07-09.md` (+ `.json`), 브라우징용 Artifact 대시보드 게시됨 (URL은 이 세션에만 남아있음 — 필요시 재게시).
- **이전에 별도로 있었던 감사**(`.claude/investigation-full-audit-2026-07-08.md`, 정합성/스레딩 버그 154건)는 이미 별도로 전량 해결됨(`audit-resolution-2026-07-08.md`). 이번 Clean Code 작업과는 **관점이 다르다** — 정합성이 아니라 가독성/유지보수성(갓파일/갓클래스/중복/매직값 등)만 다룬다.
- 255건을 3라운드 29개 WP로 나눠 마스터플랜 작성(Workflow: WP 초안 29개 병렬 → Codex 아님, Claude 적대적 리뷰어 1개로 검증 → REVISE 4건 반영 → 28 WP 확정, WP-2.13은 WP-2.12로 흡수).
  - 마스터플랜: `.claude/clean-code-master-plan-2026-07-09.md` / `.json`

---

## 2. 현재 상태 (2026-07-09 기준)

| Round | WP 수 | 상태 | 회귀 |
|---|---|---|---|
| Round 1 (Enabler: 공유모듈 추출 + 갓파일 기계분할) | 8 | ✅ 완료·머지·정리 | pytest 1837 passed, cargo green |
| Round 2 (코어 로직 정리 + Rust 모듈별 정리) | 12 | ✅ 완료·머지·정리·Codex 리뷰 완료 | pytest 1776 passed, cargo green |
| Round 3 (UI 다이얼로그/메인윈도우/워커) | 8 | ⬜ 미착수 | — |

main 현재 HEAD: `d087f22` (fix(review): Codex Round 2 리뷰 findings 2건 반영 (#239))

**남은 worktree**: `main` + `.claude/worktrees/worktree-feat-table-collation` (이 작업과 무관한 다른 브랜치, 손대지 말 것).

---

## 3. Round 1 — 무엇을 했는가 (8 WP)

Enabler 라운드: 공유 모듈 추출 + 갓파일 기계적 분할. 이후 라운드가 의존하는 기반 작업.

| WP | 내용 | 결과 PR |
|---|---|---|
| WP-1.1 | `db_core_service.py`(782줄) → client/facade/dbapi_shim 3계층 분리 | #218 |
| WP-1.2 | `sql_validator.py`(869줄) → identifier_utils/metadata/validator/autocompleter 4모듈 | #219 |
| WP-1.3 | `ConfigManager` 그룹 CRUD → `TunnelGroupManager` 추출 | #217 |
| WP-1.4 | `i18n.py`(1627줄) → keys/legacy_translate/qt_hooks 패키지 | #224 |
| WP-1.5 | 마이그레이션 공유 상수 통합 (IssueType 표시명 5중복 등) | #221 |
| WP-1.6 | 스타일 시스템 통합 (settings.py 인라인 스타일 → ButtonStyles) | #222 |
| WP-1.7 | 릴리스 스크립트 dedup (smart_release → versioning import) | #220 |
| WP-1.8 | **Rust `lib.rs` 17,006줄 → 11개 모듈 분할** (`lib.rs`는 25줄 재수출 루트로 축소) | #225 |
| (게이트 픽스) | rust-core-regression-gate allowlist 보강 (분리 신파일 6~7개) | #223 |

최종 통합검증: pytest 1837 passed / 0 failed, cargo test 216+통합 전부 pass, build --release 경고 0.

---

## 4. Round 2 — 무엇을 했는가 (12 WP)

코어 로직 정리 + Round 1이 만든 Rust 11모듈 각각의 로컬 정리.

| WP | 내용 | 결과 PR |
|---|---|---|
| WP-2.1 | `MigrationAnalyzer` 갓클래스 → 4개 협력 모듈 분해, dead FixQueryGenerator 삭제 | #233 |
| WP-2.2 | `migration_fix_wizard.py`(1472줄) → 5개 도메인 모듈 | #231 |
| WP-2.3 | `migration_rules/` (SchemaRules 믹스인화) + parsers + dump_analyzer 정리 | #236 |
| WP-2.4 | `scheduler.py`(1078줄) → 6개 책임별 모듈 | #227 |
| WP-2.5 | schema 도구군 정리 (compare_indexes/foreign_keys 통합 등) | #229 |
| WP-2.6 | `rust_dump_exporter.py`(921줄) → foreign_key_resolver.py + dump_progress.py 분리 | #228 |
| WP-2.7 | `cross_engine_migration.py` render_result_report 9단 중첩 분해 | #226 |
| WP-2.8 | 인프라 터널/플랫폼 소파일 15건 (tunnel_health_checker.py 신설 등) | #232 |
| WP-2.9 | 인프라 네트워크/SQL 소파일 12건 | #230 |
| WP-2.10 | Rust `dump.rs`/`import.rs` 정리 | #238 |
| WP-2.11 | Rust `query.rs`/`schema.rs`/`oneclick.rs` 정리 | #235 |
| WP-2.12 | Rust `migrate.rs`/`dump_format.rs`/`ddl.rs` 정리 (구 WP-2.13 CC-252/253 흡수) | #234 |
| (게이트 픽스) | allowlist 보강 (신파일 6개) | #237 |

최종 통합검증: pytest 1776 passed / 0 failed, cargo test 전부 pass, build --release 성공.

### Round 2 Codex 순차 리뷰 결과

12개 WP diff를 gpt-5.4로 순차 리뷰(동작보존 리팩터 특화 프롬프트). **실질 결함은 2건뿐**(WP-2.9):
1. `update_downloader.download_installer` 타임아웃 30s→10s 축소 (MED, 실질) — **수정 완료**
2. `MySQLConnector.schema_exists("")` True 반환 (LOW, 경미) — **수정 완료**

→ PR #239로 두 건 모두 수정·머지됨. 이 과정에서 **부수 발견**: `migration_fix_models.py`의 TYPE_CHECKING 전용 MySQLConnector import가 게이트 allowlist 스캔 사각지대(들여쓰기)로 누락되어 main이 계속 red였음 — 함께 수정.

나머지 10개 WP는 전부 APPROVE 또는 검증 결과 오탐(non-issue). 상세는 아래 "Codex 리뷰 패턴" 참조.

---

## 5. Round 3 — 다음에 할 일 (8 WP, 미착수)

마스터플랜에 스펙이 이미 있다: `.claude/clean-code-master-plan-2026-07-09.md`의 "Round 3" 섹션.
**⚠️ 스펙 라인번호는 Round 1+2 머지로 완전히 무효화됐다** — Round 1 때보다 더 심하다. 반드시 심볼명 기준으로 현재 코드를 찾아 작업해야 한다 (아래 패턴 참조).

| WP | 내용 | 예상 파일 | 신규 브랜치명 |
|---|---|---|---|
| WP-3.1 | `SQLEditorDialog`(2,608줄) 갓클래스 분해 | sql_editor_dialog/workers/code_editor 등 | refactor/cc-r3-sql-editor-decomposition |
| WP-3.2 | `RustDumpExportDialog`/`RustDumpImportDialog` SRP 분해 | db_export/import_dialog + constants.py | refactor/cc-r3-db-export-import-dialogs |
| WP-3.3 | db_dialogs/db_connection_dialog/tunnel_status_dialog 정리 | 소파일 3~4개 | refactor/cc-r3-db-misc-dialogs-cleanup |
| WP-3.4 | `MigrationAnalyzerDialog` 분해 + `oneclick_migration_dialog` + worker | migration_dialogs.py 등 | refactor/cc-r3-migration-dialogs-ui |
| WP-3.5 | fix_wizard_* 페이지 5개 + fix_wizard_worker 정리 | fix_wizard_dialog/preview/execution 등 | refactor/cc-r3-fix-wizard-pages-cleanup |
| WP-3.6 | `CrossEngineMigrationDialog`(1450줄) + endpoint_form + diff_dialog 분해 | cross_engine/diff 계열 | refactor/cc-r3-cross-engine-diff-dialogs |
| WP-3.7 | `SettingsDialog`(31메서드 갓클래스) + schedule_dialog + tunnel_config | settings/schedule/tunnel_config | refactor/cc-r3-settings-schedule-tunnel-dialogs |
| WP-3.8 | `TunnelManagerUI`(~1220줄 갓클래스) → 컨트롤러 분해 + workers 정리 + main.py | main_window.py 등 | refactor/cc-r3-main-window-workers-cleanup |

정확한 findings 목록/가이드/파일목록은 마스터플랜 JSON에서 추출:
```bash
python3 -c "
import json
plan = json.load(open('.claude/clean-code-master-plan-2026-07-09.json', encoding='utf-8'))
specs = {s['wp_id']: s for s in plan['specs']}
print(json.dumps(specs['WP-3.1'], ensure_ascii=False, indent=2))  # WP 번호 교체해서 확인
"
```

**Round 3 특이사항 (미리 알아둘 것)**:
- PyQt UI 파일이 많음 → 테스트가 `QT_QPA_PLATFORM=offscreen` 필요할 수 있음, hang 주의.
- `SettingsDialog`/`SQLEditorDialog`/`main_window.py`는 여러 WP가 참조하는 파일이 많아 disjoint 검증을 **특히 꼼꼼히** 할 것 (Round 2 때 WP-2.1/2.2가 `migration_fix_models.py`를 둘 다 살짝 건드릴 뻔한 사례처럼).
- UI 다이얼로그 분할로 신규 파일이 또 생기면 **게이트 allowlist 재발 가능성 높음** (아래 패턴 5 참고, 이번엔 처음부터 전체 트리 재스캔 방식 사용 권장).
- WP-3.8(main_window)은 Round 1/2에서 갈라진 여러 모듈(group_manager, tunnel_health_checker 등)을 소비하는 최상위 파일이라 마지막에 머지하는 게 안전(마스터플랜 머지순서 권고에도 명시됨).

---

## 6. 패턴 플레이북 — Round 3(및 향후)에 그대로 재사용

### 6.1 팀 구성 전 필수 사전검증 (1회만 하면 됨, 이미 확인됨 — 재확인 불필요)

- **worktree 격리 실증됨**: editable install(`src` → main 절대경로 finder)이지만 pytest가 `sys.path[0]`에 worktree root를 넣어 worktree src를 정확히 사용함 (Round 1에서 probe worktree로 실증).
- **config 경합 차단**: 각 팀원 pytest 실행 시 `LOCALAPPDATA=<worktree>/.cc_appdata` 로 격리 지시(각 WP 프롬프트에 포함).

### 6.2 worktree/브랜치 세팅 절차

```bash
# N개 worktree 생성 (main 기준)
for wp in ...; do
  git worktree add "C:/Users/QESG/sh-project/tf-cc-3-<N>" -b "refactor/cc-r3-<slug>" origin/main
  # .venv junction (필수 — pip install 절대 금지)
  powershell -Command "New-Item -ItemType Junction -Path '<WT>\.venv' -Target '<MAIN>\.venv' -Force"
done
```

- Round 3는 순수 Python UI라 **Rust target junction 불필요** (Round 1/2의 Rust WP만 필요했음, 그리고 Rust WP끼리는 target junction을 **하지 않는다** — 공유 시 서로 빌드 clobber).
- 각 worktree에 `_WP_SPEC.md`를 스크립트로 생성해서 배치 (마스터플랜 JSON → 파일별 스펙 텍스트 변환, Round 1/2에서 쓴 `gen_report.py` 류 패턴 재사용 가능).
- **스펙 노후화 경고를 반드시 각 `_WP_SPEC.md` 상단에 주입**: "라인번호 무효, 심볼명으로 현재 코드 위치 찾을 것, 이미 R1/R2에서 해결된 finding은 skip하고 보고에 명시."

### 6.3 팀원 에이전트 프롬프트 템플릿 (검증된 구조)

각 팀원에게 준 프롬프트 구조 (Round 1/2에서 실제로 잘 작동함):
1. 워크트리 경로/브랜치명/Python 경로(junction) 명시
2. `_WP_SPEC.md` 최우선 정독 지시 + 노후화 경고 재강조
3. 환경 주의: Bash cwd 리셋되니 매 명령 `cd <WT> && ...`, pytest는 `LOCALAPPDATA=<WT>/.cc_appdata`
4. 절대 원칙: 순수 동작보존(재수출/facade로 import 경로 유지), files_touched 밖 수정 금지(STOP & SendMessage), 버전 bump 금지, `_WP_SPEC.md`/`.cc_appdata` 커밋 금지
5. 절차: 구현 → py_compile → 타겟 pytest → 전체 pytest 1회(macOS 패키징 테스트 실패는 무시하도록 명시) → 커밋(한국어 conventional + Co-Authored-By) → push → `gh pr create` → TaskUpdate completed → SendMessage로 main에게 PR URL+요약+검증결과+위험영역 보고(한국어 250~300단어)
6. 금지사항 목록

**모델 선택 기준** (사용자 지시: "스펙이 명확하면 sonnet, 추가 사고과정 필요하면 opus"):
- 갓클래스/갓파일 대규모 분해 (복잡한 판단 필요) → **opus**
- 단순 cleanup/소파일 정리 (스펙이 명확) → **sonnet**
- Round 3 권장: WP-3.1(SQLEditorDialog), WP-3.2, WP-3.4, WP-3.6, WP-3.7, WP-3.8(대형 갓클래스) → opus / WP-3.3, WP-3.5 → sonnet (판단은 착수 시점에 재확인)

### 6.4 매니저(자신) 책임 — 실행 중

- **TaskCreate로 WP마다 task 등록**, owner 할당 + in_progress.
- 팀원이 완료 보고하면 **PR 실체를 직접 검증** (`gh pr view <N> --json files,mergeable` — 절대 보고만 믿지 말 것).
- **전체 PR 모인 후 일괄 disjoint 스캔** (파일 겹침 0 확인):
  ```python
  # PR별 files 수집 → owner dict로 겹침 검출 (Round 1/2에서 실사용한 패턴)
  ```
- 중복 배정 알림(팀원이 "이미 완료" 재확인 메시지 보내는 것)은 정상 — 조치 불필요, 그냥 인지만.
- 다른 세션의 무관한 teammate 메시지(`wp4X-agent` 등 이름이 다른 것)는 무시.

### 6.5 ⛔ rust-core-regression-gate allowlist 함정 (반복 발생, 필독)

`scripts/rust-core-regression-gate.ps1`의 `$allowedEngineLocked`는 `MySQLConnector`를 직접 import해도 되는 파일 화이트리스트. **이 게이트는 non-blocking**(PR mergeStateStatus=UNSTABLE로만 뜨고 머지는 막지 않음) 이라서 놓치기 매우 쉽다.

- **함정**: allowlist에 있는 파일(예: `db_dialogs.py`, `migration_fix_wizard.py`)을 분할하면 **신규 자식 파일**이 import를 상속받는데, 신규 파일이 allowlist에 없으면 게이트 red.
- **1차 함정 대응이 불완전했던 경험**: diff에서 `+from src.core.db_connector import MySQLConnector` (줄 시작 `+from`)로 grep했더니 **들여쓰기된 import**(`if TYPE_CHECKING: \n    from ...`)를 놓쳤다 (Round 2에서 `migration_fix_models.py` 케이스, main이 한동안 red로 방치됨).
- **올바른 방법 (Round 3부터 이걸로)**: PR들 머지 직전에, **diff 스캔이 아니라 저장소 전체 트리를 들여쓰기 무관 정규식**으로 스캔해서 allowlist와 대조:
  ```python
  import re, os
  allow = { ... }  # 스크립트에서 allowlist 파싱
  needle = re.compile(r'from src\.core\.db_connector import MySQLConnector')
  hits = []
  for base in ('src/core','src/ui'):
      for root,_,files in os.walk(base):
          if '__pycache__' in root: continue
          for fn in files:
              if fn.endswith('.py'):
                  p = os.path.join(root,fn).replace(os.sep,'/')
                  if needle.search(open(p, encoding='utf-8').read()):
                      hits.append(p)
  missing = sorted(set(hits) - allow)
  ```
- **처리 순서**: 신규 파일 발견 → allowlist 보강 브랜치/PR을 **가장 먼저** 만들어 CI green 확인 → 그 다음 기능 PR들 순차 머지.

### 6.6 머지 절차 (검증된 순서)

1. 모든 PR이 모이면 `gh pr list --state open` 로 목록 확보, 전부 `mergeable=MERGEABLE`인지 확인.
2. disjoint 스캔 (6.4) + gate allowlist 스캔 (6.5).
3. 게이트 보강 PR을 **가장 먼저** 머지 → CI green 확인.
4. 기능 PR들을 `gh pr merge <N> --squash` 로 순차 머지 (파일 겹침 없으면 순서 임의, 있으면 리뷰어 권고 순서를 따름 — 이번엔 마스터플랜의 Round 3 머지순서 참고).
5. main pull 후 **cargo build/test는 Python pytest와 동시에 돌리지 말 것** — pytest가 Rust 바이너리를 실행 중이면 cargo build가 파일 락으로 실패한다(`os error 5`). Round 3는 순수 Python이라 이 문제는 없을 가능성 높지만, 혹시 Rust 관련 테스트가 있으면 순차 실행.
6. 최종 통합검증: `pytest -q` 전체 (macOS 패키징 테스트 제외 실패 0 확인) — `grep -v test_rust_core_packaging` 로 필터링.
7. **12/8개 전부 머지 확인 후에만** worktree 정리 (아래 6.7) + 에이전트 shutdown.

### 6.7 macOS 패키징 테스트는 항상 무시

`tests/test_rust_core_packaging.py`의 `test_macos_*` 계열은 로컬(Windows)에서 **환경 의존 flaky**(0~22건 사이 요동, 실행할 때마다 다름). 회귀 판정은 반드시:
```bash
pytest -q 2>&1 | grep -E "^FAILED" | grep -v "test_rust_core_packaging"
```
이 결과가 비어있으면 회귀 0으로 판정. base main과 대조해서 동일 수치면 확정.

상세: `~/.claude/projects/.../memory/macos-validation-test-flaky-local.md` (auto-memory에 기록됨, 새 세션에서도 자동 로드됨).

### 6.8 worktree 정리 절차 (Round 1/2에서 실사용, 안전)

```bash
# 1. 스트레이 프로세스 정리 (Rust WP였다면 tunnelforge-core.exe 잔존 확인)
powershell -Command "Get-Process tunnelforge-core -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '*sh-project*' } | Stop-Process -Force"

# 2. wt-cleanup.ps1 (user-level, junction 안전분리 내장)
for n in 1..8; do
  powershell -File "C:/Users/QESG/.claude/scripts/wt-cleanup.ps1" -Path "C:\Users\QESG\sh-project\tf-cc-3-$n"
done

# 3. junction 0개 검증 후 force remove
git worktree remove --force <path>
git worktree prune

# 4. 로컬+원격 브랜치 삭제
git branch -D <branches...>
git push origin --delete <branches...>
```

### 6.9 Codex 순차 리뷰 패턴 (Round 3 완료 후 적용)

사용자가 "Round 1에서 했던 것처럼 순차 Codex 검증" 요청 시 이 패턴 재사용:

1. **인프라**: `$HOME` 확인(`/c/Users/QESG`), `node "$HOME/.claude/bin/codex-review.mjs"` 존재 확인, SID 생성.
2. **⚠️ 중요: WP가 8개 이상이면 처음부터 각 WP를 독립 fresh thread(`codex-review start`, follow-up 아님)로 리뷰할 것.** Round 2에서 동일 thread에 6~7개 follow-up을 누적했더니 thread crash 발생(`No session found`). follow-up 방식은 4~5개 이내로 제한하거나 처음부터 매 WP `start`로 새 세션.
3. 리뷰 프롬프트 템플릿 (Round 2에서 검증됨):
   - "이것은 동작보존 리팩터다. 이동된 코드 스타일은 무시하고, 실제 동작변경/깨진 재수출/추출헬퍼 버그/순환참조/(Rust면) 가시성·borrow·match semantics에 집중하라"
   - 이전 라운드에서 의도적으로 변경된 동작(예: CC-088의 이슈카운트 변화)이 있으면 "이건 스펙 지시사항이니 구현이 틀렸을 때만 플래그하라" 명시
   - Rust WP는 "런타임 경로 자동테스트 없음, 수작업 검토 특히 중요" 강조
4. **Codex가 HIGH/MED를 내도 곧바로 사용자에게 올리지 말고 먼저 매니저가 직접 검증**: `grep -rn` 으로 실제 참조/호출자 확인. Round 2에서 Codex가 낸 이슈 중 실질 결함은 12건 중 2건뿐이었고 나머지는 오탐(dead code 삭제를 회귀로 오인 등)이었다.
5. 실질 결함만 별도 PR(`fix/cc-r<N>-review-followup`)로 수정 → 검증 → 머지. **이 PR도 게이트 allowlist 영향이 있는지 반드시 재확인** (Round 2에서 실제로 여기서 사각지대 발견됨).
6. 리뷰 완료 후 `codex-review close`로 모든 세션 정리.

---

## 7. 세션 시작 시 체크리스트 (새 세션용)

1. `git log --oneline -1` 로 main이 `d087f22` 이후인지 확인 (더 진행됐으면 이 문서가 낡은 것 — git log로 뭐가 더 됐는지 먼저 파악).
2. `git worktree list` 로 잔여 worktree 없는지 확인 (있으면 이전 세션이 비정상 종료된 것 — 안전하게 정리 후 시작).
3. `.claude/clean-code-master-plan-2026-07-09.md` 의 Round 3 섹션 정독.
4. 사용자에게 Round 3 착수 여부 확인 (AskUserQuestion) — team-launch 방식 재확인, 모델 배분 재확인.
5. 6.1의 사전검증은 이미 끝났으므로 재실행 불필요, 바로 6.2(worktree 세팅)부터 시작.

---

*작성: 2026-07-09, Round 1/2 완료 세션에서 인계.*
*관련 파일: `.claude/clean-code-master-plan-2026-07-09.md`(.json), `.claude/investigation-clean-code-audit-2026-07-09.md`(.json), auto-memory의 관련 항목들(worktree cleanup, macOS flaky, rust-core-gate-allowlist, rust-module-split-mechanics, dump-import-runtime-test-gap, team-agent-ops-pitfalls, god-file-split-pitfalls).*
