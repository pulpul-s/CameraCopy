from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import psutil

from cameracopy2.models import VolumeInfo
from cameracopy2.services.volume_service import VolumeService, volume_display_name, volume_sort_key


class LinuxVolumeService(VolumeService):
    def list_volumes(self) -> list[VolumeInfo]:
        udev_context = self._udev_context()
        lsblk = self._lsblk_by_path()
        volumes: list[VolumeInfo] = []
        for partition in psutil.disk_partitions(all=False):
            mount = Path(partition.mountpoint)
            size = self._usage_total(partition.mountpoint)
            label = None
            model = None
            removable = None
            uuid = None
            transport = None
            device_serial = None
            partition_uuid = None
            hardware_path = None

            info = lsblk.get(partition.device, {})
            if info:
                label = info.get("label") or label
                model = info.get("model") or model
                filesystem = info.get("fstype") or partition.fstype or None
                uuid = info.get("uuid") or uuid
                transport = info.get("tran") or transport
                removable = _to_bool_or_none(info.get("rm"))
                partition_uuid = info.get("partuuid") or partition_uuid
                size = _to_int_or_none(info.get("size")) or size
            else:
                filesystem = partition.fstype or None

            if udev_context is not None and partition.device:
                metadata = self._udev_metadata(udev_context, partition.device)
                label = metadata.get("label") or label
                model = metadata.get("model") or model
                uuid = metadata.get("uuid") or uuid
                transport = metadata.get("transport") or transport
                removable = (
                    metadata.get("removable")
                    if metadata.get("removable") is not None
                    else removable
                )
                device_serial = metadata.get("device_serial") or device_serial
                partition_uuid = metadata.get("partition_uuid") or partition_uuid
                hardware_path = metadata.get("hardware_path") or hardware_path

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
                transport=transport,
                device_serial=device_serial,
                partition_uuid=partition_uuid,
                hardware_path=hardware_path,
                platform="linux",
            )
            volume.display_name = volume_display_name(volume)
            volumes.append(volume)
        return sorted(volumes, key=volume_sort_key)

    @staticmethod
    def _udev_context() -> Any | None:
        try:
            import pyudev

            return pyudev.Context()
        except Exception:
            return None

    @staticmethod
    def _udev_metadata(context: Any, device_file: str) -> dict[str, Any]:
        try:
            import pyudev

            device = pyudev.Devices.from_device_file(context, device_file)
            parent = device.find_parent("block")
            block = parent if parent is not None else device
            removable_value = block.attributes.get("removable")
            removable = removable_value == b"1" if removable_value is not None else None
            return {
                "label": _udev_property(device, "ID_FS_LABEL")
                or _udev_property(device, "ID_FS_LABEL_ENC"),
                "model": _udev_property(block, "ID_MODEL")
                or _udev_property(block, "ID_VENDOR")
                or _udev_property(device, "ID_MODEL"),
                "uuid": _udev_property(device, "ID_FS_UUID"),
                "transport": _udev_property(block, "ID_BUS")
                or _udev_property(device, "ID_BUS"),
                "device_serial": _udev_property(device, "ID_SERIAL")
                or _udev_property(device, "ID_USB_SERIAL")
                or _udev_property(block, "ID_SERIAL")
                or _udev_property(block, "ID_USB_SERIAL"),
                "partition_uuid": _udev_property(device, "PARTUUID")
                or _udev_property(device, "ID_PART_ENTRY_UUID"),
                "hardware_path": _udev_property(device, "ID_PATH")
                or _udev_property(block, "ID_PATH"),
                "removable": removable,
            }
        except Exception:
            return {}

    @staticmethod
    def _lsblk_by_path() -> dict[str, dict[str, Any]]:
        command = [
            "lsblk",
            "-J",
            "-b",
            "-o",
            "PATH,LABEL,MODEL,SIZE,FSTYPE,RM,MOUNTPOINTS,UUID,TRAN,PARTUUID",
        ]
        try:
            completed = subprocess.run(
                command, check=True, capture_output=True, text=True, timeout=15
            )
            payload = json.loads(completed.stdout or "{}")
        except Exception:
            return {}
        devices = payload.get("blockdevices", []) if isinstance(payload, dict) else []
        mapping: dict[str, dict[str, Any]] = {}
        for item in _walk_lsblk(devices):
            path = item.get("path")
            if isinstance(path, str):
                mapping[path] = item
        return mapping


def _udev_property(device: Any, key: str) -> Any:
    properties = getattr(device, "properties", None)
    if properties is not None:
        return properties.get(key)
    getter = getattr(device, "get", None)
    if getter is not None:
        return getter(key)
    return None


def _walk_lsblk(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for item in items:
        found.append(item)
        children = item.get("children") or []
        if isinstance(children, list):
            found.extend(_walk_lsblk(children))
    return found


def _to_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_bool_or_none(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    try:
        return bool(int(value))
    except (TypeError, ValueError):
        return None
