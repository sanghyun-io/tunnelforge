from src.ui.workers.cancellable_worker import CancellableWorker
from src.ui.workers.validation_worker import AutoCompleteWorker, MetadataLoadWorker, ValidationWorker


def test_cancellable_worker_sets_cancelled_flag():
    worker = CancellableWorker()

    assert worker._cancelled is False
    worker.cancel()
    assert worker._cancelled is True


def test_validation_workers_inherit_cancellable_worker():
    assert issubclass(ValidationWorker, CancellableWorker)
    assert issubclass(MetadataLoadWorker, CancellableWorker)
    assert issubclass(AutoCompleteWorker, CancellableWorker)
