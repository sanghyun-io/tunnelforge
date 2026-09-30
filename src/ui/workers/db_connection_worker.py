"""Connection attempts and cleanup without blocking the GUI thread."""
from PyQt6.QtCore import Qt, pyqtSignal, pyqtSlot

from src.core.logger import get_logger
from src.ui.workers.cancellable_worker import CancellableWorker


_active_workers = set()
logger = get_logger("db_connection_worker")


def has_active_connection_workers():
    # A stopped thread can still have an unclaimed result queued for the GUI.
    return bool(_active_workers)


class DBConnectionWorker(CancellableWorker):
    connection_finished = pyqtSignal(object)

    def __init__(self, connector, action="connect"):
        # A dialog may be destroyed while the Core RPC is still running.
        super().__init__(None)
        self.connector = connector
        self.action = action
        self.success = False
        self.message = ""
        self.error = False
        self.finished.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    def start(self, *args):
        _active_workers.add(self)
        try:
            super().start(*args)
        except Exception:
            _active_workers.discard(self)
            raise

    def run(self):
        try:
            if self.action != "disconnect" and not self._cancelled:
                self.success, self.message = self.connector.connect()
        except Exception as exc:
            self.message, self.error = str(exc), True
        finally:
            if self.action != "connect" or not self.success or self._cancelled:
                connector, self.connector = self.connector, None
                try:
                    connector.disconnect()
                except Exception as exc:
                    self.success, self.message, self.error = False, str(exc), True
                    logger.warning("Connection cleanup failed: %s", exc)

    def take_connector(self):
        connector, self.connector = self.connector, None
        return connector

    @pyqtSlot()
    def _finish(self):
        # Deliver only after native QThread completion. The receiver claims a
        # successful connection synchronously; cancelled/deleted dialogs do not.
        try:
            self.connection_finished.emit(self)
        finally:
            if self.connector is not None:
                DBConnectionWorker(self.take_connector(), "disconnect").start()
            _active_workers.discard(self)
            self.deleteLater()


_inflight_tunnels = set()


def is_tunnel_start_in_flight(tunnel_id):
    """취소된 시작 시도가 아직 백그라운드에서 정리 중인지 (같은 터널의 중복 시작 방지)."""
    return tunnel_id in _inflight_tunnels


class TunnelStartWorker(CancellableWorker):
    """SSH 터널 시작(TOFU 확인/개인키 비밀번호 포함)을 GUI 밖에서 수행한다.

    DBConnectionWorker 와 같은 소유권 순서: native QThread.finished 이후 queued 완료를 전달하고
    활성 집합(앱 종료 보호)에서 제거한다. 취소된 시도가 성공으로 끝나면 이번 시도가 연 터널을 닫는다.
    """
    tunnel_finished = pyqtSignal(object)

    def __init__(self, engine, config, check_port=None):
        super().__init__(None)
        self.engine = engine
        self.config = config
        self.check_port = check_port
        self.success = False
        self.message = ""
        self._was_running = bool(engine.is_running(config.get('id')))
        self.finished.connect(self._finish, Qt.ConnectionType.QueuedConnection)

    def start(self, *args):
        _active_workers.add(self)
        _inflight_tunnels.add(self.config.get('id'))
        try:
            super().start(*args)
        except Exception:
            self._release()
            raise

    def run(self):
        try:
            kwargs = {} if self.check_port is None else {'check_port': self.check_port}
            self.success, self.message = self.engine.start_tunnel(self.config, **kwargs)
        except Exception as exc:
            self.success, self.message = False, str(exc)
        finally:
            if self._cancelled and self.success and not self._was_running:
                # 사용자가 취소했는데 뒤늦게 열린 터널은 남기지 않는다.
                try:
                    self.engine.stop_tunnel(self.config.get('id'))
                except Exception as exc:
                    logger.warning("Cancelled tunnel cleanup failed: %s", exc)
                self.success, self.message = False, "cancelled"

    def _release(self):
        _inflight_tunnels.discard(self.config.get('id'))
        _active_workers.discard(self)

    @pyqtSlot()
    def _finish(self):
        try:
            self.tunnel_finished.emit(self)
        finally:
            self._release()
            self.deleteLater()
