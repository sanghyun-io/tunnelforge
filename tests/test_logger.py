import logging

from src.core.logger import WindowsSafeRotatingFileHandler


def test_windows_safe_rotating_handler_keeps_logging_when_rollover_is_locked(tmp_path):
    log_file = tmp_path / "tunnelforge.log"
    log_file.write_text("x" * 32, encoding="utf-8")
    handler = WindowsSafeRotatingFileHandler(
        log_file,
        maxBytes=1,
        backupCount=1,
        encoding="utf-8",
        rollover_retry_seconds=3600,
    )
    errors = []
    rotate_calls = []

    def locked_rotate(source, dest):
        rotate_calls.append((source, dest))
        raise PermissionError("locked")

    handler.rotate = locked_rotate
    handler.handleError = lambda record: errors.append(record)
    handler.setFormatter(logging.Formatter("%(message)s"))

    try:
        handler.emit(
            logging.LogRecord("tunnelforge.test", logging.INFO, __file__, 1, "first", (), None)
        )
        handler.emit(
            logging.LogRecord("tunnelforge.test", logging.INFO, __file__, 1, "second", (), None)
        )
    finally:
        handler.close()

    content = log_file.read_text(encoding="utf-8")
    assert errors == []
    assert "first" in content
    assert "second" in content
    assert len(rotate_calls) == 1
