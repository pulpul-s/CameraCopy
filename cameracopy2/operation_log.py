from __future__ import annotations

import logging
import os
import platform
import sys
from datetime import datetime
from pathlib import Path

from cameracopy2 import __version__
from cameracopy2.config import APP_NAME, default_config_path

_LOGGER_NAME = "cameracopy2"
_LOG_PREFIX = "copy-"
_LOG_SUFFIX = ".log"
_MAX_OPERATION_LOGS = 5


def operation_log_directory(config_path: Path | None = None) -> Path:
    """Return the per-user directory for copy-operation logs."""
    if sys.platform == "win32":
        settings_path = config_path or default_config_path()
        return settings_path.parent / "logs"

    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return Path(state_home) / APP_NAME / "logs"

    try:
        from platformdirs import user_state_dir

        state_dir = Path(user_state_dir(APP_NAME, appauthor=False))
    except (ImportError, OSError, TypeError):
        state_dir = Path.home() / ".local" / "state" / APP_NAME
    return state_dir / "logs"


class CopyOperationLog:
    """A verbose file log scoped to one user-requested copy operation."""

    def __init__(self, config_path: Path | None = None) -> None:
        self.path: Path | None = None
        self._handler: logging.FileHandler | None = None
        self._logger = logging.getLogger(_LOGGER_NAME)
        self._previous_level = self._logger.level
        self._closed = False

        try:
            directory = operation_log_directory(config_path)
            directory.mkdir(parents=True, exist_ok=True)
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S-%f")
            suffix = 0
            while True:
                suffix_text = "" if suffix == 0 else f"-{suffix}"
                path = directory / (
                    f"{_LOG_PREFIX}{timestamp}{suffix_text}{_LOG_SUFFIX}"
                )
                try:
                    handler = logging.FileHandler(
                        path,
                        mode="x",
                        encoding="utf-8",
                        delay=False,
                    )
                except FileExistsError:
                    suffix += 1
                    continue
                self.path = path
                break
            handler.setLevel(logging.DEBUG)
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s: %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S",
                )
            )
            self._handler = handler
            self._logger.setLevel(logging.DEBUG)
            self._logger.addHandler(handler)
            self.info(
                "CameraCopy %s copy operation started on %s; Python %s",
                __version__,
                platform.platform(),
                platform.python_version(),
            )
        except OSError:
            self.path = None
            self._handler = None

    def debug(self, message: str, *args: object) -> None:
        self._logger.debug(message, *args)

    def info(self, message: str, *args: object) -> None:
        self._logger.info(message, *args)

    def warning(self, message: str, *args: object) -> None:
        self._logger.warning(message, *args)

    def error(self, message: str, *args: object) -> None:
        self._logger.error(message, *args)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        handler = self._handler
        if handler is not None:
            self.info("Copy operation log closed")
            self._logger.removeHandler(handler)
            handler.close()
            self._handler = None
            self._logger.setLevel(self._previous_level)
        if self.path is not None:
            self._remove_old_logs(self.path.parent)

    @staticmethod
    def _remove_old_logs(directory: Path) -> None:
        try:
            logs = sorted(
                directory.glob(f"{_LOG_PREFIX}*{_LOG_SUFFIX}"),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
        except OSError:
            return
        for path in logs[_MAX_OPERATION_LOGS:]:
            try:
                path.unlink()
            except OSError:
                continue
