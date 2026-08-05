from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from threading import Event

from cameracopy2.models import HashResult


HashProgressCallback = Callable[[int, int], None]


def sha256_file(
    path: Path,
    block_size: int = 1024 * 1024,
    cancel_event: Event | None = None,
    progress: HashProgressCallback | None = None,
) -> str:
    digest = hashlib.sha256()
    total_bytes = Path(path).stat().st_size
    bytes_done = 0
    if progress:
        progress(0, total_bytes)
    with Path(path).open("rb") as handle:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("cancelled")
            chunk = handle.read(block_size)
            if not chunk:
                break
            digest.update(chunk)
            bytes_done += len(chunk)
            if progress:
                progress(bytes_done, total_bytes)
    return digest.hexdigest()


def compare_sha256_cancellable(
    source: Path,
    destination: Path,
    cancel_event: Event | None,
    source_progress: HashProgressCallback | None = None,
) -> HashResult:
    try:
        source_hash = sha256_file(source, cancel_event=cancel_event, progress=source_progress)
        destination_hash = sha256_file(destination, cancel_event=cancel_event)
        return HashResult(source_hash, destination_hash, source_hash == destination_hash)
    except InterruptedError:
        return HashResult(None, None, False, "cancelled")
    except Exception as exc:  # noqa: BLE001 - callers need structured failure, not traceback.
        return HashResult(None, None, False, str(exc))
