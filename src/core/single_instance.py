"""Single-instance guard for the desktop application."""
import os
import sys
import tempfile
import time

from PyQt6.QtCore import QLockFile, QObject, pyqtSignal
from PyQt6.QtNetwork import QLocalServer, QLocalSocket


DEFAULT_SERVER_NAME = "tunnelforge-single-instance-v1"
DEFAULT_LOCK_FILE = os.path.join(tempfile.gettempdir(), "tunnelforge-single-instance.lock")

_CONNECT_ATTEMPT_TIMEOUT_MS = 100
_POLL_INTERVAL_SECONDS = 0.05


class SingleInstanceGuard(QObject):
    """Own a local server so later launches can wake the first instance."""

    activation_requested = pyqtSignal()

    def __init__(
        self,
        server_name: str = DEFAULT_SERVER_NAME,
        lock_file: str = DEFAULT_LOCK_FILE,
        parent=None,
    ):
        super().__init__(parent)
        self.server_name = server_name
        self._lock = QLockFile(lock_file)
        self._lock.setStaleLockTime(1000)
        self._server = QLocalServer(self)
        self._is_primary = self._lock.tryLock(0)

        if self._is_primary:
            self._listen()

    @property
    def is_primary(self) -> bool:
        return self._is_primary

    @property
    def is_secondary(self) -> bool:
        return not self._is_primary

    def close(self):
        """Release the local server name held by the primary instance."""
        if not self._is_primary:
            return
        try:
            if self._server.isListening():
                self._server.close()
        except RuntimeError:
            pass
        QLocalServer.removeServer(self.server_name)
        self._lock.unlock()

    def _listen(self):
        self._set_user_only_access()
        if self._server.listen(self.server_name):
            self._server.newConnection.connect(self._on_new_connection)
            return

        QLocalServer.removeServer(self.server_name)
        self._set_user_only_access()
        if self._server.listen(self.server_name):
            self._server.newConnection.connect(self._on_new_connection)

    def _set_user_only_access(self):
        if hasattr(QLocalServer, "SocketOption"):
            self._server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)

    def _on_new_connection(self):
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            if socket:
                socket.readyRead.connect(socket.readAll)
                socket.disconnected.connect(socket.deleteLater)
                self.activation_requested.emit()
                socket.disconnectFromServer()

    @staticmethod
    def notify_existing_instance(
        server_name: str = DEFAULT_SERVER_NAME,
        timeout_ms: int = 1000,
    ) -> bool:
        """Send an activation request to the already-running instance."""
        deadline = time.monotonic() + (timeout_ms / 1000)

        while time.monotonic() < deadline:
            socket = QLocalSocket()
            socket.connectToServer(server_name)
            if socket.waitForConnected(_CONNECT_ATTEMPT_TIMEOUT_MS):
                message = f"activate:{sys.argv[0]}\n".encode("utf-8", errors="replace")
                socket.write(message)
                socket.flush()
                socket.waitForBytesWritten(_CONNECT_ATTEMPT_TIMEOUT_MS)
                socket.disconnectFromServer()
                socket.deleteLater()
                return True
            socket.deleteLater()
            time.sleep(_POLL_INTERVAL_SECONDS)

        return False
