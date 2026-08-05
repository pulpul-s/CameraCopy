from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psutil

from cameracopy2.models import VolumeInfo
from cameracopy2.services.volume_service import VolumeService, volume_display_name, volume_sort_key


@dataclass(slots=True)
class WindowsPhysicalDetails:
    model: str | None = None
    device_serial: str | None = None
    hardware_path: str | None = None


class WindowsVolumeService(VolumeService):
    def list_volumes(self) -> list[VolumeInfo]:
        wmi_client = self._wmi_client()
        logical_disks = self._logical_disks(wmi_client)
        physical_details = self._physical_details_by_logical_disk(logical_disks)

        volumes: list[VolumeInfo] = []
        for partition in psutil.disk_partitions(all=False):
            mount = Path(partition.mountpoint)
            key = partition.device.rstrip("\\").upper()
            disk = logical_disks.get(key)
            details = physical_details.get(key, WindowsPhysicalDetails())
            label = _wmi_value(disk, "VolumeName")
            model = details.model or _wmi_value(disk, "Description")
            uuid = _wmi_value(disk, "VolumeSerialNumber")
            try:
                size = (
                    int(_wmi_value(disk, "Size") or 0)
                    if disk
                    else psutil.disk_usage(partition.mountpoint).total
                )
            except Exception:
                size = None
            filesystem = _wmi_value(disk, "FileSystem") or (partition.fstype or None)
            drive_type = _wmi_value(disk, "DriveType")
            removable = _removable_from_drive_type(drive_type)
            volume = VolumeInfo(
                id=self._make_volume_id(partition.device, partition.mountpoint, uuid),
                display_name="",
                mount_path=mount,
                device_path=partition.device,
                label=label,
                model=model,
                size_bytes=size,
                filesystem=filesystem,
                removable=removable,
                uuid=uuid,
                transport=None,
                device_serial=details.device_serial,
                partition_uuid=None,
                hardware_path=details.hardware_path,
                platform="windows",
            )
            volume.display_name = volume_display_name(volume)
            volumes.append(volume)
        return sorted(volumes, key=volume_sort_key)

    @staticmethod
    def _wmi_client() -> Any | None:
        try:
            import wmi

            return wmi.WMI()
        except Exception:
            return None

    @staticmethod
    def _logical_disks(wmi_client: Any | None) -> dict[str, Any]:
        if wmi_client is None:
            return {}
        try:
            disks = wmi_client.Win32_LogicalDisk()
        except Exception:
            return {}
        mapping: dict[str, Any] = {}
        for disk in disks:
            device_id = _clean(_wmi_value(disk, "DeviceID"))
            if device_id:
                mapping[device_id.upper()] = disk
        return mapping

    @staticmethod
    def _physical_details_by_logical_disk(
        logical_disks: dict[str, Any],
    ) -> dict[str, WindowsPhysicalDetails]:
        """Best-effort physical details for an already enumerated logical-disk map."""
        mapping: dict[str, WindowsPhysicalDetails] = {}
        for drive, logical in logical_disks.items():
            details = WindowsPhysicalDetails()
            try:
                partitions = logical.associators(wmi_result_class="Win32_DiskPartition")
            except Exception:
                mapping[drive] = details
                continue

            for partition in partitions:
                try:
                    disks = partition.associators(wmi_result_class="Win32_DiskDrive")
                except Exception:
                    continue
                for disk in disks:
                    details.model = _clean(_wmi_value(disk, "Model")) or _clean(
                        _wmi_value(disk, "Caption")
                    )
                    serial = _clean(_wmi_value(disk, "SerialNumber"))
                    pnp = _clean(_wmi_value(disk, "PNPDeviceID"))
                    details.device_serial = serial or pnp
                    details.hardware_path = pnp
                    break
                break
            mapping[drive] = details
        return mapping


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _wmi_value(instance: Any | None, name: str) -> Any:
    if instance is None:
        return None
    try:
        return getattr(instance, name, None)
    except Exception:
        return None


def _removable_from_drive_type(value: Any) -> bool | None:
    if value is None:
        return None
    try:
        return int(value) == 2
    except (TypeError, ValueError, OverflowError):
        return None
