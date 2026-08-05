from __future__ import annotations

import platform as py_platform
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import psutil

from cameracopy2.models import VolumeInfo


FormatTargetMatchReason = Literal[
    "matched",
    "disconnected",
    "changed",
    "ambiguous",
    "identity_unavailable",
    "enumeration_failed",
]


@dataclass(frozen=True, slots=True)
class FormatTargetMatch:
    volume: VolumeInfo | None
    reason: FormatTargetMatchReason

    @property
    def ok(self) -> bool:
        return self.volume is not None and self.reason == "matched"


def human_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return "unknown size"
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"


def volume_display_name(volume: VolumeInfo) -> str:
    parts = [str(volume.mount_path)]
    for value in (volume.label, volume.model, volume.filesystem, volume.transport):
        if value and str(value) not in parts:
            parts.append(str(value))
    parts.append(human_size(volume.size_bytes))
    return " - ".join(parts)


def volume_sort_key(volume: VolumeInfo) -> tuple[bool, str]:
    return (not volume.is_likely_removable, volume.display_name.lower())


class VolumeService:
    def list_volumes(self) -> list[VolumeInfo]:
        volumes: list[VolumeInfo] = []
        system = py_platform.system().lower()
        for partition in psutil.disk_partitions(all=False):
            mount = Path(partition.mountpoint)
            size = self._usage_total(partition.mountpoint)
            volume = VolumeInfo(
                id=self._make_volume_id(partition.device, partition.mountpoint, None),
                display_name="",
                mount_path=mount,
                device_path=partition.device,
                label=None,
                model=None,
                size_bytes=size,
                filesystem=partition.fstype or None,
                removable=None,
                uuid=None,
                transport=None,
                device_serial=None,
                partition_uuid=None,
                hardware_path=partition.device,
                platform=system,
            )
            volume.display_name = volume_display_name(volume)
            volumes.append(volume)
        return sorted(volumes, key=volume_sort_key)

    def filtered_volumes(self, keywords: list[str]) -> list[VolumeInfo]:
        return [volume for volume in self.list_volumes() if volume.matches_keywords(keywords)]

    @staticmethod
    def find_matching_copy_source(
        selected: VolumeInfo, current_volumes: list[VolumeInfo]
    ) -> VolumeInfo | None:
        """Re-resolve a selected source by its current mounted location.

        Filesystem and partition UUIDs describe on-media metadata and may be
        duplicated. Filesystem UUID may reject a changed card; partition UUID
        is ignored. Neither chooses a physical source by itself.
        """
        if selected.device_path:
            matches = [
                volume
                for volume in current_volumes
                if _normalize_identity(volume.device_path)
                == _normalize_identity(selected.device_path)
                and _volume_metadata_does_not_conflict(selected, volume)
            ]
            if len(matches) == 1:
                return matches[0]
            if matches:
                return None

        matches = [
            volume
            for volume in current_volumes
            if volume.mount_path == selected.mount_path
            and _volume_metadata_does_not_conflict(selected, volume)
        ]
        return matches[0] if len(matches) == 1 else None

    def find_matching_format_target(
        self,
        selected: VolumeInfo,
        current_volumes: list[VolumeInfo] | None = None,
    ) -> FormatTargetMatch:
        """Strictly re-resolve a volume before a destructive format operation."""
        filesystem_uuid = _normalize_identity(selected.uuid)
        if not filesystem_uuid:
            return FormatTargetMatch(None, "identity_unavailable")

        if current_volumes is None:
            try:
                current_volumes = self.list_volumes()
            except Exception:
                return FormatTargetMatch(None, "enumeration_failed")

        location_matches: list[VolumeInfo] = []
        if selected.device_path:
            location_matches = [
                volume
                for volume in current_volumes
                if _normalize_identity(volume.device_path)
                == _normalize_identity(selected.device_path)
            ]
        if not location_matches:
            location_matches = [
                volume for volume in current_volumes if volume.mount_path == selected.mount_path
            ]

        if len(location_matches) > 1:
            return FormatTargetMatch(None, "ambiguous")
        if len(location_matches) == 1:
            match = location_matches[0]
            if not _volume_metadata_does_not_conflict(selected, match):
                return FormatTargetMatch(None, "changed")
            return FormatTargetMatch(match, "matched")

        uuid_matches = [
            volume
            for volume in current_volumes
            if _normalize_identity(volume.uuid) == filesystem_uuid
        ]
        if len(uuid_matches) > 1:
            return FormatTargetMatch(None, "ambiguous")
        if len(uuid_matches) == 1:
            match = uuid_matches[0]
            if not _volume_metadata_does_not_conflict(selected, match):
                return FormatTargetMatch(None, "changed")
            return FormatTargetMatch(match, "matched")

        return FormatTargetMatch(None, "disconnected")

    @staticmethod
    def _usage_total(mountpoint: str) -> int | None:
        try:
            return psutil.disk_usage(mountpoint).total
        except OSError:
            return None

    @staticmethod
    def _make_volume_id(device: str | None, mountpoint: str, uuid: str | None) -> str:
        if uuid:
            return f"uuid:{uuid}:{mountpoint}"
        return f"{device}:{mountpoint}"


def create_volume_service() -> VolumeService:
    system = py_platform.system().lower()
    if system == "linux":
        try:
            from cameracopy2.platform.linux import LinuxVolumeService

            return LinuxVolumeService()
        except Exception:
            return VolumeService()
    if system == "windows":
        try:
            from cameracopy2.platform.windows import WindowsVolumeService

            return WindowsVolumeService()
        except Exception:
            return VolumeService()
    return VolumeService()


def _normalize_identity(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().strip("{}").lower()
    return normalized or None


def _volume_metadata_does_not_conflict(
    selected: VolumeInfo,
    current: VolumeInfo,
) -> bool:
    for attr in ("uuid", "device_serial", "hardware_path"):
        selected_value = _normalize_identity(getattr(selected, attr))
        current_value = _normalize_identity(getattr(current, attr))
        if selected_value and current_value and selected_value != current_value:
            return False
    return _sizes_do_not_conflict(selected, current)


def _sizes_do_not_conflict(selected: VolumeInfo, current: VolumeInfo) -> bool:
    return (
        selected.size_bytes is None
        or current.size_bytes is None
        or selected.size_bytes == current.size_bytes
    )


def volumes_refer_to_same_mounted_volume(first: VolumeInfo, second: VolumeInfo) -> bool:
    """Return whether two selections point to the same currently mounted volume."""
    first_device = _normalize_identity(first.device_path)
    second_device = _normalize_identity(second.device_path)
    if first_device and second_device and first_device == second_device:
        return True
    return first.mount_path == second.mount_path
