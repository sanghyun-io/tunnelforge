import os
import re
from pathlib import Path

from src.core import oneclick_log
from src.core.oneclick_log import close_oneclick_logger, create_oneclick_logger


def _make_logger(tmp_path, monkeypatch, schema):
    monkeypatch.setattr(oneclick_log, "_get_migration_log_dir", lambda: str(tmp_path))
    return create_oneclick_logger(schema)


def test_normal_schema_creates_dated_log_file(tmp_path, monkeypatch):
    logger, log_path = _make_logger(tmp_path, monkeypatch, "customerdb")

    assert Path(log_path).parent == tmp_path
    assert re.fullmatch(
        r"migration_customerdb_\d{8}_\d{6}_[0-9a-f]{8}\.log", Path(log_path).name
    )
    assert Path(log_path).exists()
    close_oneclick_logger(logger)


def test_schema_with_path_and_windows_invalid_chars_stays_in_the_log_dir(tmp_path, monkeypatch):
    logger, log_path = _make_logger(tmp_path, monkeypatch, "a/b:c*")

    # No subdirectory is created and nothing raises: the unsafe characters are
    # replaced in the file name only.
    assert Path(log_path).parent == tmp_path
    assert Path(log_path).exists()
    assert "/" not in Path(log_path).name
    assert ":" not in Path(log_path).name
    close_oneclick_logger(logger)


def test_two_calls_return_distinct_loggers_and_paths(tmp_path, monkeypatch):
    first_logger, first_path = _make_logger(tmp_path, monkeypatch, "proddb")
    second_logger, second_path = _make_logger(tmp_path, monkeypatch, "proddb")

    assert first_logger is not second_logger
    assert first_path != second_path
    close_oneclick_logger(first_logger)
    close_oneclick_logger(second_logger)


def test_close_removes_handlers_and_releases_the_file(tmp_path, monkeypatch):
    logger, log_path = _make_logger(tmp_path, monkeypatch, "proddb")

    close_oneclick_logger(logger)

    assert not logger.handlers
    # Raises PermissionError on Windows while the FileHandler still holds it.
    os.remove(log_path)
