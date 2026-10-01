"""작업 공간 스냅샷을 백그라운드 스레드 하나로 저장한다 (Qt 비의존).

UI 스레드는 스냅샷만 만들어 submit() 하고, 느린 fsync 는 이 스레드가 맡는다. 대기 중인
저장이 있으면 최신 스냅샷 하나로 합친다(coalescing). 종료 시 flush() 로 마지막 저장을 보장한다.
"""
import threading
from typing import Callable, Optional

from src.core.logger import get_logger
from src.core.workspace_store import WorkspaceState, WorkspaceStore, WorkspaceStoreError

logger = get_logger('workspace_writer')

_DELETE = object()


class AsyncWorkspaceWriter:
    def __init__(self, store: WorkspaceStore, profile_id: str):
        self._store = store
        self._profile_id = profile_id
        self._cond = threading.Condition()
        self._pending = None  # WorkspaceState | _DELETE | None
        self._busy = False
        self._stopped = False
        self._last_error: Optional[str] = None
        self._omitted: list = []
        self._thread = threading.Thread(target=self._run, name='workspace-writer', daemon=True)
        self._thread.start()

    # -- UI 스레드 API ---------------------------------------------------
    def submit(self, state: WorkspaceState) -> None:
        self._enqueue(state)

    def request_delete(self) -> None:
        self._enqueue(_DELETE)

    def _enqueue(self, item) -> None:
        with self._cond:
            if self._stopped:
                return
            self._pending = item
            self._cond.notify_all()

    def flush(self, timeout: float = 5.0) -> bool:
        """대기 중이던 저장이 끝날 때까지 기다린다. 시간 안에 끝나면 True."""
        with self._cond:
            return self._cond.wait_for(lambda: self._pending is None and not self._busy, timeout)

    def stop(self, timeout: float = 5.0) -> None:
        self.flush(timeout)
        with self._cond:
            self._stopped = True
            self._cond.notify_all()
        self._thread.join(timeout)

    def take_error(self) -> Optional[str]:
        """마지막 쓰기 실패 메시지를 한 번만 돌려준다 (UI 가 경고를 한 번만 띄우도록)."""
        with self._cond:
            error, self._last_error = self._last_error, None
            return error

    def take_omitted(self) -> list:
        with self._cond:
            omitted, self._omitted = self._omitted, []
            return omitted

    # -- 작업 스레드 -----------------------------------------------------
    def _run(self) -> None:
        while True:
            with self._cond:
                self._cond.wait_for(lambda: self._pending is not None or self._stopped)
                if self._pending is None:
                    return
                item, self._pending = self._pending, None
                self._busy = True
            try:
                if item is _DELETE:
                    self._store.delete(self._profile_id)
                else:
                    result = self._store.save(item)
                    if result.omitted_tabs:
                        with self._cond:
                            self._omitted = result.omitted_tabs
            except WorkspaceStoreError as exc:
                logger.warning('workspace save failed: %s', exc)
                with self._cond:
                    self._last_error = str(exc)
            except Exception:  # 저장 실패가 에디터를 죽이면 안 된다
                logger.warning('workspace save failed unexpectedly', exc_info=True)
                with self._cond:
                    self._last_error = 'unexpected error'
            finally:
                with self._cond:
                    self._busy = False
                    self._cond.notify_all()
