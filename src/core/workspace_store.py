"""SQL 작업 공간 복구 저장소 (TF P1-4, Qt 비의존).

프로필별 JSON 파일 하나에 열린 탭(순서/제목/파일 경로/미저장 SQL 초안/커서)과 대상
DB/스키마 이름을 원자적으로 저장하고 읽는다. 설계: docs/superpowers/specs/2026-10-01-workspace-recovery-design.md

저장하지 않는 것: 비밀번호, 자격 증명, 연결 파라미터, 조회 결과, 셀 편집, 미커밋 문장,
트랜잭션/자동커밋/쓰기 해제 상태. 직렬화는 아래 dataclass 의 화이트리스트 필드만 거친다.
"""
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from src.core.logger import get_logger
from src.core.platform_paths import workspaces_dir

logger = get_logger('workspace_store')

WORKSPACE_VERSION = 1
MAX_TABS = 50
MAX_TAB_TEXT_BYTES = 2 * 1024 * 1024
MAX_FILE_BYTES = 16 * 1024 * 1024
KEEP_CORRUPT_BACKUPS = 3
ORPHAN_RETENTION_DAYS = 90

SETTING_ENABLED = 'workspace_recovery_enabled'
SETTING_AUTOSAVE_SECONDS = 'workspace_recovery_autosave_seconds'
DEFAULT_AUTOSAVE_SECONDS = 30
MIN_AUTOSAVE_SECONDS = 5
MAX_AUTOSAVE_SECONDS = 300

SESSION_OPEN = 'open'
SESSION_CLOSED = 'closed'

STATUS_OK = 'ok'
STATUS_MISSING = 'missing'
STATUS_CORRUPT = 'corrupt'
STATUS_NEWER_VERSION = 'newer_version'

_PROFILE_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,96}$')
_WRITE_LOCK = threading.Lock()


class WorkspaceStoreError(Exception):
    """저장소 오류 (경로 검증 실패, 쓰기 실패 등)."""


def autosave_seconds(value: Any) -> int:
    """설정값을 허용 범위(5~300초)로 보정한다. 잘못된 값은 기본값."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return DEFAULT_AUTOSAVE_SECONDS
    return max(MIN_AUTOSAVE_SECONDS, min(MAX_AUTOSAVE_SECONDS, seconds))


def _utc_now() -> datetime:
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


def validate_profile_id(profile_id: Any) -> str:
    if not isinstance(profile_id, str) or not _PROFILE_ID_RE.match(profile_id):
        raise WorkspaceStoreError('invalid profile id')
    return profile_id


# ---------------------------------------------------------------------------
# 데이터 모델 (화이트리스트)
# ---------------------------------------------------------------------------
@dataclass
class CursorState:
    position: int = 0
    anchor: int = 0
    first_visible_line: int = 0


@dataclass
class FileState:
    mtime_ns: int = 0
    size: int = 0
    sha256: str = ''


@dataclass
class TabState:
    id: str
    title_index: int = 1
    file_path: Optional[str] = None
    text: Optional[str] = None
    dirty: bool = False
    cursor: CursorState = field(default_factory=CursorState)
    file_state: Optional[FileState] = None
    target: Optional[Dict[str, str]] = None  # 탭별 대상 (예약; 현재는 항상 None)
    text_omitted: bool = False  # 크기 제한으로 초안을 저장하지 못함 (조용한 절단 금지)


@dataclass
class WorkspaceState:
    profile_id: str
    target_database: str = ''
    target_schema: str = ''
    active_tab: int = 0
    tabs: List[TabState] = field(default_factory=list)
    session_state: str = SESSION_OPEN
    app_version: str = ''
    saved_at: str = ''


@dataclass
class SaveResult:
    path: Path
    omitted_tabs: List[str]  # 크기/개수 제한으로 초안이 저장되지 않은 탭 id
    wrote_sibling: bool = False


@dataclass
class LoadResult:
    status: str
    state: Optional[WorkspaceState] = None
    message: str = ''
    backup_path: Optional[Path] = None
    crashed: bool = False  # 이전 세션이 정상 종료되지 않음 (session.state == "open")


@dataclass
class WorkspaceSummary:
    profile_id: str
    path: Path
    saved_at: Optional[datetime]
    tab_count: int
    orphan: bool
    expired: bool


# ---------------------------------------------------------------------------
# 직렬화 / 검증
# ---------------------------------------------------------------------------
def _text_bytes(text: str) -> int:
    return len(text.encode('utf-8', errors='replace'))


def _tab_to_dict(tab: TabState) -> Dict[str, Any]:
    return {
        'id': tab.id,
        'title_index': tab.title_index,
        'file_path': tab.file_path,
        'text': tab.text,
        'dirty': bool(tab.dirty),
        'text_omitted': bool(tab.text_omitted),
        'cursor': {
            'position': tab.cursor.position,
            'anchor': tab.cursor.anchor,
            'first_visible_line': tab.cursor.first_visible_line,
        },
        'file_state': None if tab.file_state is None else {
            'mtime_ns': tab.file_state.mtime_ns,
            'size': tab.file_state.size,
            'sha256': tab.file_state.sha256,
        },
        'target': tab.target,
    }


def state_to_dict(state: WorkspaceState, saved_at: str) -> Dict[str, Any]:
    return {
        'version': WORKSPACE_VERSION,
        'profile_id': state.profile_id,
        'saved_at': saved_at,
        'session': {'state': state.session_state, 'app_version': state.app_version},
        'target': {'database': state.target_database, 'schema': state.target_schema},
        'active_tab': state.active_tab,
        'tabs': [_tab_to_dict(tab) for tab in state.tabs],
    }


def _int(value: Any, default: int = 0, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return max(minimum, value)


def _parse_tab(raw: Any) -> Optional[TabState]:
    if not isinstance(raw, dict):
        return None
    tab_id = raw.get('id')
    if not isinstance(tab_id, str) or not tab_id or len(tab_id) > 64:
        return None
    text = raw.get('text')
    if text is not None and not isinstance(text, str):
        return None
    file_path = raw.get('file_path')
    if file_path is not None and (not isinstance(file_path, str) or not os.path.isabs(file_path)):
        file_path = None
    cursor_raw = raw.get('cursor') if isinstance(raw.get('cursor'), dict) else {}
    limit = len(text) if text is not None else None
    position = _int(cursor_raw.get('position'))
    anchor = _int(cursor_raw.get('anchor'))
    if limit is not None:
        position, anchor = min(position, limit), min(anchor, limit)
    file_state = None
    fs_raw = raw.get('file_state')
    if isinstance(fs_raw, dict) and isinstance(fs_raw.get('sha256'), str):
        file_state = FileState(_int(fs_raw.get('mtime_ns')), _int(fs_raw.get('size')), fs_raw['sha256'])
    target = raw.get('target')
    if not (isinstance(target, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in target.items())):
        target = None
    return TabState(
        id=tab_id,
        title_index=_int(raw.get('title_index'), 1, 1) or 1,
        file_path=file_path,
        text=text,
        dirty=raw.get('dirty') is True,
        cursor=CursorState(position, anchor, _int(cursor_raw.get('first_visible_line'))),
        file_state=file_state,
        target=target,
        text_omitted=raw.get('text_omitted') is True,
    )


def parse_workspace(data: Any, profile_id: str) -> WorkspaceState:
    """검증된 상태를 만든다. 형식 오류(최상위)는 ValueError, 탭 단위 오류는 해당 탭만 건너뛴다."""
    if not isinstance(data, dict) or not isinstance(data.get('tabs'), list):
        raise ValueError('workspace root is malformed')
    if data.get('profile_id') != profile_id:
        raise ValueError('workspace belongs to a different profile')
    session = data.get('session') if isinstance(data.get('session'), dict) else {}
    target = data.get('target') if isinstance(data.get('target'), dict) else {}
    tabs: List[TabState] = []
    seen = set()
    for raw in data['tabs'][:MAX_TABS]:
        tab = _parse_tab(raw)
        if tab is not None and tab.id not in seen:
            seen.add(tab.id)
            tabs.append(tab)
    active = min(_int(data.get('active_tab')), max(0, len(tabs) - 1))
    return WorkspaceState(
        profile_id=profile_id,
        target_database=target.get('database') if isinstance(target.get('database'), str) else '',
        target_schema=target.get('schema') if isinstance(target.get('schema'), str) else '',
        active_tab=active,
        tabs=tabs,
        session_state=SESSION_OPEN if session.get('state') == SESSION_OPEN else SESSION_CLOSED,
        app_version=session.get('app_version') if isinstance(session.get('app_version'), str) else '',
        saved_at=data.get('saved_at') if isinstance(data.get('saved_at'), str) else '',
    )


# ---------------------------------------------------------------------------
# 파일 상태 비교 (파일 기반 탭 복원용)
# ---------------------------------------------------------------------------
def compute_file_state(path: str) -> Optional[FileState]:
    try:
        stat = os.stat(path)
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return FileState(stat.st_mtime_ns, stat.st_size, digest.hexdigest())
    except OSError:
        return None


def compare_file_state(path: str, saved: Optional[FileState]) -> str:
    """'unchanged' | 'changed' | 'missing' | 'unknown'(저장된 상태 없음)."""
    if saved is None:
        return 'unknown'
    current = compute_file_state(path)
    if current is None:
        return 'missing'
    return 'unchanged' if current.sha256 == saved.sha256 and current.size == saved.size else 'changed'


# ---------------------------------------------------------------------------
# 저장소
# ---------------------------------------------------------------------------
class WorkspaceStore:
    def __init__(self, directory: Optional[Path] = None):
        self.directory = Path(directory) if directory is not None else workspaces_dir()

    # -- 경로 -----------------------------------------------------------
    def path_for(self, profile_id: str) -> Path:
        return self.directory / f'{validate_profile_id(profile_id)}.json'

    def _sibling_path(self, profile_id: str) -> Path:
        return self.directory / f'{validate_profile_id(profile_id)}.compat-v{WORKSPACE_VERSION}.json'

    def _ensure_directory(self) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)

    # -- 쓰기 -----------------------------------------------------------
    def _primary_is_newer(self, profile_id: str) -> bool:
        data = self._read_json(self.path_for(profile_id))
        return isinstance(data, dict) and _int(data.get('version')) > WORKSPACE_VERSION

    def _apply_limits(self, state: WorkspaceState) -> List[str]:
        """제한을 넘는 초안은 text 를 비우고 text_omitted 로 표시한다 (절단 금지). 생략된 탭 id 반환."""
        omitted: List[str] = []
        if len(state.tabs) > MAX_TABS:
            omitted.extend(tab.id for tab in state.tabs[MAX_TABS:])
            state.tabs = state.tabs[:MAX_TABS]
        for tab in state.tabs:
            if tab.text is not None and _text_bytes(tab.text) > MAX_TAB_TEXT_BYTES:
                tab.text, tab.text_omitted = None, True
                omitted.append(tab.id)
        return omitted

    def save(self, state: WorkspaceState, now: Optional[datetime] = None) -> SaveResult:
        profile_id = validate_profile_id(state.profile_id)
        saved_at = _iso(now or _utc_now())
        with _WRITE_LOCK:
            self._ensure_directory()
            # 입력 스냅샷을 바꾸지 않는다.
            working = WorkspaceState(**{**state.__dict__, 'tabs': [TabState(**{**t.__dict__}) for t in state.tabs]})
            omitted = self._apply_limits(working)
            payload = json.dumps(state_to_dict(working, saved_at), ensure_ascii=False, indent=1)
            # 파일 전체 제한: 큰 초안부터 생략한다.
            while len(payload.encode('utf-8')) > MAX_FILE_BYTES:
                candidates = [t for t in working.tabs if t.text]
                if not candidates:
                    raise WorkspaceStoreError('workspace metadata exceeds the file size limit')
                biggest = max(candidates, key=lambda t: _text_bytes(t.text or ''))
                biggest.text, biggest.text_omitted = None, True
                omitted.append(biggest.id)
                payload = json.dumps(state_to_dict(working, saved_at), ensure_ascii=False, indent=1)
            sibling = self._primary_is_newer(profile_id)
            target = self._sibling_path(profile_id) if sibling else self.path_for(profile_id)
            self._atomic_write(target, payload)
        return SaveResult(target, sorted(set(omitted)), sibling)

    def _atomic_write(self, target: Path, payload: str) -> None:
        tmp = target.with_name(f'{target.name}.tmp.{os.getpid()}.{threading.get_ident()}')
        try:
            with open(tmp, 'w', encoding='utf-8') as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if os.name != 'nt':
                os.chmod(tmp, 0o600)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise WorkspaceStoreError(f'cannot write workspace file: {exc.__class__.__name__}') from exc

    # -- 읽기 -----------------------------------------------------------
    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            with open(path, 'r', encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError):
            return None

    def load(self, profile_id: str, now: Optional[datetime] = None) -> LoadResult:
        validate_profile_id(profile_id)
        primary = self.path_for(profile_id)
        sibling = self._sibling_path(profile_id)
        if not primary.exists() and not sibling.exists():
            return LoadResult(STATUS_MISSING)
        try:
            data = json.loads(primary.read_text(encoding='utf-8')) if primary.exists() else None
        except (OSError, ValueError):
            return self._quarantine(primary, 'workspace file is not valid JSON', now)
        if isinstance(data, dict) and _int(data.get('version')) > WORKSPACE_VERSION:
            # 더 새 버전이 쓴 파일: 건드리지 않는다. 호환 사본이 있으면 그것을 읽는다.
            if sibling.exists():
                return self._load_file(sibling, profile_id, now)
            return LoadResult(STATUS_NEWER_VERSION, message='workspace was written by a newer TunnelForge')
        if primary.exists():
            return self._load_file(primary, profile_id, now, data)
        return self._load_file(sibling, profile_id, now)

    def _load_file(self, path: Path, profile_id: str, now: Optional[datetime], data: Any = None) -> LoadResult:
        if data is None:
            try:
                data = json.loads(path.read_text(encoding='utf-8'))
            except (OSError, ValueError):
                return self._quarantine(path, 'workspace file is not valid JSON', now)
        try:
            state = parse_workspace(data, profile_id)
        except ValueError as exc:
            return self._quarantine(path, str(exc), now)
        return LoadResult(STATUS_OK, state, crashed=state.session_state == SESSION_OPEN)

    def _quarantine(self, path: Path, reason: str, now: Optional[datetime]) -> LoadResult:
        stamp = (now or _utc_now()).astimezone(timezone.utc).strftime('%Y%m%d%H%M%S')
        backup = path.with_name(f'{path.stem}.corrupt-{stamp}')
        try:
            os.replace(path, backup)
        except OSError:
            backup = None
        self._prune_corrupt(path.stem)
        logger.warning('workspace file quarantined: %s', reason)
        return LoadResult(STATUS_CORRUPT, message=reason, backup_path=backup)

    def _prune_corrupt(self, stem: str) -> None:
        backups = sorted(self.directory.glob(f'{stem}.corrupt-*'))
        for old in backups[:-KEEP_CORRUPT_BACKUPS]:
            try:
                old.unlink()
            except OSError:
                pass

    # -- 삭제 / 정리 ----------------------------------------------------
    def delete(self, profile_id: str) -> None:
        """한 프로필의 작업 공간(호환 사본 포함)을 지운다. 손상 백업은 유지한다."""
        with _WRITE_LOCK:
            for path in (self.path_for(profile_id), self._sibling_path(profile_id)):
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise WorkspaceStoreError(f'cannot delete workspace file: {exc.__class__.__name__}') from exc

    def delete_all(self) -> int:
        """저장된 모든 작업 공간과 손상 백업, 임시 파일을 지운다 (설정의 '저장된 작업 공간 지우기')."""
        removed = 0
        with _WRITE_LOCK:
            if not self.directory.is_dir():
                return 0
            for path in self.directory.iterdir():
                if path.is_file() and (path.suffix == '.json' or '.corrupt-' in path.name or '.tmp.' in path.name):
                    try:
                        path.unlink()
                        removed += 1
                    except OSError:
                        logger.warning('workspace file could not be removed')
        return removed

    def cleanup_stale_tmp(self) -> int:
        """쓰기 도중 비정상 종료로 남은 임시 파일을 제거한다 (대상 파일은 이전/새 완전본 중 하나)."""
        removed = 0
        if self.directory.is_dir():
            for path in self.directory.glob('*.tmp.*'):
                try:
                    path.unlink()
                    removed += 1
                except OSError:
                    pass
        return removed

    # -- 목록 (고아 작업 공간) -------------------------------------------
    def list_workspaces(
        self, known_profile_ids: Iterable[str], now: Optional[datetime] = None
    ) -> List[WorkspaceSummary]:
        """저장된 작업 공간 목록. known_profile_ids 에 없는 프로필은 고아(orphan)이며,
        90일이 지난 고아는 expired 로 표시한다 (삭제는 사용자가 목록에서 확인한 뒤)."""
        known = set(known_profile_ids)
        current = now or _utc_now()
        summaries: List[WorkspaceSummary] = []
        if not self.directory.is_dir():
            return summaries
        for path in sorted(self.directory.glob('*.json')):
            profile_id = path.name[:-len('.json')]
            if profile_id.endswith(f'.compat-v{WORKSPACE_VERSION}') or not _PROFILE_ID_RE.match(profile_id):
                continue
            data = self._read_json(path)
            if not isinstance(data, dict) or not isinstance(data.get('tabs'), list):
                continue
            saved_at = _parse_iso(data.get('saved_at'))
            orphan = profile_id not in known
            expired = bool(orphan and saved_at and current - saved_at > timedelta(days=ORPHAN_RETENTION_DAYS))
            summaries.append(WorkspaceSummary(profile_id, path, saved_at, len(data['tabs']), orphan, expired))
        return summaries
