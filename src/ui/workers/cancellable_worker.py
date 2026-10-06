"""Shared cancellable QThread base class."""

from PyQt6.QtCore import QThread


class CancellableWorker(QThread):
    """QThread with a simple cooperative cancellation flag."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._cancelled = False

    def cancel(self):
        self._cancelled = True


def worker_is_running(worker) -> bool:
    """isRunning() that tolerates deleted Qt objects and non-QThread stand-ins."""
    try:
        is_running = getattr(worker, "isRunning")
        return bool(is_running()) if callable(is_running) else False
    except (AttributeError, RuntimeError, TypeError):
        return False


def has_running_worker(workers: set) -> bool:
    """Return whether any retained worker still runs, dropping the finished ones."""
    active = False
    for worker in list(workers):
        if worker_is_running(worker):
            active = True
        else:
            workers.discard(worker)
    return active
