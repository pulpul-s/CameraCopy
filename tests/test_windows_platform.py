from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from cameracopy2.models import VolumeInfo
from cameracopy2.platform.windows import (
    WindowsVolumeService,
    _removable_from_drive_type,
)
from cameracopy2.services.format_service import FormatResult, FormatService


def test_windows_physical_lookup_reuses_existing_logical_disk_map() -> None:
    class BrokenLogical:
        def associators(self, **_kwargs):  # noqa: ANN201
            raise RuntimeError("broken association")

    good_disk = SimpleNamespace(
        Model="Reader",
        Caption="Reader",
        SerialNumber="SER",
        PNPDeviceID="PNP",
    )

    class GoodPartition:
        def associators(self, **_kwargs):  # noqa: ANN201
            return [good_disk]

    class GoodLogical:
        def associators(self, **_kwargs):  # noqa: ANN201
            return [GoodPartition()]

    details = WindowsVolumeService._physical_details_by_logical_disk(  # noqa: SLF001
        {"E:": BrokenLogical(), "F:": GoodLogical()}
    )

    assert details["E:"].model is None
    assert details["F:"].model == "Reader"
    assert details["F:"].device_serial == "SER"


def test_windows_volume_scan_queries_logical_disks_once(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform import windows as windows_module

    query_count = 0
    disk = SimpleNamespace(
        Model="Sony DSC USB Device",
        Caption="Sony DSC USB Device",
        SerialNumber="SER",
        PNPDeviceID="PNP",
    )

    class Partition:
        def associators(self, **_kwargs):  # noqa: ANN201
            return [disk]

    class Logical:
        DeviceID = "E:"
        VolumeName = None
        Description = "Removable Disk"
        VolumeSerialNumber = "A1B2-C3D4"
        Size = str(64 * 1024**3)
        FileSystem = "exFAT"
        DriveType = 2

        def associators(self, **_kwargs):  # noqa: ANN201
            return [Partition()]

    class Client:
        def Win32_LogicalDisk(self):  # noqa: ANN201, N802
            nonlocal query_count
            query_count += 1
            return [Logical()]

    monkeypatch.setattr(WindowsVolumeService, "_wmi_client", staticmethod(Client))
    monkeypatch.setattr(
        windows_module.psutil,
        "disk_partitions",
        lambda all=False: [  # noqa: ARG005
            SimpleNamespace(device="E:\\", mountpoint="E:\\", fstype="exFAT")
        ],
    )

    volumes = WindowsVolumeService().list_volumes()

    assert query_count == 1
    assert len(volumes) == 1
    assert volumes[0].model == "Sony DSC USB Device"
    assert volumes[0].partition_uuid is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2, True), ("2", True), (3, False), (None, None), ("bad", None), (object(), None)],
)
def test_windows_drive_type_parsing_is_safe(value: object, expected: bool | None) -> None:
    assert _removable_from_drive_type(value) is expected


def test_windows_com_context_initializes_and_releases(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform import windows_com

    events: list[str] = []
    fake_pythoncom = SimpleNamespace(
        CoInitialize=lambda: events.append("initialize"),
        CoUninitialize=lambda: events.append("uninitialize"),
    )
    monkeypatch.setattr(windows_com.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)

    with windows_com.windows_com_initialized():
        events.append("work")

    assert events == ["initialize", "work", "uninitialize"]


def test_windows_com_context_releases_after_exception(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform import windows_com

    events: list[str] = []
    fake_pythoncom = SimpleNamespace(
        CoInitialize=lambda: events.append("initialize"),
        CoUninitialize=lambda: events.append("uninitialize"),
    )
    monkeypatch.setattr(windows_com.sys, "platform", "win32")
    monkeypatch.setitem(sys.modules, "pythoncom", fake_pythoncom)

    with pytest.raises(RuntimeError, match="boom"):
        with windows_com.windows_com_initialized():
            raise RuntimeError("boom")

    assert events == ["initialize", "uninitialize"]


def test_windows_format_command_has_no_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = FormatService()
    captured: dict[str, object] = {}
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(service, "_windows_shell", lambda: "powershell.exe")
    monkeypatch.setattr(service, "_windows_wmi_available", lambda: True)

    def fake_run(command, success_message, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return FormatResult(True, success_message)

    monkeypatch.setattr(service, "_run_command", fake_run)
    volume = VolumeInfo(
        id="E",
        display_name="E",
        mount_path=Path("E:\\"),
        uuid="uuid",
        device_path="E:\\",
        removable=True,
        platform="windows",
    )

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert result.ok
    assert "timeout_seconds" not in captured
