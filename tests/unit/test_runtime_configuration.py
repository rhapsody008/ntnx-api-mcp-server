"""Unit tests for runtime configuration precedence and mode behavior."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest

from src.config import load_settings


def test_load_settings_precedence_env_then_file_then_cli(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("PC_PORT", "9440")
    monkeypatch.setenv("PC_INSECURE", "true")
    monkeypatch.setenv("LOG_LEVEL", "INFO")

    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "\n".join(
            [
                "pc_port: 9441",
                "log_level: WARNING",
                "pc_insecure: false",
            ]
        ),
        encoding="utf-8",
    )

    settings = load_settings(
        config_file=config_file,
        overrides={"pc_port": 9442, "log_level": "ERROR"},
    )

    assert settings.pc_port == 9442
    assert settings.log_level == "ERROR"
    assert settings.pc_insecure is False


def test_artifact_only_mode_allowed_without_pc_host() -> None:
    settings = load_settings(overrides={"pc_host": None})
    assert settings.pc_host is None
    # Access to runtime artifact paths should still be valid in artifact-only mode.
    assert settings.artifacts_dir.exists()
    assert settings.default_artifacts_dir.exists()
    assert settings.log_dir.exists()


def test_settings_tolerate_read_only_directories(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A read-only filesystem must not fail startup — bundled specs still serve."""
    real_mkdir = Path.mkdir

    def _refuse(self, *args, **kwargs):  # type: ignore[no-untyped-def]
        if str(self).startswith("/data/"):
            raise OSError(errno.EROFS, "Read-only file system")
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _refuse)

    settings = load_settings(
        overrides={
            "artifacts_dir": "/data/artifacts",
            "log_dir": "/data/logs",
            "default_artifacts_dir": "/data/specs",
        }
    )

    assert settings.artifacts_dir == Path("/data/artifacts")
    assert settings.log_dir == Path("/data/logs")


def test_settings_reject_directory_path_that_is_a_file(tmp_path: Path) -> None:
    not_a_dir = tmp_path / "logs"
    not_a_dir.write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="not a directory"):
        load_settings(overrides={"log_dir": str(not_a_dir)})
