"""Shared cancellable QThread base class."""

from PyQt6.QtCore import QThread


class CancellableWorker(QThread):
    """QThread with a simple cooperative cancellation flag."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True
