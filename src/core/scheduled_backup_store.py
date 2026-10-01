"""예약 백업 폴더의 소유 증명(마커 파일)과 보존 정책. Qt 비의존.

예약 백업이 만든 폴더에만 마커 파일(`.tunnelforge_scheduled_backup.json`)을 남긴다. 보존 정리는 마커로
소유가 증명된 폴더(스케줄 id와 폴더 이름이 일치)만 대상으로 하며, 마커가 없는 폴더(과거 버전의 백업,
사용자가 만든 폴더)는 절대 삭제하지 않는다. 진행 중(running) 백업과 가장 최근의 완료 백업은 보호한다.
"""
import json
import os
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from src.core.logger import get_logger

logger = get_logger('scheduled_backup_store')

MARKER_NAME = '.tunnelforge_scheduled_backup.json'
MARKER_VERSION = 1

STATE_RUNNING = 'running'
STATE_COMPLETED = 'completed'
STATE_FAILED = 'failed'
STATE_INTERRUPTED = 'interrupted'
STATES = (STATE_RUNNING, STATE_COMPLETED, STATE_FAILED, STATE_INTERRUPTED)

_LOCK = threading.Lock()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _parse(value) -> Optional[datetime]:
    if not isinstance(value, str):
        return None
    try:
        return datetime.strptime(value, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


@dataclass
class OwnedBackup:
    path: str
    schedule_id: str
    state: str
    created_at: datetime
    finished_at: Optional[datetime] = None


def marker_path(directory: str) -> str:
    return os.path.join(directory, MARKER_NAME)


def write_marker(directory: str, schedule_id: str, schedule_name: str, state: str,
                 created_at: Optional[datetime] = None, finished_at: Optional[datetime] = None,
                 message: str = '') -> None:
    """마커를 원자적으로 쓴다 (임시 파일 -> os.replace). 상태 갱신 때도 같은 함수를 쓴다."""
    if state not in STATES:
        raise ValueError(f'unknown backup state: {state}')
    path = marker_path(directory)
    previous = read_marker(directory)
    created = created_at or (previous['created_at'] if previous else _now())
    payload = {
        'version': MARKER_VERSION,
        'schedule_id': schedule_id,
        'schedule_name': schedule_name,
        'dir_name': os.path.basename(os.path.normpath(directory)),
        'state': state,
        'created_at': _iso(created),
        'finished_at': _iso(finished_at) if finished_at else '',
        'message': (message or '')[:300],
    }
    tmp = f'{path}.tmp.{os.getpid()}.{threading.get_ident()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def read_marker(directory: str) -> Optional[dict]:
    """검증된 마커 (created_at/finished_at 은 datetime). 없거나 손상되었으면 None."""
    try:
        with open(marker_path(directory), 'r', encoding='utf-8') as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get('version') != MARKER_VERSION:
        return None
    created = _parse(data.get('created_at'))
    if (created is None or data.get('state') not in STATES
            or not isinstance(data.get('schedule_id'), str) or not data['schedule_id']
            or data.get('dir_name') != os.path.basename(os.path.normpath(directory))):
        return None
    return {**data, 'created_at': created, 'finished_at': _parse(data.get('finished_at'))}


def list_owned(output_dir: str, schedule_id: str) -> List[OwnedBackup]:
    """output_dir 바로 아래 폴더 중 이 스케줄이 만든 것(마커 증명)만. 심볼릭 링크는 제외한다."""
    owned: List[OwnedBackup] = []
    try:
        names = os.listdir(output_dir)
    except OSError:
        return owned
    for name in names:
        path = os.path.join(output_dir, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        marker = read_marker(path)
        if marker and marker['schedule_id'] == schedule_id:
            owned.append(OwnedBackup(path, schedule_id, marker['state'], marker['created_at'], marker['finished_at']))
    return sorted(owned, key=lambda b: b.created_at)


def sweep_interrupted(output_dir: str, schedule_id: str, schedule_active: bool) -> int:
    """앱이 백업 도중 종료되어 running 으로 남은 마커를 interrupted 로 바꾼다 (실행 중인 스케줄은 제외)."""
    if schedule_active:
        return 0
    changed = 0
    for backup in list_owned(output_dir, schedule_id):
        if backup.state == STATE_RUNNING:
            marker = read_marker(backup.path)
            try:
                write_marker(backup.path, schedule_id, marker.get('schedule_name', ''), STATE_INTERRUPTED,
                             finished_at=_now(), message='앱 종료로 중단됨')
                changed += 1
            except OSError:
                logger.warning('could not mark an interrupted scheduled backup')
    return changed


def select_for_deletion(owned: List[OwnedBackup], retention_count: int, retention_days: int,
                        now: Optional[datetime] = None) -> List[OwnedBackup]:
    """보존 정책에 따라 삭제할 소유 백업 선정.

    - running 은 절대 선정하지 않는다.
    - 완료 백업은 최근 retention_count 개를 보존하고, retention_days 보다 오래된 것도 선정하되
      **가장 최근 완료 백업은 항상 남긴다** (오래된 백업만 남은 상태에서 전부 지워지지 않도록).
    - 실패/중단 백업은 retention_days 보다 오래되면 선정한다.
    """
    now = now or _now()
    cutoff = now - timedelta(days=max(0, retention_days))
    completed = [b for b in owned if b.state == STATE_COMPLETED]
    victims: List[OwnedBackup] = []
    keep_count = max(1, retention_count)
    if len(completed) > keep_count:
        victims.extend(completed[:len(completed) - keep_count])
    survivors = [b for b in completed if b not in victims]
    for backup in survivors[:-1]:  # 가장 최근 완료 백업은 제외
        if backup.created_at < cutoff:
            victims.append(backup)
    for backup in owned:
        if backup.state in (STATE_FAILED, STATE_INTERRUPTED) and backup.created_at < cutoff:
            victims.append(backup)
    unique = {b.path: b for b in victims}
    return sorted(unique.values(), key=lambda b: b.created_at)


def delete_owned(output_dir: str, backup: OwnedBackup) -> bool:
    """소유가 다시 증명된 폴더 하나를 삭제한다 (삭제 직전 재검증)."""
    with _LOCK:
        base = os.path.realpath(output_dir)
        path = backup.path
        if os.path.islink(path) or os.path.realpath(os.path.dirname(path)) != base:
            return False  # output_dir 바로 아래의 실제 폴더만
        marker = read_marker(path)
        if not marker or marker['schedule_id'] != backup.schedule_id or marker['state'] == STATE_RUNNING:
            return False
        shutil.rmtree(path)
        return True


def apply_retention(output_dir: str, schedule_id: str, retention_count: int, retention_days: int,
                    now: Optional[datetime] = None) -> List[str]:
    """보존 정책을 적용하고 삭제한 경로를 돌려준다. 실패한 삭제는 건너뛰고 기록만 남긴다."""
    removed: List[str] = []
    for backup in select_for_deletion(list_owned(output_dir, schedule_id), retention_count, retention_days, now):
        try:
            if delete_owned(output_dir, backup):
                removed.append(backup.path)
        except OSError:
            logger.warning('scheduled backup retention could not delete a folder', exc_info=True)
    return removed
