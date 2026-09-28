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
