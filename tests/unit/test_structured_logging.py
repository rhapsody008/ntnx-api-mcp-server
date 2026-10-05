"""Unit tests for standard logging configuration."""

from __future__ import annotations

import errno
import logging
from pathlib import Path

from src.cli import _configure_logging


def test_configure_logging_sets_json_formatter() -> None:
    log_path = _configure_logging("INFO", "json", Path("logs"))
    root = logging.getLogger()
    assert len(root.handlers) == 2
    assert log_path.name.startswith("nutanix-mcp-")
    assert log_path.suffix == ".log"
    formatter = root.handlers[0].formatter
    assert formatter is not None
    assert '"level":"%(levelname)s"' in formatter._fmt  # type: ignore[attr-defined]


def test_configure_logging_sets_text_formatter() -> None:
    _configure_logging("DEBUG", "text", Path("logs"))
    root = logging.getLogger()
    assert len(root.handlers) == 2
    assert root.level == logging.DEBUG
    formatter = root.handlers[0].formatter
    assert formatter is not None
    assert "%(levelname)s" in formatter._fmt  # type: ignore[attr-defined]


def test_configure_logging_degrades_when_log_dir_is_read_only(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """A read-only LOG_DIR must not stop startup; stderr logging continues."""

    def _refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(logging, "FileHandler", _refuse)

    log_path = _configure_logging("INFO", "json", Path("/data/logs"))

    assert log_path is None
    root = logging.getLogger()
    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0], logging.StreamHandler)


def test_configure_logging_degrades_when_log_dir_cannot_be_created(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    def _refuse(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise OSError(errno.EROFS, "Read-only file system")

    monkeypatch.setattr(Path, "mkdir", _refuse)

    assert _configure_logging("INFO", "text", Path("/data/logs/missing")) is None
    assert len(logging.getLogger().handlers) == 1
