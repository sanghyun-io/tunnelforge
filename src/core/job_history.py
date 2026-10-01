"""작업 목록 기록 저장소 (TF-STATUS-132, Qt 비의존).

오래 걸리는 Export / Import / 안전 전환 / 크로스엔진 이관의 실행마다 한 건을 영구 기록한다.
저장하는 것: 프로필 id/이름, 대상 이름(DB/스키마), 작업 종류, 실제 실행 모드, 시작/종료 시각, 결과 상태,
오류 요약, 보고서/로그 파일 경로, 작은 요약 수치, (재실행 가능한 종류의) 비밀 없는 설정.
저장하지 않는 것: 비밀번호, 연결 문자열, 호스트/사용자, 데이터, SQL. 오류 문구는 비밀 패턴을 가린다.

파일은 원자적으로(임시 파일 -> os.replace) 쓰고, 최근 MAX_RECORDS(500)건만 유지한다.
실행 중(running)이던 기록이 다음 실행 때 남아 있으면 interrupted 로 바꾼다(앱 종료/크래시).
"""
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.core.logger import get_logger
from src.core.platform_paths import job_history_file

logger = get_logger('job_history')

HISTORY_VERSION = 1
MAX_RECORDS = 500
MAX_ERROR_CHARS = 400
MAX_FIELD_CHARS = 200
MAX_DETAILS = 12
MAX_RERUN_TABLES = 200
KEEP_CORRUPT_BACKUPS = 3

KIND_EXPORT_FULL = 'export_full'
KIND_EXPORT_TABLES = 'export_tables'
KIND_IMPORT = 'import'
KIND_PROMOTE = 'promote'
KIND_MIGRATION_PREFLIGHT = 'migration_preflight'
KIND_MIGRATION_RUN = 'migration_run'
KIND_MIGRATION_RESUME = 'migration_resume'
KINDS = (KIND_EXPORT_FULL, KIND_EXPORT_TABLES, KIND_IMPORT, KIND_PROMOTE,
         KIND_MIGRATION_PREFLIGHT, KIND_MIGRATION_RUN, KIND_MIGRATION_RESUME)

STATUS_RUNNING = 'running'
STATUS_COMPLETED = 'completed'
STATUS_PARTIAL = 'partial'
STATUS_FAILED = 'failed'
STATUS_CANCELLED = 'cancelled'
STATUS_INTERRUPTED = 'interrupted'  # 실행 중 앱이 종료되어 결과를 알 수 없음
STATUSES = (STATUS_RUNNING, STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED)
FINAL_STATUSES = (STATUS_COMPLETED, STATUS_PARTIAL, STATUS_FAILED, STATUS_CANCELLED, STATUS_INTERRUPTED)

# 재실행 설정에 허용하는 키 (비밀/연결 정보는 구조적으로 들어올 수 없다)
_RERUN_KEYS = ('schema', 'scope', 'tables', 'compression', 'threads', 'snapshot_mode', 'include_fk_parents')
_SECRET_KEY_RE = re.compile(r'pass|secret|token|credential|pwd|key|user|host|uri|dsn', re.IGNORECASE)

_WRITE_LOCK = threading.RLock()

_URI_RE = re.compile(r'(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@]*@')
_SECRET_ASSIGN_RE = re.compile(r'(?i)\b(password|passwd|pwd|secret|token|api[_-]?key|passphrase)\b["\']?\s*[=:]\s*("[^"]*"|\'[^\']*\'|[^\s,}]+)')
_DASH_P_RE = re.compile(r'(?<!\S)-p\S+')


def scrub_text(value: Any, limit: int = MAX_ERROR_CHARS) -> str:
    """오류/이름 문구에서 비밀 패턴(URI 자격 증명, password=..., -p...)을 가리고 길이를 제한한다."""
    text = '' if value is None else str(value)
    text = _URI_RE.sub(r'\1***@', text)
    text = _SECRET_ASSIGN_RE.sub(lambda m: f'{m.group(1)}=***', text)
    text = _DASH_P_RE.sub('-p***', text)
    text = ' '.join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + '…'


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse_iso(value: Any) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class JobRecord:
    id: str
    kind: str
    status: str = STATUS_RUNNING
    profile_id: str = ''
    profile_name: str = ''
    target: str = ''
    mode: str = ''
    started_at: str = ''
    finished_at: str = ''
    error_summary: str = ''
    report_path: str = ''
    log_path: str = ''
    details: Dict[str, Any] = field(default_factory=dict)
    rerun: Optional[Dict[str, Any]] = None

    def started(self) -> Optional[datetime]:
        return _parse_iso(self.started_at)

    def finished(self) -> Optional[datetime]:
        return _parse_iso(self.finished_at)

    def duration_seconds(self) -> Optional[int]:
        start, end = self.started(), self.finished()
        return int((end - start).total_seconds()) if start and end else None


def _clean_details(details: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    cleaned: Dict[str, Any] = {}
    for key, value in (details or {}).items():
        if len(cleaned) >= MAX_DETAILS:
            break
        key = scrub_text(key, 40)
        if not key or _SECRET_KEY_RE.search(key):
            continue
        if isinstance(value, bool) or isinstance(value, int):
            cleaned[key] = value
        elif isinstance(value, str):
            cleaned[key] = scrub_text(value, MAX_FIELD_CHARS)
    return cleaned


def _clean_rerun(rerun: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    if not isinstance(rerun, dict):
        return None
    cleaned: Dict[str, Any] = {}
    for key in _RERUN_KEYS:
        if key not in rerun:
            continue
        value = rerun[key]
        if key == 'tables' and isinstance(value, (list, tuple)):
            cleaned[key] = [scrub_text(t, MAX_FIELD_CHARS) for t in list(value)[:MAX_RERUN_TABLES] if isinstance(t, str)]
        elif isinstance(value, bool) or isinstance(value, int):
            cleaned[key] = value
        elif isinstance(value, str):
            cleaned[key] = scrub_text(value, MAX_FIELD_CHARS)
    return cleaned or None


def _to_dict(record: JobRecord) -> Dict[str, Any]:
    return {
        'id': record.id, 'kind': record.kind, 'status': record.status,
        'profile_id': record.profile_id, 'profile_name': record.profile_name,
        'target': record.target, 'mode': record.mode,
        'started_at': record.started_at, 'finished_at': record.finished_at,
        'error_summary': record.error_summary, 'report_path': record.report_path, 'log_path': record.log_path,
        'details': record.details, 'rerun': record.rerun,
    }


def _from_dict(raw: Any) -> Optional[JobRecord]:
    if not isinstance(raw, dict):
        return None
    job_id, kind, status = raw.get('id'), raw.get('kind'), raw.get('status')
    if not isinstance(job_id, str) or not job_id or kind not in KINDS or status not in STATUSES:
        return None

    def text(key: str) -> str:
        value = raw.get(key)
        return value if isinstance(value, str) else ''

    details = raw.get('details') if isinstance(raw.get('details'), dict) else {}
    return JobRecord(
        id=job_id, kind=kind, status=status,
        profile_id=text('profile_id'), profile_name=text('profile_name'), target=text('target'), mode=text('mode'),
        started_at=text('started_at'), finished_at=text('finished_at'),
        error_summary=text('error_summary'), report_path=text('report_path'), log_path=text('log_path'),
        details=_clean_details(details), rerun=_clean_rerun(raw.get('rerun')),
    )


class JobHistory:
    """JSON 파일 하나에 최근 작업 기록을 보관한다. 모든 변경은 읽기-수정-원자적 쓰기."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path is not None else job_history_file()

    # -- 파일 입출력 ------------------------------------------------------
    def _read(self) -> List[JobRecord]:
        try:
            text = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return []
        except OSError:
            return []
        try:
            data = json.loads(text)
            if not isinstance(data, dict) or not isinstance(data.get('records'), list):
                raise ValueError('malformed')
        except ValueError:
            self._quarantine()
            return []
        return [r for r in (_from_dict(item) for item in data['records']) if r is not None]

    def _quarantine(self) -> None:
        stamp = _now().strftime('%Y%m%d%H%M%S%f')
        backup = self.path.with_name(f'{self.path.name}.corrupt-{stamp}')
        try:
            os.replace(self.path, backup)
        except OSError:
            return
        logger.warning('job history file was unreadable and was set aside')
        for old in sorted(self.path.parent.glob(f'{self.path.name}.corrupt-*'))[:-KEEP_CORRUPT_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass

    def _write(self, records: List[JobRecord]) -> None:
        records = self._trim(records)
        payload = json.dumps({'version': HISTORY_VERSION, 'records': [_to_dict(r) for r in records]},
                             ensure_ascii=False, indent=1)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f'{self.path.name}.tmp.{os.getpid()}.{threading.get_ident()}')
        try:
            with open(tmp, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _trim(records: List[JobRecord]) -> List[JobRecord]:
        """최근 MAX_RECORDS 건만 남긴다. 실행 중 기록은 버리지 않는다."""
        if len(records) <= MAX_RECORDS:
            return records
        excess = len(records) - MAX_RECORDS
        kept: List[JobRecord] = []
        for record in records:  # 오래된 것부터 저장되어 있다
            if excess > 0 and record.status != STATUS_RUNNING:
                excess -= 1
                continue
            kept.append(record)
        return kept

    # -- API --------------------------------------------------------------
    def begin(self, kind: str, *, profile_id: str = '', profile_name: str = '', target: str = '',
              mode: str = '', details: Optional[Dict[str, Any]] = None, report_path: str = '',
              rerun: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> str:
        if kind not in KINDS:
            raise ValueError(f'unknown job kind: {kind}')
        record = JobRecord(
            id=uuid.uuid4().hex[:16], kind=kind, status=STATUS_RUNNING,
            profile_id=scrub_text(profile_id, 96), profile_name=scrub_text(profile_name, MAX_FIELD_CHARS),
            target=scrub_text(target, MAX_FIELD_CHARS), mode=scrub_text(mode, MAX_FIELD_CHARS),
            started_at=_iso(now or _now()), details=_clean_details(details), rerun=_clean_rerun(rerun),
            report_path=scrub_text(report_path, 500),
        )
        with _WRITE_LOCK:
            records = self._read()
            records.append(record)
            self._write(records)
        return record.id

    def finish(self, job_id: str, status: str, *, error: str = '', report_path: str = '', log_path: str = '',
               details: Optional[Dict[str, Any]] = None, now: Optional[datetime] = None) -> bool:
        if status not in FINAL_STATUSES:
            raise ValueError(f'not a final status: {status}')
        with _WRITE_LOCK:
            records = self._read()
            for record in records:
                if record.id == job_id:
                    record.status = status
                    record.finished_at = _iso(now or _now())
                    record.error_summary = scrub_text(error)
                    if report_path:
                        record.report_path = scrub_text(report_path, 500)
                    if log_path:
                        record.log_path = scrub_text(log_path, 500)
                    if details:
                        record.details = _clean_details({**record.details, **details})
                    self._write(records)
                    return True
        return False

    def list(self) -> List[JobRecord]:
        """최신순."""
        with _WRITE_LOCK:
            return list(reversed(self._read()))

    def delete(self, job_ids: Iterable[str]) -> int:
        """기록 삭제. 실행 중인 기록은 삭제하지 않는다. 지운 개수를 돌려준다."""
        wanted = set(job_ids)
        with _WRITE_LOCK:
            records = self._read()
            kept = [r for r in records if not (r.id in wanted and r.status != STATUS_RUNNING)]
            removed = len(records) - len(kept)
            if removed:
                self._write(kept)
            return removed

    def clear(self) -> int:
        with _WRITE_LOCK:
            records = self._read()
            kept = [r for r in records if r.status == STATUS_RUNNING]
            removed = len(records) - len(kept)
            if removed:
                self._write(kept)
            return removed

    def mark_interrupted(self, alive_ids: Iterable[str] = (), now: Optional[datetime] = None) -> int:
        """앱 시작 시 호출: 이전 실행에서 running 으로 남은 기록을 interrupted 로 바꾼다."""
        alive = set(alive_ids)
        changed = 0
        with _WRITE_LOCK:
            records = self._read()
            for record in records:
                if record.status == STATUS_RUNNING and record.id not in alive:
                    record.status = STATUS_INTERRUPTED
                    record.finished_at = _iso(now or _now())
                    record.error_summary = '앱이 종료되어 결과를 알 수 없습니다 (작업 도중 종료/비정상 종료).'
                    changed += 1
            if changed:
                self._write(records)
        return changed


# ---------------------------------------------------------------------------
# UI 가 쓰는 안전한 진입점: 기록 실패가 작업을 방해하면 안 된다.
# ---------------------------------------------------------------------------
_ACTIVE_IDS: set = set()


def make_history() -> JobHistory:
    """테스트가 임시 디렉토리로 바꿀 수 있도록 모듈 수준에 둔다."""
    return JobHistory()


def job_begin(kind: str, **fields) -> Optional[str]:
    try:
        job_id = make_history().begin(kind, **fields)
        _ACTIVE_IDS.add(job_id)
        return job_id
    except Exception:
        logger.warning('job history begin failed', exc_info=True)
        return None


def job_finish(job_id: Optional[str], status: str, **fields) -> None:
    if not job_id:
        return
    try:
        make_history().finish(job_id, status, **fields)
    except Exception:
        logger.warning('job history finish failed', exc_info=True)
    finally:
        _ACTIVE_IDS.discard(job_id)


def sweep_interrupted() -> int:
    """앱 시작 시 한 번: 이 프로세스가 시작하지 않은 running 기록을 interrupted 로 표시."""
    try:
        return make_history().mark_interrupted(_ACTIVE_IDS)
    except Exception:
        logger.warning('job history sweep failed', exc_info=True)
        return 0
