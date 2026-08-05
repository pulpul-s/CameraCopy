from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

VolumeScanKind = Literal[
    "startup",
    "poll",
    "manual",
    "settings",
    "settings_dialog",
    "post_format",
    "pre_copy",
]
_SCAN_PRIORITY: dict[VolumeScanKind, int] = {
    "startup": 0,
    "poll": 0,
    "settings": 0,
    "settings_dialog": 0,
    "post_format": 0,
    "manual": 1,
    "pre_copy": 2,
}


@dataclass(frozen=True, slots=True)
class VolumeScanRequest:
    kind: VolumeScanKind
    included_devices: tuple[str, ...]


def prefer_scan_request(
    current: VolumeScanRequest | None, new: VolumeScanRequest
) -> VolumeScanRequest:
    """Keep one pending scan, preferring pre-copy and manual requests."""
    if current is None or _SCAN_PRIORITY[new.kind] >= _SCAN_PRIORITY[current.kind]:
        return new
    return current
