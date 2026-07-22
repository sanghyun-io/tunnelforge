# TunnelForge 전수조사 해결 결과 (2026-07-08)

감사 보고서(.claude/investigation-full-audit-2026-07-08.md)의 확정 154건을 4개 라운드 30개 WP로 전건 해결.
마스터 계획: .claude/audit-master-plan-2026-07-08.md (Codex 설계, 라운드 내 파일충돌 0 검증됨)

## 라운드별 병합 내역
| Round | WP | PR | 통합 검증(main) |
|---|---|---|---|
| 1 (코어 계약) | 6 | #174-#179 | 1918 passed |
| 2 (코어/로직) | 11 | #180-#190 | 2035 passed |
| 3 (UI/스레딩) | 10 | #191-#200 | 2134 passed |
| 4 (죽은코드 제거) | 3 | #201-#203 | 1807 passed (retired 테스트 삭제로 감소) |

## 최종 검증 (main 7c10f27)
- 전체 pytest: 1807 passed, 0 failed
- cargo test (migration_core): 전부 ok
- 앱 UI 스모크: success=True, core_hello=True
- 열린 audit PR: 0 / 잔여 audit worktree·브랜치: 0

## 대표 해결 항목 (HIGH 6)
1. sql-editor DB 콤보 전환 후 이전 DB로 쿼리 실행 → 재연결 가드 (#190)
2. 쿼리 실행 중 processEvents 재진입 → QThread+시그널 (#190)
3. MySQL DDL 암묵 커밋 거짓 롤백 약속 → 확인 UX + auto_committed 표시 (#190)
4. PostgreSQL aborted 트랜잭션 "커밋 완료" 오보 → fail-fast rollback (#190)
5. import 중 다이얼로그 닫힘 → 닫기 가드 + 실제 취소 (#198)
6. 원클릭 마이그레이션 가짜 취소 → 취소 UX 정직화 (#194)

+ 최초 보고 버그(0행 SELECT가 "미커밋 변경: 1건"으로 오분류)는 #177(RustDbCursor description 계약)+#190(is not None 판별)으로 해결.

## 후속 권고 (감사 범위 밖 발견)
- CLAUDE.md의 "ForeignKeyResolver가 partial export FK 포함 담당" 서술이 부정확해짐(#182에서 _resolve_required_tables_from_rust_schema로 대체) → 문서 갱신 권장
- scripts/rust-core-regression-gate.ps1:29 의 metadata_worker.py 잔재 항목(무해) → 정리 권장
- main_window의 _context_rust_core_export/import/orphan_check 3개 메서드가 show_context_menu 삭제로 호출부 없음 → 후속 정리 대상
