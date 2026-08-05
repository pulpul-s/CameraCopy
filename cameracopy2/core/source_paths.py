from __future__ import annotations

from pathlib import Path

from cameracopy2.config import normalize_source_subfolder
from cameracopy2.models import VolumeInfo


def resolve_source_root(volume: VolumeInfo, source: str) -> Path:
    """Resolve a configured source strictly under the selected volume.

    Source is volume-relative unless the user provides an absolute path that is
    already inside the selected volume. CameraCopy must not infer /home, /media,
    or any other path that was not selected.
    """
    raw_source = str(source or "").strip()
    mount_path = volume.mount_path.expanduser()
    resolved_mount_path = _safe_resolve(mount_path)
    relative_source = normalize_source_subfolder(raw_source)

    expanded = Path(raw_source).expanduser() if raw_source else None
    if expanded is not None and _looks_absolute(raw_source, expanded):
        absolute = _safe_resolve(expanded)
        if _is_relative_to(absolute, resolved_mount_path):
            return absolute

    return mount_path / relative_source if relative_source else mount_path


def source_display_text(volume: VolumeInfo | None, source: str) -> str:
    if volume is None:
        return normalize_source_subfolder(source) or ""
    return str(resolve_source_root(volume, source))


def _looks_absolute(raw_source: str, path: Path) -> bool:
    return (
        raw_source.startswith("~")
        or path.is_absolute()
        or (len(raw_source) >= 2 and raw_source[1] == ":")
    )


def _safe_resolve(path: Path) -> Path:
    try:
        return path.resolve(strict=False)
    except OSError:
        return path.absolute()


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True
