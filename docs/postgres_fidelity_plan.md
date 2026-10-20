# PostgreSQL dump fidelity plan (TF-STATUS-205, 208, 209)

Status (2026-10-20): PR1 (reason codes) and PR2 (generated columns, PostgreSQL expression checker, single-statement DDL) shipped with TF-STATUS-207 in v2.22.0. The owner paused the rest after TF-STATUS-209; PR3-PR5 below and TF-STATUS-208 are not started. Line numbers refer to main before TF-204/TF-205 and must be re-located. Track progress in `docs/current_status.md`.

Source: the 2026-10-19 design workflow (4 readers, 2 critics). Sentences are the planned Korean UI text; each needs an exact English entry in `src/core/i18n/legacy_translate.py`.

## Summary

TF-STATUS-205 최종 설계: PostgreSQL→PostgreSQL 덤프가 계산 열, 함수·프로시저, 트리거(트리거 함수 포함)를 담고 일반 replace Import로 그대로 복원하게 만든다. 두 비평의 지적 21건 가운데 확인된 것은 모두 반영했다. 근거 코드는 small-agenda(origin/main)와 TF-204 작업 트리에서 직접 읽어 확인했다.

포맷은 새 번호 없이 MySQL이 쓰는 6/7을 그대로 쓴다.
- 계산 열은 NormalizedColumn.generated에 담는다(format 6).
- 함수·프로시저·트리거는 DumpManifest.stored_programs에 담는다(format 7). 엔진은 manifest.source_engine='postgresql'로 구분한다.
- 추가 필드는 네 개다. 프로그램 항목에 arguments(형식만 있는 호출 시그니처), tables, public_execute를 두고, manifest에 required_extensions를 둔다.
- v2.20.0 이전 앱은 이 덤프를 대상 변경 전에 거부한다. 새 객체가 없는 덤프는 바이트 단위로 지금과 같다.

비평을 반영해 바뀐 핵심은 다음과 같다.
1. 계산 열 식 검사기를 PostgreSQL 규칙으로 새로 만든다. 지금 검사기는 MySQL 따옴표 규칙이라, 역슬래시를 넣은 식이 검사를 통과한 뒤 PG에서 문장을 이어 붙일 수 있었다(ddl.rs:38, adapters.rs:807·825). PG CREATE TABLE과 probe 문장은 한 문장만 받는 extended protocol(execute_single)로 보낸다. 이것을 PR2로 앞당긴다.
2. 덤프 테이블의 행 형식이나 원본 스키마의 형식을 인자·반환 형식으로 쓰는 함수는 export에서 사유 코드(routine_type_unsupported)를 붙여 뺀다. 함수를 먼저 만들면 DROP TABLE이 중간에 실패하기 때문이다(import.rs:1182-1210, ddl.rs DROP에 CASCADE 없음).
3. probe에서 계산 열·트리거·함수가 대상 스키마의 다른 함수·연산자에 묶이지 않았는지 pg_depend로 확인한다(program_name_resolution_changed).
4. 덮어쓸 대상 함수를 덤프 밖 인덱스·계산 열·제약이 쓰고 있으면 아무것도 바꾸기 전에 멈춘다(function_has_external_dependents).
5. 일부 export·Import에서 함수가 부르는 다른 함수가 빠지지 않게 한다. 필요한 함수가 하나라도 있으면 원본 스키마 함수를 전부 담는다. 일부 Import는 필요한 함수는 교체하고, 대상에 없는 덤프 함수는 새로 만든다(덮어쓰지 않는다).
6. 함수 재확인은 이름과 형식만으로 대상 함수를 찾는다. 본문은 정확히 비교하고, 서버가 만든 앞부분만 canonical_sql로 비교한다.
7. 프로그램 생성·건너뜀 판정을 '덤프 엔진 == 대상 엔진' 하나로 맞춘다(import.rs:441·526·538·550, import_preview.rs:105·125·203).
8. TF-204가 copy_refusals의 프로그램 거부를 지웠으므로, PR3에서 PG 프로그램 덤프 거부를 명시적으로 다시 넣는다.
9. PG probe는 ROLLBACK으로 정리하고 DROP 정리 문장을 쓰지 않는다. lock_timeout을 걸고 FK는 맨 끝에 만든다.
10. 테이블마다 트리거와 활성 상태를 한 트랜잭션에서 만든다. PG에서도 trigger_tables를 채운다.

Import 순서:
1. preflight(함수 외부 의존 검사 포함)
2. probe 트랜잭션(BEGIN…ROLLBACK)
3. 함수 트랜잭션: 의존 순서대로 CREATE OR REPLACE하고 COMMIT
4. 테이블마다 DROP/CREATE → COPY(계산 열 제외) → 인덱스·FK → 그 테이블의 트리거와 상태(한 트랜잭션)
5. views

cross-engine 규칙:
- 계산 열은 대상 변경 전에 unsupported_generated_column으로 거부한다.
- 저장 프로그램은 건너뛰고 programs_skipped_cross_engine으로 보고한다. 양방향 같다.

PR은 다섯 개다.
- PR1: 사유 코드
- PR2: 계산 열과 안전한 PG DDL 실행
- PR3: 함수·프로시저
- PR4: 트리거
- PR5: 뒤로 미룬 PG verify-in-copy. digest 호환 조건을 추가했다.

PR2와 PR3은 TF-204(feat/safe-restore-programs)가 먼저 머지된 뒤 그 위로 rebase한다.

## Owner decisions (all took the recommended answer)

- **다른 DB 종류로 가져올 때(MySQL 덤프→PG, PG 덤프→MySQL) 덤프 안의 트리거·함수를 어떻게 할까요? 지금은 만들지 않고 '건너뜀' 목록으로 보여 주며 테이블 데이터는 가져옵니다.**
  - Chosen: 지금처럼 건너뛰고 객체마다 보고합니다. 계산 열만 대상 변경 전에 거부합니다.
- **최소 몇 버전의 PostgreSQL에서 이 기능이 동작해야 하나요? CI의 덤프 테스트는 PG 18.4뿐입니다. VIRTUAL 계산 열은 PG18, SQL 표준 함수 본문은 PG14 기능입니다.**
  - Chosen: PG 13 이상을 지원합니다. 버전마다 다른 카탈로그 열은 to_jsonb(row)->>'col'로 읽습니다. CI dump 잡에 postgres:13 서비스를 하나 더 두고 live_postgres_objects를 PG 13과 18.4 양쪽에서 돌립니다(generated_virtual_unsupported_on_target 테스트 포함).
- **원본에서 실행 권한을 좁혀 둔 함수(예: PUBLIC EXECUTE를 회수한 SECURITY DEFINER 함수)를 대상에 새로 만들 때 권한을 어떻게 할까요? PostgreSQL은 새 함수에 PUBLIC 실행 권한을 자동으로 줍니다.**
  - Chosen: 같은 트랜잭션에서 PUBLIC EXECUTE를 회수하고 '권한을 다시 주세요' 보고 줄을 냅니다. 교체한 함수는 대상 권한을 유지합니다.
- **대상에 이름과 인자 형식이 같은 함수가 있는데 반환형이나 인자 이름이 달라 그대로 덮어쓸 수 없으면 어떻게 할까요?**
  - Chosen: 아무것도 바꾸기 전에 function_replace_incompatible로 멈추고, 대상 함수를 직접 정리하라고 안내합니다.
- **PG 트리거의 꺼짐 상태(DISABLED, REPLICA, ALWAYS)를 복원할까요?**
  - Chosen: 원본 상태 그대로 복원합니다. 트리거 생성과 상태 변경을 한 트랜잭션에서 합니다.
- **PG CHECK 제약과 GENERATED ALWAYS identity도 이번에 담을까요? PG 스키마에서 흔해서 지금도 PG export가 막히는 주된 원인일 수 있습니다.**
  - Chosen: TF-205에는 넣지 않고 PR4 바로 뒤의 후속 이슈로 진행합니다. PR2의 PG 식 검사기와 재확인 장치를 그대로 씁니다. 그때까지는 PR1의 사유 코드로 거부합니다.
- **테이블 행 형식을 인자나 반환 형식으로 쓰는 함수(예: RETURNS SETOF orders, f(orders))는 PG 앱에서 흔합니다. 이런 함수를 이번에 담을까요? 담으려면 가져올 때 대상의 같은 함수를 먼저 지우고(DROP FUNCTION) 테이블을 만든 뒤 다시 만들어야 합니다. 그러면 대상 함수의 권한과 그 함수를 쓰는 대상 객체가 영향을 받습니다.**
  - Chosen: 이번에는 routine_type_unsupported(빼고 내보내기 가능)로 거부하고 문장으로 이유를 알립니다. 실제 수요가 확인되면 '테이블 뒤에 만들기 + 대상 같은 함수 DROP' 순서를 따로 설계합니다.
- **일부 테이블만 가져올 때 덤프에 있지만 대상에 없는 함수를 새로 만들어도 될까요? 트리거 함수가 본문에서 부르는 다른 함수는 DB가 의존 관계로 기록하지 않습니다. 그래서 만들지 않으면 가져오기는 성공해도 나중에 INSERT할 때 오류가 날 수 있습니다.**
  - Chosen: 대상에 없는 덤프 함수만 새로 만들고(기존 함수는 덮어쓰지 않음) 목록을 보고합니다.

## Format

새 format 번호는 쓰지 않는다. 설계상 예약된 8/9(MySQL 2단계, 미출시)와도 겹치지 않는다. 바뀌는 것은 모두 serde default + skip_serializing_if다. 그래서 MySQL 덤프와 새 객체가 없는 PG 덤프는 바이트 단위로 같고, PG 덤프는 계속 format 3이다(live_dump_cross_engine.rs:54).

1. 계산 열(PR2): 기존 NormalizedColumn.generated{expression, stored}를 그대로 쓴다.
- expression은 원본 스키마 search_path 아래에서 얻은 pg_get_expr(adbin, adrelid) 원문이다.
- stored는 attgenerated 's'면 true, 'v'면 false(PG18)다.
- 엔진이 PG인 덤프의 expression은 is_safe_pg_expression으로 검사한다. MySQL 덤프는 지금처럼 is_safe_check_expression으로 검사한다.
- dump.rs:1016 규칙상 generated가 있으면 6이 된다.
- 구 reader: v2.19 이하는 6을 거부한다. v2.20은 mysql_column_direction_refusals(ddl.rs:127, source != mysql)가 DROP 전에 거부한다.

2. 함수·프로시저·트리거(PR3·PR4): 기존 DumpManifest.stored_programs에 PG 항목을 넣는다. manifest.source_engine == "postgresql"일 때만 PG 규칙으로 해석한다.
- kind: "FUNCTION" | "PROCEDURE" | "TRIGGER"
- name
- table, timing("BEFORE" | "AFTER"), event(원문 이벤트 목록): TRIGGER만
- definition:
  - FUNCTION/PROCEDURE는 pg_get_functiondef 출력에서 앞부분 `CREATE OR REPLACE <KIND> <schema>.<name>`을 뗀 나머지다. `(`로 시작한다.
  - TRIGGER는 pg_get_triggerdef(oid, true) 출력에서 ` ON <table> ` 뒤의 나머지다. `FOR EACH` 또는 `REFERENCING`으로 시작한다.
- status: TRIGGER만. 'D'/'R'/'A'를 "DISABLED"/"REPLICA"/"ALWAYS"로 쓰고, 'O'면 생략한다.
- definer: 원본 소유 역할, 보고용
- MySQL 전용 필드는 PG에서 비운다.
- 배열 순서는 생성 순서다. 함수는 pg_depend(pg_proc→pg_proc, 인자 DEFAULT 등) 위상 순서다.

NormalizedStoredProgram에 새로 추가하는 선택 필드:
- `arguments: Option<String>`: FUNCTION/PROCEDURE의 oidvectortypes(proargtypes)를 원본 search_path 아래에서 읽은 값이다. 예: 'integer, text[]'. 형식만 있는 호출 시그니처로, 이름과 모드가 없고 PG14+ 프로시저의 OUT 인자는 들어간다. 오버로드 식별, 대상 함수 조회(to_regprocedure(format('%I.%I(%s)', 대상, name, arguments))), 표시에 쓴다.
- `tables: Vec<String>`: 이 함수를 쓰는 덤프 테이블이다. 트리거의 pg_depend(tgfoid와 WHEN 안의 함수)와 계산 열 attrdef의 pg_depend에서 채운다.
- `public_execute: Option<bool>`: 원본에서 PUBLIC에 EXECUTE가 없었을 때만 Some(false).

DumpManifest에 새로 추가하는 선택 필드:
- `required_extensions: Vec<String>`: stored_programs가 비어 있지 않을 때만, 원본 DB의 pg_extension.extname(plpgsql 제외)을 담는다. 본문이 부를 수 있는 확장을 알리는 용도라 경고에만 쓴다.

dump.rs:1016 규칙상 stored_programs가 비어 있지 않으면 7이 된다.

구 reader:
- v2.19 이하는 7을 거부한다.
- v2.20 replace/preview는 import.rs:81 'stored programs require a MySQL source'(export_invalid)로 DROP 전에 거부한다.
- v2.20.0 safe는 copy_refusals의 dump_programs_unsupported_in_copy로 복사 전에 거부한다.
- MAX_SUPPORTED_DUMP_FORMAT_VERSION은 7 그대로다.

새 reader의 검증: PG 항목은 pg_programs.rs 검증기만 통과시킨다. 다음은 import_table_ddls에서 DROP 전에 export_invalid로 거부한다.
- 중복 키 (kind, name, arguments)
- 덤프에 없는 trigger table
- 허용되지 않는 kind
- source_engine과 맞지 않는 필드
- is_safe_pg_expression을 통과하지 못하는 PG 계산 열 식

3. InspectionResult에 `object_reasons: BTreeMap<String,String>`를 추가한다(PR1, serde default + skip empty). 덤프 파일 형식은 바뀌지 않는다.

## Design decisions

- format 6/7과 기존 필드를 재사용하고 번호를 올리지 않는다. PG 프로그램은 stored_programs에 넣고 source_engine으로 엔진을 구분한다. 추가 필드는 프로그램의 arguments·tables·public_execute와 manifest의 required_extensions뿐이다.
  - Why: 모든 구 reader(v≤2.19는 버전 게이트, v2.20은 import.rs:81, ddl.rs:127, safe_restore copy_refusals)가 대상 변경 전에 이미 실패한다. 재사용하면 held_object_ids, program_id, 결과 이벤트 개수, Python 보고 줄을 그대로 쓸 수 있다. required_extensions는 v2.20이 무시해도 그 덤프 자체가 import.rs:81에서 거부되므로 안전하다.
- PG 캡처는 LiveAdapter::connect(SET search_path TO 원본 스키마) 연결에서 pg_catalog를 직접 읽는다. 함수는 pg_proc(pg_depend deptype 'e' 제외), 트리거는 pg_trigger(NOT tgisinternal)에서 읽는다. inspect inventory도 같은 쿼리로 바꾸고 id 형식(trigger:t:n, routine:n)은 유지한다.
  - Why: information_schema는 권한에 따라 행을 거르므로 읽기 전용 계정에서 트리거가 경고 없이 빠진다. search_path를 원본 스키마로 두면 deparse에서 같은 스키마 한정자만 빠져 대상 스키마로 그대로 옮겨진다. id를 유지하면 scoped_* 판정과 Python 소비자가 그대로 동작한다.
- PG 식 검사기 is_safe_pg_expression을 ddl.rs에 새로 둔다(PR2). '' 이중화만 있는 표준 문자열, 역슬래시 이스케이프가 있는 E'', $tag$…$tag$ 달러 인용, "" 이중화가 있는 식별자를 읽는다. 다음은 거부한다: ;, --, /*, NUL, 괄호 밖 쉼표, 괄호 불균형, 닫히지 않은 인용, SELECT 같은 문장 키워드. #과 @ 연산자(#>>, @>, @ a)는 허용한다. source_engine이 postgresql이면 inspect의 증명(b)와 generate_table_ddl(ddl.rs:1366)이 이 검사기를 쓰고, 그 밖은 지금 검사기를 쓴다.
  - Why: 확인 결과 is_safe_check_expression(ddl.rs:27-61)은 '…' 안의 역슬래시를 이스케이프로 본다(L38). 그래서 `'\'), x int); DROP TABLE keep_me; …` 같은 식을 문자열 하나로 보고 통과시키지만, PG는 `'\'`를 닫힌 문자열로 읽는다. 지금은 ddl.rs:127이 PG DDL에 계산 열을 못 들어오게 막아서 드러나지 않았다. PR2가 이 막음을 풀면서 처음 노출된다.
- PG CREATE TABLE은 LiveAdapter::execute_single(postgres client.execute, extended protocol)로만 보낸다(PR2에 도입). create_new_table의 PG 분기(adapters.rs:824-826)와 run_owned_ddl_probe의 execute 클로저가 이것을 쓴다. 이 클로저는 CREATE TABLE, CREATE INDEX, FK, DROP을 한 문장씩만 보낸다. 여러 문장을 돌려주는 헬퍼가 있으면 나눠서 보낸다. execute_sql(batch_execute)은 SET 같은 내부 고정 문장에만 남긴다.
  - Why: 검사기가 틀려도 서버가 'cannot insert multiple commands into a prepared statement'로 문장 연결을 막는다. generate_table_ddl은 끝에 ;이 붙은 한 문장이라(ddl.rs:1335) extended protocol로 그대로 보낼 수 있다. 검사기와 프로토콜, 두 겹으로 막는다.
- 계산 열 증명(PR2)은 inspect에서 다음 세 조건을 모두 만족할 때만 generated를 채운다. (a) attgenerated가 s 또는 v다. (b) pg_get_expr 결과가 is_safe_pg_expression을 통과한다. (c) pg_attrdef의 pg_depend 참조가 같은 테이블 열이나 pg_catalog 객체뿐이다. PR3부터는 원본 스키마 함수도 허용하되, 그 함수를 실제로 캡처했을 때만 held다. 실패하면 generated는 None이고 lossy로 거부되며 reason은 generated_expression_unsafe 또는 generated_dependency_unsupported다. pg_get_expr는 그 쿼리만 감싼 짧은 트랜잭션의 set_config('search_path', 원본, true) 아래에서 돌린다.
  - Why: MySQL과 같은 fail-closed 방식이다. search_path를 그 쿼리에만 걸면 다른 PG 덤프의 type/default 문자열이 바뀌지 않는다.
- 계산 열 적재: copy_rows_csv_sql과 copy_csv_line_for_table(ddl.rs:831-863)이 같은 열 목록에서 generated 열을 뺀다. 덤프 청크에는 값이 계속 들어 있다. mysql_column_direction_refusals의 계산 열 조건을 source != target으로 바꾸고, INVISIBLE은 MySQL→MySQL 전용 그대로다. replace Import는 STORED 계산 열이 있는 테이블마다 generated_values_recomputed 보고 줄을 낸다.
  - Why: 청크 검증기는 모든 열을 요구한다. PG COPY는 열 목록에 계산 열이 있으면 거부한다(PR2 live 테스트로 확인). 다시 계산된 값은 collation이나 ICU 버전에 따라 원본과 다를 수 있는데 replace는 이를 비교하지 않는다. 그래서 숨기지 않고 알리고, 비교는 verify-in-copy가 한다.
- 함수 캡처 증명(PR3)은 다음 조건을 모두 만족할 때만 담는다. (a) prokind가 f/p다. (b) SQL 표준 본문이 아니다(to_jsonb(p)->>'prosqlbody' IS NULL). (c) 언어가 c/internal이 아니다. (d) pg_get_functiondef가 format('CREATE OR REPLACE %s %I.%I', kind, nsp, proname)와 '('로 시작한다. (e) prosrc가 정의 안에 그대로 들어 있다. (f) 이 함수가 pg_depend(classid pg_proc → refclassid pg_type)로 원본 스키마의 형식(덤프 테이블 행 형식, enum, domain, composite, 그 배열)을 참조하지 않는다. routine:<이름>은 그 이름의 모든 오버로드를 캡처했을 때만 held다. 실패 사유는 routine_kind_unsupported, routine_sql_body_unsupported, routine_language_unsupported, routine_definition_unproven, routine_type_unsupported이고 전부 생략 가능(waivable)이다. 담은 함수는 pg_depend(pg_proc→pg_proc) 위상 순서로 정렬한다.
  - Why: (f)는 두 비평이 함께 지적한 순서 결함을 막는다. 함수 트랜잭션이 테이블보다 먼저 COMMIT되면 f(orders)나 RETURNS SETOF orders는 옛 테이블 행 형식에 묶인다. 그러면 DROP TABLE(ddl.rs DROP에 CASCADE 없음)이 이미 함수가 바뀐 뒤에 실패한다. 대상에 이미 있으면 postgres_drop_dependents(import.rs:1182-1210)가 처음부터 거부한다. 위상 정렬은 인자 DEFAULT처럼 CREATE 시점에 해석되는 함수 간 참조 때문이다. check_function_bodies=off여도 이것은 해석된다.
- 트리거 캡처 증명(PR4): pg_get_triggerdef(oid, true)를 다음 조각으로 나눈다. 서버가 만든 `CREATE TRIGGER %I ` 접두사, timing, event, 따옴표 밖 첫 ` ON %I `, 나머지(definition). 조각을 다시 이어 붙였을 때 원문과 같아야 한다. CONSTRAINT TRIGGER, tgparentid≠0, view 위 트리거는 담지 않는다(trigger_kind_unsupported, view_trigger_unsupported). 트리거가 pg_depend로 참조하는 원본 스키마 함수(tgfoid와 WHEN 안의 함수)가 모두 held일 때만 트리거도 held다(trigger_function_not_held). 이 함수들의 tables에 트리거 테이블을 넣는다.
  - Why: ON은 예약어라 UPDATE OF 열 이름 안에서는 따옴표가 붙으므로, 따옴표 밖 첫 ` ON `이 테이블 위치다. tgfoid만 보면 WHEN 안의 함수를 놓친다(두 번째 비평).
- 일부 export와 일부 Import의 함수 범위: export는 선택 테이블의 트리거·WHEN·계산 열이 원본 스키마 함수를 하나라도 쓰면 원본 스키마의 확장 소유가 아닌 함수를 전부 담는다. 범위 밖 함수가 증명에 실패하면 거부하지 않고 WARNING_OBJECT_NOT_EXPORTED만 남긴다. 일부 Import는 두 가지를 한다. (1) tables가 선택 테이블과 겹치는 함수를 CREATE OR REPLACE한다. (2) 그 밖의 덤프 함수 가운데 대상에 같은 (이름, arguments)가 없는 것만 CREATE한다. 기존 대상 함수는 덮어쓰지 않고 programs_created_missing으로 보고한다. 나머지는 programs_skipped_partial이다. stored_programs가 있으면 required_extensions 가운데 대상에 없는 확장을 preview와 Import 보고에서 extension_missing_on_target 경고로 낸다.
  - Why: PG는 plpgsql·SQL 본문 안의 호출을 pg_depend에 남기지 않는다. 그래서 pg_depend만 따라가면 helper 함수가 빠지고, Import는 성공했는데 INSERT 때 42883으로 실패한다. 이런 조용한 손실은 원칙에 어긋난다. 없는 함수를 새로 만드는 것은 대상의 기존 객체를 바꾸지 않는다. 확장은 본문 의존이라 증명할 수 없으므로 경고로만 알린다.
- Export 일관성: 거부 판정 전에 캡처하고, 데이터 덤프 뒤 PG에서도 다시 캡처해 비교한다. 다르면 fatal 'stored programs changed during export; retry'로 끝낸다. 계산 열은 기존 PG lock 뒤 재-inspect가 지킨다.
  - Why: ACCESS SHARE lock은 CREATE TRIGGER나 CREATE OR REPLACE FUNCTION을 막지 않는다. pg_get_*def는 최신 카탈로그를 읽는다.
- Import 순서(PG):
1. 기존 preflight와 함수 외부 의존 검사
2. probe 트랜잭션: BEGIN; SET LOCAL check_function_bodies=off; SET LOCAL lock_timeout='5s'; → 적용 대상 함수를 위상 순서로 실제 이름에 CREATE OR REPLACE하고 정의 재확인 → probe 테이블(계산 열 포함)과 인덱스, 계산 열 재확인 → probe 테이블 위 트리거와 정의 재확인 → probe 객체의 pg_depend 이름 해석 확인 → FK(맨 끝) → ROLLBACK
3. 함수 트랜잭션: 같은 CREATE OR REPLACE(+REVOKE)를 하고 첫 DROP 전에 COMMIT
4. 테이블마다 DROP → CREATE(execute_single) → COPY → complete_imported_table → 그 테이블의 CREATE TRIGGER와 상태 ALTER(한 트랜잭션, COMMIT 뒤 checkpoint)
5. finalize의 views

PG probe는 ROLLBACK으로 정리하고 run_owned_ddl_probe의 DROP TABLE 정리(import.rs:931-945)를 건너뛴다. 트랜잭션 probe는 PR3에 도입한다. PR2의 계산 열 재확인은 지금의 autocommit probe에서 한다. skip_triggers 토글을 PG에도 연다.
  - Why: PG DDL은 트랜잭션 안에서 돌므로 실제 이름으로 만들어 보고 ROLLBACK하면 대상 변경 없이 증명할 수 있다. 확인 결과 지금 정리 단계는 같은 execute로 DROP을 보낸다. 실패해 중단된 트랜잭션 안에서는 이 DROP도 실패해 ddl_probe_cleanup_failed로 잘못 보고된다. FK를 맨 끝에 두고 lock_timeout을 걸면 라이브 부모 테이블의 SHARE ROW EXCLUSIVE lock 시간이 짧아진다. 트리거와 상태를 한 트랜잭션에 두면 꺼져 있어야 할 트리거가 켜진 채 남지 않는다.
- probe 재확인:
- 함수: 대상 OID를 to_regprocedure(format('%I.%I(%s)', 대상, name, arguments))로 찾는다. 달러 인용 본문(prosrc)은 덤프 정의의 본문과 정확히 비교한다. 서버가 만든 앞부분과 속성 줄만 canonical_sql로 비교한다.
- 트리거: pg_get_triggerdef(probe_oid,true)와 다시 조립한 정의를 canonical_sql로 비교한다.
- 계산 열: probe 열의 pg_get_expr를 expression과 canonical_sql로 비교한다.
다르면 DROP 전에 program_definition_changed 또는 generated_expression_changed로 거부한다.
  - Why: arguments는 형식만 있는 시그니처라 regprocedure 입력이 받는다. pg_get_function_identity_arguments는 인자 이름과 모드가 들어가 regprocedure 입력이 받지 못한다. canonical_sql(safe_restore.rs:991-1008)은 달러 인용을 몰라서, 본문을 소문자로 바꾸고 공백을 없애 '- -1'과 '--1'을 같게 만든다. 본문은 prosrc 그대로 저장·출력되므로 버전 차이 관대함이 필요 없다.
- 이름 해석 확인: probe 트랜잭션에서 probe attrdef, probe 트리거(WHEN 포함), 새로 만든 함수의 pg_depend 참조 가운데 대상 스키마 객체를 본다. 그것은 probe 테이블 자신이거나 덤프에서 만든 함수(이름+arguments)여야 한다. 다른 대상 스키마 함수·연산자·형식이면 program_name_resolution_changed로 DROP 전에 거부한다. pg_catalog와 한정자가 붙은 다른 스키마 참조는 허용한다.
  - Why: LiveAdapter::connect는 search_path를 대상 스키마로 둔다(adapters.rs:574-576). PG 함수 해석은 정확한 형식 일치를 schema 순서보다 우선한다. 그래서 다형 함수(예: pg_catalog.array_length(anyarray,int))를 대상 스키마의 array_length(int[],int)가 가로챌 수 있다. 그래도 deparse 텍스트는 똑같아 텍스트 재확인으로는 보이지 않는다. 한정자가 없는 이름은 pg_catalog나 대상 스키마로만 해석되므로 대상 스키마만 보면 된다.
- SQL 실행 보안: PG 함수·트리거 CREATE는 execute_single로만 보낸다. 헤더는 매번 직접 조립한다.
- 함수: `CREATE OR REPLACE <KIND> <qi(대상)>.<qi(name)>` + definition
- 트리거: `CREATE TRIGGER <qi(name)> <timing> <event> ON <qi(대상)>.<qi(table)> <definition>`
timing은 BEFORE/AFTER만, event는 (INSERT|UPDATE [OF 식별자,…]|DELETE|TRUNCATE)를 OR로 이은 것만 허용하는 토크나이저로 검사한다. definition은 FOR EACH/REFERENCING으로 시작해야 하고, table은 Import 집합의 테이블이어야 한다.
  - Why: MySQL 단일 문장 연결과 같은 보장이다. 변조된 manifest가 다른 스키마나 테이블에 객체를 만들 수 없다.
- 교체 규칙: 함수는 (이름, arguments) 단위로 CREATE OR REPLACE하고, DROP FUNCTION과 CASCADE는 쓰지 않는다.
- 같은 시그니처에서 반환형·인자 이름·종류가 다르면 DROP 전에 function_replace_incompatible로 거부한다.
- preflight와 preview가 덮어쓸 대상 함수 OID의 pg_depend를 읽는다. Import 집합 밖 테이블의 인덱스·attrdef·제약이 그 함수를 쓰면 function_has_external_dependents로 거부한다. 트리거·뷰만 쓰면 function_dependents_affected 경고를 낸다.
- 새로 만드는 함수와 이름이 같은 대상 오버로드가 있으면 function_overload_added 경고를 낸다.
- 교체 테이블의 대상 트리거는 DROP TABLE과 함께 사라지고 덤프 트리거로 다시 만든다.
- PG는 trigger_name_conflicts를 Import와 preview 모두에서 건너뛴다.
- 교체 전 대상 함수 정의를 journal의 previous_definition에 남긴다.
  - Why: '덤프 밖 객체는 건드리지 않는다'는 규칙 때문이다. OR REPLACE는 대상의 식 인덱스, 저장 계산 열, CHECK의 의미를 조용히 바꾸고 저장 값을 낡게 만든다. 이를 대상 변경 전에 막는다. 확인 결과 지금 preflight(import.rs:1149-1212)는 덤프 테이블에 의존하는 것만 본다.
- 원본과 대상 스키마 이름이 다르면 다음 두 경우를 기존 program_source_schema_reference로 DROP 전에 거부한다. (1) 달러 인용 본문이 references_source_schema 트립와이어에 걸린다. (2) 헤더와 proconfig(SET search_path 등)에 원본 스키마 이름이 따옴표 유무와 상관없이 들어 있다.
  - Why: deparse된 부분에서는 같은 스키마 한정자가 이미 빠져 있다. 남는 위험은 사람이 쓴 본문과 문자열 리터럴이다.
- 소유자와 권한: 새 함수의 소유자는 Import 계정이 된다. SECURITY DEFINER, LEAKPROOF, PARALLEL, COST, SET은 정의 그대로 둔다. 원본 소유자가 current_user와 다르면 program_owner_replaced를 보고한다. public_execute=false인 함수를 새로 만들 때는 같은 트랜잭션에서 REVOKE EXECUTE … FROM PUBLIC을 하고 function_grants_reset을 보고한다. OR REPLACE로 교체한 함수는 대상 GRANT를 유지한다.
  - Why: 출하된 MySQL 결정과 같다. 기본 PUBLIC EXECUTE 때문에 SECURITY DEFINER 함수의 실행 권한이 조용히 넓어지는 것만 막는다.
- probe 실패를 SQLSTATE로 나눈다.
- 42501: pg_program_privilege_missing
- 42P13·42809: function_replace_incompatible
- 42883·42704: program_dependency_missing
- 55P03(lock_timeout): target_busy
- 재확인 불일치: program_definition_changed / generated_expression_changed / program_name_resolution_changed
- 그 밖: program_probe_failed
대상 server_version_num이 180000 미만인데 덤프에 VIRTUAL 계산 열이 있으면 DDL 전에 generated_virtual_unsupported_on_target로 거부한다(PR2). 필요한 권한은 정책 문서와 preview에 적는다.
  - Why: '남는 거부마다 코드와 문장' 원칙 때문이다. VIRTUAL은 지금은 일반 ddl_preflight_failed로만 나온다(첫 번째 비평).
- cross-engine 판정은 헬퍼 same_engine(manifest, endpoint) = manifest.source_engine == endpoint.engine 하나로 한다(PR3). import.rs의 건너뜀 판정(441), 트리거 선택(526), 루틴 probe(538), EVENT 권한(550)에 쓴다. import_preview.rs의 skipped_cross_engine(105), MySQL 권한 블록(125), trigger_name_conflicts(203)에도 쓴다. 계산 열은 엔진이 다르면 import_table_ddls에서 DROP 전에 거부하고, validate_import_metadata는 preview와 같은 unsupported_generated_column 코드를 낸다. PG triggers_dropped(import_preview.rs:198)는 대소문자를 구별해 이름을 비교한다(PR4).
  - Why: 확인 결과 지금 조건은 대상 엔진만 본다. import.rs:81이 풀리면 PG 덤프를 MySQL로 가져올 때 PG 트리거·함수가 MySQL 생성과 probe 경로로 들어간다. 출하된 '건너뛰고 보고'(live_mysql_triggers.rs:362-378)를 양방향으로 지키려면 모든 분기가 같은 조건을 써야 한다.
- PR1의 사유 코드 경로: inspect_postgresql_unsupported_objects가 id마다 원인 코드를 InspectionResult.object_reasons에 넣는다. dump.rs는 PG 거부에서 이를 refusal.reasons에 합친다. 코드 이름은 schema.rs의 `pub(crate) const POSTGRES_REFUSAL_REASONS`에 모으고, Python 테스트가 정규식으로 읽어 문장·번역·RUST_CORE_CODES가 있는지 검사한다. 이후 PR이 추가하는 inspect 사유 코드도 이 const에 넣는다.
  - Why: map에 넣는 코드는 기존 소스 스캐너가 찾지 못한다.
- verify-in-copy:
- PR2에서 safe_restore의 held 차감 조건을 source_engine == 대상 엔진으로 넓힌다(TF-204 rebase 뒤 위치). 그래서 계산 열만 있는 PG 덤프는 safe까지 지원한다.
- PR3에서 copy_refusals에 명시적 거부를 넣는다: manifest.source_engine == "postgresql" && !stored_programs.is_empty()이면 dump_programs_unsupported_in_copy. 문장은 엔진 중립이다.
  - Why: 확인 결과 TF-204 작업 트리(git diff HEAD safe_restore.rs)는 copy_refusals에서 엔진과 무관한 프로그램 거부를 지운다. program_copy_refusals는 MySQL 연결 전용이다. TF-204 머지 뒤 PR3이 import.rs:81을 풀면 PG 프로그램 덤프를 막는 것이 없어 후보 schema에 함수가 생긴다. 그러면 정리 단계의 DROP SCHEMA CASCADE(safe_promote_postgres.rs:814)가 라이브 트리거를 지울 수 있다.
- PG 트리거 손실 보고(PR4): journal.trigger_tables를 PG에서도 채운다. 계획 테이블의 대상 트리거(pg_trigger NOT tgisinternal)와 덤프 트리거의 테이블을 넣는다.
  - Why: 확인 결과 import.rs:529-537은 MySQL 연결일 때만 채운다. 그래서 PG에서 트리거 생성이 실패해도 tables_lost_triggers가 늘 비어 있다.

## PR3: PG→PG 함수·프로시저 보존과 replace Import (format 7 재사용)

Depends on: PR2, TF-204(feat/safe-restore-programs) 머지 후 rebase

### Scope

포맷:
- NormalizedStoredProgram에 arguments(oidvectortypes), tables, public_execute를 추가하고, DumpManifest에 required_extensions를 추가한다(skip 조건 포함).

새 모듈 pg_programs.rs:
- collect_postgres_programs(endpoint, schema, scope): pg_proc, 확장 소유 제외. 증명 (a)-(f)와 사유 코드를 적용하고 위상 정렬한다.
- 범위: 전체 export는 전부 담는다. 일부 export는 선택 테이블 계산 열이 원본 스키마 함수를 쓰면 전부 담는다. 범위 밖 함수가 증명에 실패하면 manifest 경고만 남긴다.
- tables는 pg_depend로 채운다.
- validate_pg_program, pg_program_create_sql(헤더 조립), split_function_body(달러 인용 본문 분리)도 둔다.

캡처와 inventory:
- dump.rs의 mysql_programs 호출부(1253, 1461)를 엔진별로 나눈다. PG도 데이터 덤프 뒤 다시 캡처해 비교한다.
- held_object_ids: PG 항목은 validate_pg_program을 통과해야 하고, routine:n은 같은 이름이 전부 담겼을 때만 held다.
- inspect routine inventory를 pg_proc(확장 제외, DISTINCT proname)로 바꾼다. 계산 열의 원본 스키마 함수 의존을 허용한다.

Import(import.rs):
- import.rs:81의 MySQL 전용 거부를 source_engine별 검증기로 바꾼다. 키는 (kind,name,arguments)이고, 스키마 이름이 다르면 program_source_schema_reference 검사를 한다.
- same_engine 헬퍼를 import.rs 441·526·538·550과 import_preview.rs 105·125·203에 쓴다.
- 일부 Import 함수 범위(decisions)를 따른다. programs_created_missing과 programs_skipped_partial을 보고한다.
- preflight: 덮어쓸 대상 함수의 덤프 밖 의존 객체를 검사한다(function_has_external_dependents / function_dependents_affected).
- PG probe를 한 트랜잭션으로 바꾼다:
  - BEGIN, SET LOCAL check_function_bodies=off, SET LOCAL lock_timeout
  - 함수를 위상 순서로 CREATE OR REPLACE하고 재확인한다(본문 정확, 앞부분 canonical)
  - probe 테이블과 이름 해석 확인
  - FK는 맨 끝
  - ROLLBACK, DROP 정리는 하지 않는다
  run_owned_ddl_probe가 같은 연결·트랜잭션을 쓰도록 실행 클로저를 하나로 합친다.
- probe 실패는 SQLSTATE별 코드다.
- 첫 DROP 전 함수 트랜잭션: previous_definition을 기록하고, CREATE OR REPLACE와 필요하면 REVOKE EXECUTE FROM PUBLIC을 한 뒤 COMMIT한다. journal에 programs_imported, programs_failed, program_warnings(program_owner_replaced, function_grants_reset, function_overload_added, extension_missing_on_target)를 쓴다.

import_preview.rs:
- PG 대상에서 to_regprocedure(이름+arguments)로 replace/create를 나눈다.
- pg_has_role(owner,'USAGE')가 거짓이면 blocker pg_program_privilege_missing을 낸다.
- 외부 의존 blocker·경고, overload 경고, extension 경고를 낸다.

safe_restore.rs: copy_refusals에 source_engine=="postgresql" && !stored_programs.is_empty() → dump_programs_unsupported_in_copy를 넣는다.

Python:
- display_program_name이 routine:n(arguments)을 보여 준다.
- 새 보고 줄을 추가한다.
- _PARTIAL_EXPORT_NOTE_POSTGRESQL: '일부 테이블만 내보낼 때는 테이블을 담고, 그 계산 열이 원본 스키마 함수를 쓰면 원본 스키마 함수를 모두 함께 담습니다. 뷰는 빠집니다.'

문장:
- routine_kind_unsupported: '집계 함수나 윈도 함수 {}는 아직 덤프에 담을 수 없습니다.'
- routine_sql_body_unsupported: 'BEGIN ATOMIC 같은 SQL 표준 본문으로 만든 함수 {}는 아직 덤프에 담을 수 없습니다.'
- routine_language_unsupported: 'C 언어나 내부 함수 {}는 덤프에 담을 수 없습니다. 대상 DB에 같은 확장을 설치해 쓰세요.'
- routine_definition_unproven: '함수 {}의 정의를 원본 그대로 읽었는지 확인하지 못해 담지 않았습니다.'
- routine_type_unsupported: '함수 {}가 덤프할 테이블의 행 형식이나 이 스키마의 사용자 정의 형식을 인자·반환 형식으로 써서 아직 덤프에 담을 수 없습니다.'
- function_replace_incompatible: '대상 DB에 이름과 인자가 같은 함수가 있지만 반환 형식·인자 이름·종류가 달라 덮어쓸 수 없습니다. 대상 함수를 정리한 뒤 다시 가져오세요. 대상 테이블은 바뀌지 않았습니다.'
- function_has_external_dependents: '덮어쓸 함수 {}를 가져오지 않는 테이블의 인덱스·계산 열·제약이 쓰고 있어 시작하지 않았습니다. 덮어쓰면 그 값이 원본과 달라질 수 있습니다. 대상 테이블은 바뀌지 않았습니다.'
- function_dependents_affected: '덮어쓴 함수를 가져오지 않은 트리거나 뷰도 쓰고 있어 그 동작이 바뀝니다: {}'
- function_overload_added: '대상 DB에 이름이 같은 다른 함수가 있어, 기존 호출이 새 함수를 부르게 될 수 있습니다: {}'
- programs_created_missing: '대상 DB에 없던 덤프 함수를 새로 만들었습니다: {}'
- extension_missing_on_target: '원본 DB에 있던 확장이 대상 DB에 없습니다. 함수 본문이 이 확장을 쓰면 실행할 때 오류가 납니다: {}'
- program_dependency_missing: '함수나 트리거가 쓰는 언어·형식·함수가 대상 DB에 없어 시작하지 않았습니다. 필요한 확장이나 객체를 먼저 만드세요. 대상 테이블은 바뀌지 않았습니다.'
- pg_program_privilege_missing: '함수나 트리거를 만들 권한이 없습니다. 대상 스키마의 CREATE 권한, 같은 이름 함수의 소유자 권한, 함수 언어의 USAGE 권한을 확인하세요. 대상 테이블은 바뀌지 않았습니다.'
- program_definition_changed: '대상 DB가 함수나 트리거 정의를 원본과 다르게 저장해 시작하지 않았습니다. 대상 테이블은 바뀌지 않았습니다.'
- target_busy: '대상 DB의 다른 작업이 테이블을 잠그고 있어 검증을 끝내지 못했습니다. 잠시 뒤 다시 시도하세요. 대상 테이블은 바뀌지 않았습니다.'
- program_owner_replaced: '함수 소유자가 가져온 계정으로 바뀌었습니다: {}'
- function_grants_reset: '원본에서 실행 권한을 좁혀 둔 함수는 대상에서 소유자만 실행할 수 있게 만들었습니다. 필요한 역할에 EXECUTE 권한을 다시 주세요: {}'

기존 문장 수정:
- program_source_schema_reference: '저장 프로그램 본문이나 설정이 원본 DB(스키마) 이름을 참조합니다. 같은 이름으로 가져오거나 정의에서 그 이름을 빼세요.'
- programs_skipped_cross_engine: '다른 DB 종류로 가져오므로 덤프의 트리거·함수·프로시저·이벤트를 만들지 않습니다.'
- dump_programs_unsupported_in_copy: '트리거·함수·이벤트가 담긴 PostgreSQL 덤프는 아직 복사본에서 검증할 수 없습니다.'

### Files

- migration_core/src/pg_programs.rs (신규)
- migration_core/src/lib.rs (mod)
- migration_core/src/adapters.rs (L285-318 NormalizedStoredProgram)
- migration_core/src/dump_format.rs (DumpManifest.required_extensions, held_object_ids)
- migration_core/src/schema.rs (L773-783 routine inventory, generated 의존 허용)
- migration_core/src/dump.rs (L1253-1290, L1444-1467)
- migration_core/src/import.rs (L72-97, L436-446, L515-560, L760-787, L855-946, L1149-1212 근처 함수 의존 검사, 테이블 루프 앞 함수 단계)
- migration_core/src/import_preview.rs (L99-160, L203-226)
- migration_core/src/safe_restore.rs (copy_refusals, TF-204 rebase 뒤)
- src/exporters/rust_dump_exporter.py (CORE_CODE_SENTENCES, display_program_name L762-767, import 보고 L1110-1157)
- src/ui/dialogs/db_export_dialog.py (L55)
- src/ui/dialogs/import_plan_dialog.py
- src/core/i18n/legacy_translate.py, src/core/error_report_codes.py
- migration_core/tests/live_postgres_objects.rs, live_export_contract.rs (L226-324), live_dump_cross_engine.rs, live_safe_restore.rs
- tests/test_rust_dump_exporter.py, tests/test_db_export_dialog.py (L1889-1904), tests/test_import_plan_dialog.py
- docs/export_import_policy.md, docs/current_status.md

### Tests

단위 테스트(pg_programs.rs):
- 접두사 불일치나 prosrc 누락이면 routine_definition_unproven.
- 오버로드 하나가 실패하면 routine:f는 held가 아니다.
- 헤더 조립은 대상 스키마를 quote한다.
- split_function_body: 본문의 아포스트로피·'--'·'- -1'을 그대로 두고, 앞부분만 canonical 비교한다.
- 'SET search_path TO ''src''' 헤더와 본문의 "src".t는 스키마 이름이 다를 때만 program_source_schema_reference.
- 위상 정렬: f(a int DEFAULT g())는 g 뒤에 온다.
- import_table_ddls: 키 중복은 export_invalid, source_engine=mysql인 manifest의 PG 모양 항목은 MySQL 검증기로 거부.
- MySQL 항목을 다시 직렬화해도 새 키가 없다(바이트 동일).
- same_engine: PG 덤프→MySQL 대상이면 selected_triggers와 probe_routines가 호출되지 않는다.

live_postgres_objects.rs:
- pg_functions_round_trip_replace: plpgsql, SQL, INOUT 프로시저, 오버로드 f(int)/f(text), 이름 붙은 인자와 VARIADIC 인자, DEFAULT g()를 쓰는 함수, SECURITY DEFINER + SET search_path, STRICT IMMUTABLE PARALLEL SAFE COST 5를 둔다. format 7이고, 다른 이름 대상 스키마에서:
  - 정의가 스키마 이름만 다르고 같다.
  - 대상 전용 g2()와 대상 f(bigint)가 남고 function_overload_added가 나온다.
  - 대상 f(int)는 OID가 같은 채 정의가 바뀐다.
  - prosecdef와 proconfig가 같다.
- pg_rowtype_function_refused_with_reason: RETURNS SETOF <덤프 테이블>, f(<덤프 테이블>), enum 인자 함수 → routine_type_unsupported(bypassable). allow_incomplete export를 처음 가져오기와 두 번째 가져오기 모두 성공한다.
- pg_function_external_dependents_refuse_before_change: 대상 전용 테이블의 식 인덱스가 대상 f(int)를 쓰면 function_has_external_dependents이고 대상 함수·테이블이 그대로다. 대상 전용 트리거만 쓰면 성공하고 function_dependents_affected가 나온다.
- pg_generated_column_using_dump_function_round_trips(PR2 거부 뒤집기, 일부 export 포함).
- pg_extension_functions_are_not_inventoried(pgcrypto), 그리고 대상에 확장이 없으면 extension_missing_on_target 경고.
- pg_function_replace_incompatible_refuses_before_change.
- pg_function_owner_mismatch_refuses_before_change(preview와 import).
- pg_missing_language_refuses_before_change.
- pg_tampered_function_cannot_chain_statements.
- pg_function_body_naming_source_schema_refused.
- pg_restricted_function_execute_is_not_widened.
- pg_sql_body_function_refused_with_reason(PG14+), pg_aggregate_refused_with_reason.
- pg_function_failure_before_drop_leaves_target_unchanged.
- pg_probe_rollback_leaves_nothing: probe 실패를 주입해도 ddl_probe_cleanup_failed가 나지 않고 tf_probe_* 객체가 없다.
- pg_probe_respects_lock_timeout: 다른 세션이 FK 부모를 잠그면 target_busy이고 대상 그대로다.
- pg_partial_import_creates_missing_functions: 일부 Import에서 대상에 없는 덤프 함수만 생기고 기존 대상 함수는 그대로이며 programs_created_missing이 나온다.

live_dump_cross_engine.rs: pg_programs_dump_into_mysql_skips_and_reports. 테이블은 들어가고, programs_skipped_cross_engine이 나오며, MySQL에 트리거·루틴이 생기지 않는다.

live_safe_restore.rs: pg_program_dump_refused_in_verify_in_copy를 TF-204 이후 코드에서 돌린다. copy_refused이고 dump_programs_unsupported_in_copy다.

뒤집는 기존 테스트: live_export_contract.rs PG 전체 export 목록에서 routine:{fn}이 빠진다.

Python: 새 코드 문장, display_program_name, 일부 export 안내와 영어 번역, 보고 줄.

## PR4: PG→PG 트리거(트리거 함수 포함) 보존과 replace Import

Depends on: PR3

### Scope

캡처(pg_programs.rs):
- pg_trigger(NOT tgisinternal) + pg_get_triggerdef(oid,true)를 읽는다. 조각 분해 증명, status 매핑, 사유 코드(trigger_kind_unsupported, view_trigger_unsupported, trigger_definition_unproven, trigger_function_not_held)는 decisions대로다.
- 트리거의 pg_depend 함수 집합(tgfoid + WHEN)으로 held를 판정하고 tables를 채운다.
- 일부 export는 선택 테이블 트리거가 원본 스키마 함수를 쓰면 원본 스키마 함수를 전부 담는다.
- inspect trigger inventory를 pg_trigger로 바꾼다. id는 같고 view 트리거도 포함한다.
- scoped_omitted_objects: trigger의 table이 schema 테이블이 아니면 full_export일 때 범위에 넣는다.

Import:
- probe 트랜잭션에서 probe 테이블마다 트리거를 만들고 재확인한다. 트리거의 pg_depend로 이름 해석도 확인한다.
- 테이블 루프(import.rs:633-660)의 PG 분기: 테이블마다 BEGIN → execute_single로 CREATE TRIGGER → status에 따라 ALTER TABLE … DISABLE/ENABLE REPLICA/ENABLE ALWAYS TRIGGER → COMMIT. COMMIT 뒤에 triggers_created와 checkpoint를 쓴다.
- 실패하면 기존 trigger_create_failed다.
- journal.trigger_tables를 PG에서도 채운다.
- PG에서는 trigger_name_conflicts를 건너뛴다. skip_triggers를 PG에도 허용한다.

import_preview.rs: PG도 programs 목록을 채운다. triggers_dropped는 PG에서 대소문자를 구별해 비교하고, trigger_name_conflicts는 PG에서 건너뛴다.

Python:
- db_import_dialog의 '트리거 없이 테이블만 가져오기'를 PG 대상과 TRIGGER 항목이 있는 PG 덤프에서도 켤 수 있게 한다.
- _INCOMPLETE_EXPORT_TOOLTIP_POSTGRESQL: '구체화 뷰나 집계 함수처럼 덤프에 담을 수 없는 객체와 정의를 확인하지 못한 트리거·함수를 묻지 않고 빼고 내보냅니다. 결과는 완전한 백업이 아닙니다. 계산 열·CHECK 제약처럼 빼면 테이블 정의가 달라지는 것은 빼고 내보낼 수 없습니다.'
- _PARTIAL_EXPORT_NOTE_POSTGRESQL: '일부 테이블만 내보낼 때는 테이블과 그 트리거를 담고, 트리거나 계산 열이 원본 스키마 함수를 쓰면 원본 스키마 함수를 모두 함께 담습니다. 뷰는 빠집니다.'

문장:
- trigger_kind_unsupported: '트리거 {}는 제약 트리거(CONSTRAINT TRIGGER)이거나 파티션에서 물려받은 트리거라 아직 덤프에 담을 수 없습니다.'
- view_trigger_unsupported: '뷰에 걸린 INSTEAD OF 트리거 {}는 아직 덤프에 담을 수 없습니다.'
- trigger_definition_unproven: '트리거 {}의 정의를 원본 그대로 읽었는지 확인하지 못해 담지 않았습니다.'
- trigger_function_not_held: '트리거 {}가 부르는 함수를 덤프에 담지 못해 트리거도 담지 않았습니다.'

### Files

- migration_core/src/pg_programs.rs
- migration_core/src/schema.rs (L751-759)
- migration_core/src/dump.rs (L1053-1066, L1253-1290, L1444-1467)
- migration_core/src/dump_format.rs (held_object_ids trigger)
- migration_core/src/import.rs (L72-97, L526-537 trigger_tables, L760-787, L633-660)
- migration_core/src/import_preview.rs (L99-110, L197-205)
- src/ui/dialogs/db_import_dialog.py (L1001-1023)
- src/ui/dialogs/db_export_dialog.py (L52, L55)
- src/exporters/rust_dump_exporter.py, src/core/i18n/legacy_translate.py, src/core/error_report_codes.py
- migration_core/tests/live_postgres_objects.rs, live_export_contract.rs (L226-324), live_import_policy.rs
- tests/test_db_import_dialog.py (L143-161), tests/test_db_export_dialog.py (L298-358, L1889-1904), tests/test_rust_dump_exporter.py, tests/test_import_plan_dialog.py
- docs/export_import_policy.md, docs/current_status.md

### Tests

단위 테스트:
- 조각 분해: UPDATE OF "on", b와 WHEN 분해, 큰따옴표 테이블 이름, 다르게 조립되면 trigger_definition_unproven.
- 이벤트 토크나이저: 'INSERT; DROP'과 'INSERT ON other' 거부.
- table이 Import 집합 밖이면 export_invalid.
- status 매핑.
- PG에서는 trigger_name_conflicts를 호출하지 않는다(import·preview).

live_postgres_objects.rs:
- pg_triggers_round_trip_replace: 트리거 일곱 가지(BEFORE INSERT row, REFERENCING statement, UPDATE OF+WHEN, TRUNCATE, DELETE 가드, suppress_redundant_updates_trigger, DISABLED와 ALWAYS). 정의와 tgenabled가 같고, 가드가 동작하고, 두 번째 Import도 성공하며, 대상 전용 테이블 트리거가 남는다.
- pg_trigger_helper_function_carried: 트리거 함수가 본문에서 helper()를 부르고 WHEN이 is_hot()을 쓴다. 테이블 하나만 export해도 helper와 is_hot이 담긴다. helper가 없는 대상에 일부 Import하면 helper가 생기고 INSERT가 성공한다.
- pg_partial_export_carries_trigger_functions.
- pg_trigger_function_in_other_schema: 대상에 public.touch()가 없으면 program_dependency_missing, 있으면 성공.
- pg_view_trigger_refused_with_reason, pg_constraint_trigger_refused_with_reason.
- pg_tampered_trigger_header_refused, pg_tampered_trigger_cannot_chain_statements.
- pg_read_only_account_sees_triggers.
- pg_skip_triggers_toggle.
- pg_trigger_status_is_atomic: 상태 ALTER 실패를 주입하면 그 테이블에 트리거가 켜진 채 남지 않는다.
- pg_trigger_create_failure_reports_lost_triggers: trigger_create_failed를 주입하면 tables_lost_triggers에 그 테이블이 나온다.

live_import_policy.rs: PG preview의 triggers_dropped에 덤프가 다시 만드는 트리거가 나오지 않고, 대소문자만 다른 대상 트리거는 dropped로 나온다.

뒤집는 기존 테스트: live_export_contract.rs:226-324. 전체 export 거부와 allow_incomplete 경고에는 구체화 뷰만 남는다.

Python: PG 토글 활성, PG 툴팁·일부 안내 정확 문자열과 영어 번역.

## PR5: (뒤로 미룸) PG verify-in-copy와 예약 리허설이 함수·트리거 덤프를 받는다

Depends on: PR4

### Scope

TF-204 MySQL PR6-PR7이 머지된 뒤 따로 설계한다. 방향은 다음과 같다.
- 후보 schema에는 함수와 트리거를 만들지 않고 probe만 한다.
- cutover 트랜잭션(safe_promote_postgres.rs:376-501) 안에서 SET SCHEMA → 원본 schema에 CREATE OR REPLACE FUNCTION → 옮겨진 테이블에 덤프 트리거 CREATE 순서로 한다.
- plan_inner(L209-226)와 target_copy_refusals의 함수·트리거 일괄 거부를 좁힌다.
- objects/dependencies/structure_digest/inspect_backup/external_references가 pg_proc, pg_trigger, trigger→function 의존을 담게 한다. 단, 이미 기록된 PG 백업과 displaced namespace의 digest가 바뀌면 안 된다.
  - 새 키는 값이 있을 때만 붙인다. 예: `|| CASE WHEN a.attgenerated<>'' THEN jsonb_build_object('generated',a.attgenerated::text) ELSE '{}' END`. jsonb_strip_nulls는 기존 null 키까지 지우므로 쓰지 않는다.
  - 또는 digest에 버전을 두고 옛 기록은 옛 알고리즘으로 검증한다.
  - 새 객체가 없는 relation의 digest를 고정하는 테스트를 먼저 넣는다.
- 정리(DROP SCHEMA CASCADE)가 다른 schema의 트리거를 지우지 않음을 증명한다.
- plan_inner의 코드 없는 영어 거부에도 Refusal 코드를 준다.
- safe_restore의 routine:<이름> 필터와 확장 함수 target_routines 거부를 고친다.
그 전까지는 PR3의 copy_refusals 명시적 거부가 복사 전에 멈춘다.

### Files

- migration_core/src/safe_restore.rs
- migration_core/src/safe_promote_postgres.rs (L64-82 objects, digest 기록 L775·L882·L1030·L1088)
- migration_core/src/safe_promotion.rs
- migration_core/src/safe_backups.rs
- migration_core/tests/live_safe_restore.rs, live_backup_lifecycle.rs

### Tests

먼저 전제 검증 spike를 한다: SET SCHEMA 뒤 트리거가 테이블을 따라가는지, tgfoid가 유지되는지, 함수 schema를 DROP SCHEMA CASCADE하면 다른 schema의 트리거도 지워지는지를 PG 13과 18에서 live로 확인한다.

digest 고정 테스트:
- 고정 PG inventory(새 객체 없음)의 structure_digest가 PR5 전후로 같다.
- v2.20.0이 기록한 백업을 PR5 코드가 inspect·guided recovery로 검증할 수 있다.

이어서 다음을 테스트한다: pg_safe_restore_carries_dump_functions_and_triggers, 실패 주입 뒤 원본 복구, guided recovery 역방향.

## Risks

- pg_get_triggerdef(oid, true)의 pretty 모드가 search_path 위의 테이블을 한정하지 않는다는 점은 확인하지 않았다. 다르면 모든 트리거가 trigger_definition_unproven으로 거부된다(fail-closed). PR4 첫 live 테스트에서 확인한다.
- PG 13 원본을 PG 18 대상으로 옮기는 식으로 버전이 다르면 서버가 만든 앞부분과 식의 deparse가 달라질 수 있다. 그러면 probe 재확인이 잘못 거부할 수 있다. 본문은 정확 비교라 버전 영향이 없지만, 앞부분과 식은 PG 13 CI가 없으면 검증되지 않는다(owner question 2).
- check_function_bodies=off로 만들기 때문에 SQL 함수 본문의 테이블 참조 오류는 Import 때 잡히지 않고 실행할 때 드러난다(pg_dump와 같은 동작).
- plpgsql 본문 안의 이름은 실행할 때 호출자의 search_path로 해석된다. probe의 pg_depend 이름 해석 확인은 계산 열, WHEN, 시그니처, 트리거 함수만 지킨다. 본문 호출을 대상 스키마의 다른 함수가 가로채는 것은 막지 못한다(원본에서도 같은 성질이다).
- 함수 트랜잭션은 첫 DROP 전에 COMMIT된다. 이후 테이블 단계가 실패하면 함수는 이미 덤프 정의로 바뀐 상태다. journal의 previous_definition으로 되돌릴 수 있지만 자동 복구는 아니다.
- 덤프의 함수·트리거 본문은 Import 계정 권한으로 실행되고, SECURITY DEFINER 함수의 소유자도 Import 계정이 된다. manifest에는 서명이 없어 신뢰하지 않는 덤프의 위험이 view보다 크다. extended protocol과 PG 식 검사기는 문장 연결과 헤더 변조만 막는다.
- probe 트랜잭션은 lock_timeout 안에서 FK 부모 테이블에 SHARE ROW EXCLUSIVE lock을 잡는다. 그동안 애플리케이션의 그 테이블 쓰기가 잠깐 막힌다. 정책 문서에 적는다.
- 일부 Import는 대상에 없는 덤프 함수를 새로 만든다(owner question 8). 선택과 상관없어 보이는 함수가 대상에 생길 수 있지만, 기존 대상 객체는 바뀌지 않는다.
- required_extensions는 경고뿐이다. 함수 본문이 그 확장을 실제로 쓰는지는 증명할 수 없어서, 대상에 확장이 없어도 Import는 진행된다.
- routine_type_unsupported 때문에 RETURNS SETOF <테이블>을 쓰는 앱은 '빼고 내보내기' 없이는 전체 PG export가 계속 거부된다(owner question 7).
- inventory를 pg_catalog로 바꾸면, 지금 읽기 전용 계정으로 '성공'하던 PG export가 PR3에서 PR4 사이에 트리거 거부(bypassable)로 바뀔 수 있다.
- PG COPY가 계산 열이 든 열 목록을 거부하고 빼면 다시 계산한다는 것은 PG 문서 기준이다. PR2의 전제 증명 테스트가 처음 확인한다.
- v2.20.0 앱에 PG 프로그램 덤프(format 7)를 넣으면 'stored programs require a MySQL source'(export_invalid)로 거부되고, '덤프 파일이 온전하지 않거나…' 문장이 나온다. 안전하지만 '새 버전에서 만든 덤프'라는 안내는 아니다.
- 병렬 브랜치 TF-204(feat/safe-restore-programs)가 safe_restore.rs, import.rs, import_preview.rs, mysql_programs.rs를 바꾸는 중이다. 특히 copy_refusals의 프로그램 거부를 지운다. PR2와 PR3은 TF-204가 머지된 뒤 rebase해야 한다. PR3의 명시적 PG 거부가 빠지면 PG 프로그램 덤프가 후보 schema로 들어간다.
- PG structure_digest(safe_promote_postgres.rs:64-82)는 attgenerated를 담지 않는다. 그래서 같은 식을 쓰는 DEFAULT 열과 GENERATED 열을 구별하지 못한다. PR2는 digest를 바꾸지 않는다(옛 기록 호환). 이 틈은 PR5의 조건부 키로 메운다.
- RLS policy, rule, COMMENT, 상속·파티션, 열 collation은 오늘도 PG export에서 경고 없이 빠진다. 특히 RLS는 데이터 노출 위험이 있어 별도 이슈가 필요하다.
- 대상 DB의 event trigger(pg_event_trigger)가 Import의 CREATE FUNCTION/TRIGGER나 probe DDL에 반응해 실행될 수 있다. 이를 다루는 코드가 없다.
- map에 넣는 사유 코드는 기존 Python 소스 스캐너가 찾지 못한다. POSTGRES_REFUSAL_REASONS const에 넣는 것을 빼먹으면 문장 없는 코드가 사용자에게 그대로 보인다.
- refuted: 첫 번째 비평의 'PR2 digest change' 부분. PR2 설계는 promotion inventory와 structure_digest를 건드리지 않으므로 PR2에서는 옛 백업 digest가 깨지지 않는다. digest 호환 조건은 PR5 scope에만 반영했다.
- refuted(해당 없음): 두 번째 비평의 행 형식 함수 시나리오 (a), 즉 '덤프에 t가 있는데 probe가 program_dependency_missing으로 잘못 보고'. 행 형식 함수를 export에서 routine_type_unsupported로 빼므로 이 경로는 생기지 않는다. 비평의 핵심인 순서 결함은 반영했다.

## Out of scope

- PG verify-in-copy(safe)와 예약 리허설이 함수·트리거가 든 덤프를 받는 것(PR5, TF-204의 PG판): PG 교체는 테이블만 ALTER TABLE SET SCHEMA로 옮긴다. 후보 schema에 만든 함수는 OID로 묶인 채 남고, 정리 단계의 DROP SCHEMA CASCADE가 라이브 테이블의 트리거까지 지운다(safe_promote_postgres.rs:814). 증명 장치(objects/dependencies/structure_digest)도 pg_class만 본다. 그래서 cutover를 다시 설계해야 한다. 그 전까지는 PR3이 copy_refusals에 다시 넣는 명시적 거부로 복사 전에 멈춘다. 계산 열만 있는 PG 덤프는 PR2에서 safe까지 지원한다.
- cross-engine 번역(MySQL 생성식↔PG 생성식, 트리거·함수 본문 변환): 식이나 의미가 조용히 바뀌는 것은 소유자 원칙에 어긋난다. 계산 열은 거부하고 프로그램은 건너뛴 뒤 보고한다.
- PG CHECK 제약, GENERATED ALWAYS identity, 비기본 시퀀스, 식 DEFAULT, enum/domain/확장 형식, 식·부분·비btree 인덱스, cross-schema FK, 구체화 뷰 보존: 이번에는 PR1에서 사유 코드와 문장만 단다. CHECK와 ALWAYS identity는 PR2의 PG 식 검사기와 probe 재확인 방식으로 싸게 담을 수 있어 후속 이슈로 권장한다(owner question).
- 덤프 테이블의 행 형식이나 원본 스키마의 형식(enum·domain·composite, 그 배열)을 인자·반환 형식으로 쓰는 함수: PG는 함수를 만들 때 시그니처 형식을 묶는다. 테이블보다 먼저 만들면 옛 테이블에 묶여 DROP TABLE(CASCADE 없음)이 중간에 실패한다. 대상에 이미 있으면 preflight가 dependent_object로 거부한다(import.rs:1182-1210). 대상의 같은 시그니처 함수를 DROP하는 순서를 새로 설계해야 해서, 이번에는 routine_type_unsupported(생략 가능)로 뺀다(owner question).
- SQL 표준 본문 함수(BEGIN ATOMIC/RETURN, PG14+), 집계·윈도 함수, C/internal 언어 함수, CONSTRAINT TRIGGER, 파티션 복제 트리거(tgparentid), view의 INSTEAD OF 트리거: 생성 순서·의존성이나 바이너리 의존 때문에 증명 가능한 재생성 경로가 없다. 사유 코드가 붙은 생략 가능(waivable) 거부로 남긴다.
- 원본 스키마 밖(public 등)의 함수, 확장 설치: 덤프 대상 스키마만 소유한다. 이런 함수를 정의에서 직접 참조하면 Import probe가 대상 변경 전에 program_dependency_missing으로 잡는다. 본문 안에서만 부르는 경우는 잡지 못하므로, manifest의 required_extensions와 extension_missing_on_target 경고로 알린다.
- 함수 ACL(GRANT) 전체 복사, 소유자 재현: 대상에 같은 역할이 있다는 보장이 없다. PUBLIC EXECUTE가 넓어지는 것만 막는다(public_execute). 나머지는 보고 줄로 처리한다.
- PG RLS/policy, rule, event trigger, 테이블·열 COMMENT, 열 collation, 상속·파티션, unlogged: TF-205 범위가 아니다. 오늘도 PG export에서 경고 없이 빠진다(schema.rs:734-841에 감지 쿼리가 없다). 특히 RLS는 보안 손실이므로 새 TF-STATUS 이슈로 등록할 것을 권장한다.
- safe_restore.rs의 routine:<이름>을 테이블 이름으로 비교하는 필터, 확장 함수가 PG target_routines 거부를 일으키는 문제, PG structure_digest 확장: PR5와 함께 고칠 safe 경로 문제다. digest를 바꿀 때 지켜야 할 호환 조건은 PR5 scope에 적었다.
- replace Import에서 다시 계산된 계산 열 값을 덤프 값과 비교하는 것: 출하된 MySQL 계산 열과 같이 replace는 대상 계산 결과를 그대로 쓴다. 값 비교는 verify-in-copy가 한다. replace는 generated_values_recomputed 보고 줄만 낸다.
