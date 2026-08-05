from __future__ import annotations

import json
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Any

from dateutil import parser as date_parser

RATING_TAGS = ("XMP:Rating", "Rating", "XMP-xmp:Rating")
TIMESTAMP_TAGS = (
    "EXIF:DateTimeOriginal",
    "DateTimeOriginal",
    "CreateDate",
    "MediaCreateDate",
    "TrackCreateDate",
    "XMP:CreateDate",
    "XMP:DateCreated",
    "XMP-photoshop:DateCreated",
)
DEFAULT_TAGS = tuple(dict.fromkeys((*RATING_TAGS, *TIMESTAMP_TAGS)))
DEFAULT_BATCH_SIZE = 100

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MetadataCapability:
    executable: str
    available: bool
    path: str | None


@dataclass(slots=True)
class _MetadataCacheEntry:
    data: dict[str, Any]
    tags: set[str]


class ExifToolService:
    """Optional subprocess wrapper around ExifTool.

    ExifTool is intentionally not a Python dependency. The service fails softly when
    the executable is missing or when metadata cannot be read. A per-file cache and
    chunked batch warmup path avoid one subprocess per file during large copy jobs.
    """

    def __init__(self, executable: str = "exiftool") -> None:
        self.executable = executable
        self.executable_path = shutil.which(executable)
        self.available = self.executable_path is not None
        self._cache: dict[Path, _MetadataCacheEntry] = {}

    def capability(self) -> MetadataCapability:
        return MetadataCapability(
            executable=self.executable,
            available=self.available,
            path=self.executable_path,
        )

    def warm_cache(
        self,
        paths: list[Path],
        tags: list[str] | tuple[str, ...] | None = None,
        *,
        cancel_event: Event | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        """Batch-read metadata for paths that are not already cached.

        This is best-effort. Failed batch calls are not cached, so later reads can
        retry. Cache entries track which tag sets were requested, preventing a
        narrow rating-only read from hiding a later timestamp read.
        """
        if not self.available or (cancel_event is not None and cancel_event.is_set()):
            return

        tag_tuple = self._normalized_tags(tags)
        wanted = list(dict.fromkeys(Path(path).resolve() for path in paths))
        missing = [
            path
            for path in wanted
            if path.exists() and not self._cache_covers(path, tag_tuple)
        ]
        if not missing:
            return

        batch_size = max(1, int(batch_size or DEFAULT_BATCH_SIZE))
        for start in range(0, len(missing), batch_size):
            if cancel_event is not None and cancel_event.is_set():
                return
            batch = missing[start : start + batch_size]
            payload = self._run_json_command(
                tag_tuple,
                batch,
                timeout=max(20, len(batch) * 2),
                cancel_event=cancel_event,
            )
            if not isinstance(payload, list):
                continue
            for item in payload:
                if not isinstance(item, dict):
                    continue
                source = item.get("SourceFile")
                if source:
                    self._merge_cache(Path(str(source)).resolve(), item, tag_tuple)

    def read_json(
        self, path: Path, tags: list[str] | tuple[str, ...] | None = None
    ) -> dict[str, Any]:
        if not self.available:
            return {}

        resolved = Path(path).resolve()
        tag_tuple = self._normalized_tags(tags)
        if self._cache_covers(resolved, tag_tuple):
            return dict(self._cache[resolved].data)

        payload = self._run_json_command(tag_tuple, [path], timeout=20)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            self._merge_cache(resolved, payload[0], tag_tuple)
            return dict(self._cache[resolved].data)
        return dict(self._cache[resolved].data) if resolved in self._cache else {}

    @staticmethod
    def _normalized_tags(
        tags: list[str] | tuple[str, ...] | None,
    ) -> tuple[str, ...]:
        return tuple(dict.fromkeys(tags or DEFAULT_TAGS))

    def _cache_covers(self, path: Path, tags: tuple[str, ...]) -> bool:
        entry = self._cache.get(path)
        return entry is not None and set(tags).issubset(entry.tags)

    def _merge_cache(
        self, path: Path, data: dict[str, Any], tags: tuple[str, ...]
    ) -> None:
        entry = self._cache.get(path)
        if entry is None:
            self._cache[path] = _MetadataCacheEntry(dict(data), set(tags))
            return
        entry.data.update(data)
        entry.tags.update(tags)

    def read_all_json(self, path: Path) -> dict[str, Any]:
        """Read all metadata ExifTool exposes for a file.

        Used by diagnostics only. Copy jobs intentionally read a small tag set for
        performance.
        """
        if not self.available:
            return {}
        payload = self._run_json_command((), [path], timeout=20)
        if isinstance(payload, list) and payload and isinstance(payload[0], dict):
            return payload[0]
        return {}

    def _build_json_command(self, tags: tuple[str, ...], paths: list[Path]) -> list[str]:
        command = [self.executable, "-j", "-n", "-G"]
        command.extend(f"-{tag}" for tag in tags)
        command.append("--")
        command.extend(str(path) for path in paths)
        return command

    def _run_json_command(
        self,
        tags: tuple[str, ...],
        paths: list[Path],
        *,
        timeout: int,
        cancel_event: Event | None = None,
    ) -> object:
        if cancel_event is not None and cancel_event.is_set():
            return []
        command = self._build_json_command(tags, paths)
        if cancel_event is None:
            try:
                completed = subprocess.run(
                    command,
                    check=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout,
                )
                return json.loads(completed.stdout or "[]")
            except Exception as exc:  # noqa: BLE001 - optional external tool
                logger.warning("ExifTool metadata read failed for %s: %s", paths, exc)
                return []

        process: subprocess.Popen[str] | None = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + timeout
            stdout = ""
            while True:
                if cancel_event.is_set():
                    self._terminate_process(process)
                    return []
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning("ExifTool metadata read timed out for %s", paths)
                    self._kill_process(process)
                    return []
                try:
                    stdout, _stderr = process.communicate(timeout=min(0.1, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode != 0:
                logger.warning(
                    "ExifTool exited with code %s for %s", process.returncode, paths
                )
                return []
            return json.loads(stdout or "[]")
        except Exception as exc:  # noqa: BLE001 - optional external tool
            logger.warning("ExifTool metadata read failed for %s: %s", paths, exc)
            if process is not None and process.poll() is None:
                self._kill_process(process)
            return []

    @staticmethod
    def _terminate_process(process: subprocess.Popen[str]) -> None:
        try:
            process.terminate()
            process.communicate(timeout=2)
        except subprocess.TimeoutExpired:
            ExifToolService._kill_process(process)
        except Exception as exc:  # noqa: BLE001 - best-effort process cleanup
            logger.warning("Could not terminate ExifTool cleanly: %s", exc)
            ExifToolService._kill_process(process)

    @staticmethod
    def _kill_process(process: subprocess.Popen[str]) -> None:
        try:
            process.kill()
            process.communicate(timeout=2)
        except Exception as exc:  # noqa: BLE001 - best-effort process cleanup
            logger.warning("Could not kill ExifTool cleanly: %s", exc)

    def read_rating(self, path: Path) -> int | None:
        data = self.read_json(path, RATING_TAGS)
        for tag in RATING_TAGS:
            value = _get_metadata_value(data, tag)
            if value is None:
                continue
            try:
                rating = int(float(value))
            except (TypeError, ValueError):
                continue
            if 0 <= rating <= 5:
                return rating
        return None

    def read_capture_timestamp(self, path: Path) -> datetime | None:
        data = self.read_json(path, TIMESTAMP_TAGS)
        for tag in TIMESTAMP_TAGS:
            value = _get_metadata_value(data, tag)
            if not value:
                continue
            parsed = _parse_exiftool_datetime(value)
            if parsed is not None:
                return parsed
        return None


def _parse_exiftool_datetime(value: Any) -> datetime | None:
    text = str(value).strip()
    if not text:
        return None
    for fmt in (
        "%Y:%m:%d %H:%M:%S",
        "%Y:%m:%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S%z",
    ):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    try:
        return date_parser.parse(text)
    except (TypeError, ValueError, OverflowError):
        return None


def _get_metadata_value(data: dict[str, Any], tag: str) -> Any:
    """Handle both grouped and ungrouped ExifTool JSON keys.

    With `-G`, keys are usually shaped like `EXIF:DateTimeOriginal` or
    `XMP:Rating`. Older cached data or tests may use the plain tag name.
    """
    if tag in data:
        return data[tag]
    short = tag.split(":")[-1]
    if short in data:
        return data[short]
    for key, value in data.items():
        if key.split(":")[-1] == short:
            return value
    return None
