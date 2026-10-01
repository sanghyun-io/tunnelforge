"""
복원 리허설 (예약 백업 옵션)
- 방금 만든 백업을 사용자가 지정한 비운영 대상에 '안전 복원'(후보 네임스페이스)으로 복원한다.
- 행 수/내용 digest 검증은 Rust Core 안전 복원이 수행하고, 여기서는 매니페스트의 행 수와 한 번 더 대조한다.
- 검증이 끝나면 소유가 증명된 후보만 restore.backups cleanup(#269/#270)으로 정리한다.
- 대상의 기존 네임스페이스는 절대 변경하지 않으며(없으면 만들지도 않고 실패), 운영 프로필은 대상이 될 수 없다.
- 결과는 작업 목록(job_history)과 백업 폴더의 리허설 보고서에 남긴다 (비밀정보 없음).
"""
import json
import os
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.core.connection_trust import apply_registered_tls
from src.core.db_core_facade import DbCoreFacade, DbEndpoint
from src.core.job_history import (
    KIND_RESTORE_REHEARSAL, STATUS_COMPLETED, STATUS_FAILED, job_begin, job_finish,
)
from src.core.logger import get_logger
from src.core.production_guard import Environment, ProductionGuard
from src.core.schedule_config import ScheduleConfig
from src.core import scheduled_backup_store as backup_store

logger = get_logger(__name__)

REPORT_NAME = '_tunnelforge_rehearsal_report.json'
# 환경이 명시적으로 비운영(development/staging)인 프로필만 리허설 대상이 될 수 있다 (미설정도 거부: fail closed).
ALLOWED_ENVIRONMENTS = (Environment.DEVELOPMENT, Environment.STAGING)


def rehearsal_target_error(tunnel_config: Optional[dict], schedule: ScheduleConfig) -> Optional[str]:
    """리허설 대상 프로필/네임스페이스 검증. 사용자에게 보여줄 사유 또는 None."""
    if not schedule.rehearsal_tunnel_id:
        return None
    if not schedule.rehearsal_schema:
        return "복원 리허설 대상 스키마(데이터베이스)를 지정하세요."
    if not tunnel_config:
        return "복원 리허설 대상 터널 설정을 찾을 수 없습니다."
    if ProductionGuard.is_production(tunnel_config):
        return "운영(Production) 프로필은 복원 리허설 대상으로 선택할 수 없습니다."
    if ProductionGuard.get_environment(tunnel_config) not in ALLOWED_ENVIRONMENTS:
        return ("복원 리허설 대상은 환경이 개발/스테이징으로 설정된 프로필이어야 합니다. "
                "프로필의 환경을 먼저 설정하세요.")
    return None


def _namespace(engine: str, database: str, schema: str) -> Tuple[str, str]:
    return (database or "postgres", schema) if engine == "postgresql" else ("", schema)


class RehearsalOutcome:
    def __init__(self, ok: bool, message: str, report_path: str = "", candidate_retained: bool = False):
        self.ok, self.message, self.report_path, self.candidate_retained = ok, message, report_path, candidate_retained


@dataclass
class _Facts:
    """보고서에 쓰이는 리허설 사실 모음."""
    restore_id: str = ""
    candidate: str = ""
    verified: bool = False
    original_unchanged: bool = False
    status: str = ""
    tables: Optional[List[Dict[str, Any]]] = None
    cleanup: str = "not_attempted"
    cleanup_message: str = ""
    blockers: Optional[List[str]] = None


class RestoreRehearsal:
    def __init__(
        self,
        resolve_connection: Callable[[ScheduleConfig], Tuple[Any, str]],
        find_tunnel_config: Callable[[str], Optional[dict]],
        connector_factory: Callable[..., Any],
        facade_factory: Callable[[], DbCoreFacade] = DbCoreFacade,
    ):
        self.resolve_connection = resolve_connection
        self.find_tunnel_config = find_tunnel_config
        self.connector_factory = connector_factory
        self.facade_factory = facade_factory

    # ------------------------------------------------------------------ public
    def run(self, schedule: ScheduleConfig, backup_dir: str, source) -> RehearsalOutcome:
        """방금 만든 backup_dir 로 리허설을 수행한다. source 는 백업에 쓴 해석된 연결(엔진/호스트/포트)."""
        label = self._label(schedule)
        job_id = job_begin(KIND_RESTORE_REHEARSAL, profile_id=schedule.rehearsal_tunnel_id,
                           profile_name=schedule.name, target=label, mode="복원 리허설 (후보 복원 → 검증 → 정리)")
        facts = _Facts()
        resolved = None
        started = datetime.now()
        try:
            error = rehearsal_target_error(self.find_tunnel_config(schedule.rehearsal_tunnel_id), schedule)
            if error:
                return self._finish(job_id, schedule, backup_dir, started, facts, error)
            resolved, error = self.resolve_connection(replace(schedule, tunnel_id=schedule.rehearsal_tunnel_id))
            if error:
                return self._finish(job_id, schedule, backup_dir, started, facts, error)
            error = self._preflight(schedule, resolved, source)
            if error:
                return self._finish(job_id, schedule, backup_dir, started, facts, error)
            error = self._restore_verify_cleanup(schedule, backup_dir, resolved, facts)
            return self._finish(job_id, schedule, backup_dir, started, facts, error)
        except Exception as exc:  # 어떤 예외도 작업 목록에 실패로 남긴다
            logger.exception("복원 리허설 오류")
            return self._finish(job_id, schedule, backup_dir, started, facts, f"복원 리허설 오류: {exc}")
        finally:
            release = getattr(resolved, 'release', None)
            if callable(release):
                try:
                    release()
                except Exception:
                    logger.warning("리허설 연결 정리 실패", exc_info=True)

    # ----------------------------------------------------------------- helpers
    @staticmethod
    def _label(schedule: ScheduleConfig) -> str:
        if schedule.rehearsal_database:
            return f"{schedule.rehearsal_database}.{schedule.rehearsal_schema}"
        return schedule.rehearsal_schema

    def _preflight(self, schedule: ScheduleConfig, target, source) -> str:
        if target.engine != source.engine:
            return "복원 리허설은 백업과 같은 DB 엔진의 대상에서만 지원합니다."
        same_server = (target.host, int(target.port)) == (source.host, int(source.port))
        source_ns = _namespace(source.engine, schedule.database, schedule.schema)
        target_ns = _namespace(target.engine, schedule.rehearsal_database, schedule.rehearsal_schema)
        if same_server and source_ns == target_ns:
            return "복원 리허설 대상이 백업 원본과 같은 네임스페이스입니다. 다른 스키마를 지정하세요."
        # 대상 네임스페이스는 이미 있어야 한다: 안전 복원이 후보를 따로 만들고 기존 네임스페이스는 건드리지 않는다.
        # (없는 이름을 지정하면 그 이름 자체가 새로 만들어져 정리 대상이 되지 못하므로 거부한다.)
        connector = self.connector_factory(
            target.engine, target.host, int(target.port), target.user, target.password,
            database=(schedule.rehearsal_database or "postgres") if target.engine == "postgresql" else "information_schema")
        try:
            ok, message = connector.connect()
            if not ok:
                return f"복원 리허설 대상에 연결할 수 없습니다: {message}"
            with connector.connection.cursor() as cursor:
                cursor.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
                               (schedule.rehearsal_schema,))
                exists = cursor.fetchone() is not None
            if not exists:
                return (f"복원 리허설 대상 '{self._label(schedule)}'이(가) 없습니다. 비어 있는 네임스페이스를 먼저 "
                        "만들어 두세요 (기존 데이터는 변경되지 않으며 리허설은 별도 후보에만 복원합니다).")
        finally:
            connector.disconnect()
        return ""

    def _endpoint_payload(self, schedule: ScheduleConfig, target) -> Dict[str, Any]:
        schema = schedule.rehearsal_schema
        return DbEndpoint(
            engine=target.engine, host=target.host, port=int(target.port), user=target.user,
            password=target.password,
            database=(schedule.rehearsal_database or "postgres") if target.engine == "postgresql" else schema,
            schema=schema if target.engine == "postgresql" else "",
        ).to_payload()

    def _restore_verify_cleanup(self, schedule: ScheduleConfig, backup_dir: str, target, facts: _Facts) -> str:
        from src.exporters.rust_dump_exporter import RustDumpConfig, RustDumpImporter

        facade = self.facade_factory()
        state: Dict[str, Any] = {}

        def capture(line: str) -> None:
            try:
                value = json.loads(line)
            except (TypeError, ValueError):
                return
            if isinstance(value, dict) and value.get("event") == "safe_restore_ready":
                state.update(value)

        try:
            config = RustDumpConfig(
                host=target.host, port=int(target.port), user=target.user, password=target.password,
                schema=schedule.rehearsal_schema, engine=target.engine,
                database=schedule.rehearsal_database if target.engine == "postgresql" else "")
            importer = RustDumpImporter(config, facade=facade)
            ok, message, _ = importer.import_dump(
                backup_dir, target_schema=schedule.rehearsal_schema, threads=4, import_mode="safe",
                raw_output_callback=capture)
            error = "" if ok else f"후보 복원 실패: {message}"
            facts.restore_id = str(state.get("restore_id") or "")
            facts.status = str(state.get("status") or "")
            facts.verified = state.get("verified") is True
            facts.original_unchanged = state.get("original_unchanged") is True
            facts.blockers = [str(b) for b in state.get("blockers") or []]
            candidate = state.get("candidate_target") or {}
            facts.candidate = str(candidate.get("schema") if target.engine == "postgresql" else candidate.get("database") or "")
            if not error:
                error = self._verify(backup_dir, state, facts)
            # 성공/실패와 무관하게 이 실행이 만든 소유 후보는 정리를 시도한다 (실패하면 보고서에 남기고 보존).
            cleanup_error = self._cleanup_candidates(facade, schedule, backup_dir, target, facts)
            return error or cleanup_error
        finally:
            try:
                facade.client.shutdown()
            except Exception:
                pass

    @staticmethod
    def _verify(backup_dir: str, state: Dict[str, Any], facts: _Facts) -> str:
        if not (facts.verified and facts.original_unchanged):
            return "후보 복원이 검증되지 않았거나 대상 원본의 불변이 확인되지 않았습니다."
        try:
            with open(os.path.join(backup_dir, "_tunnelforge_dump.json"), encoding="utf-8") as handle:
                manifest = json.load(handle)
            with open(str(state.get("report_path") or ""), encoding="utf-8") as handle:
                report = json.load(handle)
        except (OSError, ValueError) as exc:
            return f"검증 자료를 읽을 수 없습니다: {exc}"
        verification = report.get("verification") or {}
        actual = verification.get("actual_row_counts") or {}
        digests = verification.get("content_digests") or {}
        rows: List[Dict[str, Any]] = []
        mismatched: List[str] = []
        for table in manifest.get("tables", []):
            name = str(table.get("name") or "")
            expected = int(table.get("rows") or 0)
            restored = actual.get(name)
            restored = restored if isinstance(restored, int) else None
            matches = restored == expected and name in digests
            if not matches:
                mismatched.append(name)
            rows.append({"table": name, "expected_rows": expected, "restored_rows": restored,
                         "digest_verified": name in digests})
        facts.tables = rows
        if not rows:
            return "백업 매니페스트에 검증할 테이블이 없습니다."
        if mismatched:
            return "행 수 또는 내용 digest가 일치하지 않는 테이블: " + ", ".join(mismatched)
        return ""

    def _cleanup_candidates(self, facade, schedule: ScheduleConfig, backup_dir: str, target, facts: _Facts) -> str:
        endpoint = apply_registered_tls(self._endpoint_payload(schedule, target))
        base = {"endpoint": endpoint, "input_dirs": [backup_dir]}
        try:
            listing = facade.restore_backups({"action": "list", **base})
        except Exception as exc:
            facts.cleanup, facts.cleanup_message = "list_failed", str(exc)
            return f"리허설 후보 정리 실패(조회): {exc}"
        leftovers = [e for e in listing.get("backups") or []
                     if (e.get("candidate") or {}).get("exists")
                     and (not facts.restore_id or e.get("restore_id") == facts.restore_id)]
        if not leftovers:
            facts.cleanup = "nothing_to_clean"
            return ""
        for entry in leftovers:
            restore_id = entry.get("restore_id")
            try:
                plan = facade.restore_backups({"action": "cleanup_plan", "restore_id": restore_id,
                                               "target": "candidate", **base})
                if not plan.get("can_cleanup"):
                    facts.cleanup = "blocked"
                    facts.cleanup_message = "; ".join(str(b) for b in plan.get("blockers") or [])
                    return f"리허설 후보를 정리할 수 없어 보존했습니다: {facts.cleanup_message}"
                applied = facade.restore_backups({"action": "cleanup_apply", "restore_id": restore_id,
                                                  "target": "candidate", "plan_digest": plan.get("plan_digest"),
                                                  "confirmed": True, **base})
                facts.cleanup, facts.cleanup_message = "cleaned", str(applied.get("message") or "")
            except Exception as exc:
                facts.cleanup, facts.cleanup_message = "failed", str(exc)
                return f"리허설 후보 정리 실패: {exc}"
        return ""

    def _finish(self, job_id, schedule: ScheduleConfig, backup_dir: str, started: datetime,
                facts: _Facts, error: str) -> RehearsalOutcome:
        retained = facts.cleanup in ("blocked", "failed", "list_failed")
        ok = not error
        report_path = self._write_report(schedule, backup_dir, started, facts, error)
        if retained:
            # 정리되지 않은 후보의 저널은 이 백업 폴더에 있으므로 보존 정책이 폴더를 지우지 않게 한다.
            backup_store.set_hold(backup_dir, "rehearsal_candidate_retained")
        message = ("복원 리허설 완료: 후보 복원 → 행/digest 검증 → 후보 정리"
                   if ok else f"복원 리허설 실패: {error}")
        if ok:
            job_finish(job_id, STATUS_COMPLETED, report_path=report_path,
                       details={"candidate_cleanup": facts.cleanup, "tables": len(facts.tables or [])})
        else:
            job_finish(job_id, STATUS_FAILED, error=error, report_path=report_path,
                       details={"candidate_cleanup": facts.cleanup})
        return RehearsalOutcome(ok, message, report_path, retained)

    def _write_report(self, schedule: ScheduleConfig, backup_dir: str, started: datetime,
                      facts: _Facts, error: str) -> str:
        path = os.path.join(backup_dir, REPORT_NAME)
        body = {
            "kind": "restore_rehearsal",
            "result": "failed" if error else "passed",
            "error": error,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "schedule": schedule.name,
            "backup_dir": os.path.basename(os.path.normpath(backup_dir)),
            "rehearsal_target": {"tunnel_id": schedule.rehearsal_tunnel_id,
                                 "namespace": self._label(schedule)},
            "restore_id": facts.restore_id,
            "restore_status": facts.status,
            "candidate_namespace": facts.candidate,
            "verified": facts.verified,
            "target_original_unchanged": facts.original_unchanged,
            "blockers": facts.blockers or [],
            "tables": facts.tables or [],
            "candidate_cleanup": facts.cleanup,
            "candidate_cleanup_message": facts.cleanup_message[:300],
        }
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(body, handle, ensure_ascii=False, indent=1)
        except OSError:
            logger.warning("리허설 보고서 쓰기 실패", exc_info=True)
            return ""
        return path
