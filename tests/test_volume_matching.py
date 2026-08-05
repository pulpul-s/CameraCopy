from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cameracopy2.models import CopyReport, VolumeInfo
from cameracopy2.services.format_service import FormatResult, FormatService
from cameracopy2.services.volume_service import VolumeService


def _volume(
    name: str,
    mount: Path,
    *,
    uuid: str | None = None,
    partition_uuid: str | None = None,
    device_path: str | None = None,
    size_bytes: int | None = 64_000,
    removable: bool | None = True,
    platform: str = "linux",
) -> VolumeInfo:
    return VolumeInfo(
        id=name,
        display_name=name,
        mount_path=mount,
        uuid=uuid,
        partition_uuid=partition_uuid,
        device_path=device_path,
        size_bytes=size_bytes,
        removable=removable,
        platform=platform,
    )


def test_strict_format_match_uses_fresh_volume_identity(tmp_path: Path) -> None:
    selected = _volume(
        "selected",
        tmp_path / "old",
        uuid="ABCD-1234",
        partition_uuid="{PART-1}",
        device_path="/dev/sdb1",
    )
    current = _volume(
        "current",
        tmp_path / "new",
        uuid="abcd-1234",
        partition_uuid="part-1",
        device_path="/dev/sdc1",
    )

    match = VolumeService().find_matching_format_target(selected, [current])

    assert match.ok
    assert match.volume is current


def test_strict_format_match_ignores_shared_partition_uuid(tmp_path: Path) -> None:
    selected = _volume(
        "selected",
        tmp_path / "first",
        uuid="6A6A-E415",
        partition_uuid="00000000-01",
        device_path="/dev/sdb1",
        size_bytes=74_500,
    )
    current = _volume(
        "current",
        tmp_path / "first",
        uuid="6A6A-E415",
        partition_uuid="00000000-01",
        device_path="/dev/sdb1",
        size_bytes=74_500,
    )
    other_card = _volume(
        "other",
        tmp_path / "second",
        uuid="FB7E-FAF6",
        partition_uuid="00000000-01",
        device_path="/dev/sdc1",
        size_bytes=119_100,
    )

    match = VolumeService().find_matching_format_target(
        selected, [other_card, current]
    )

    assert match.ok
    assert match.volume is current


def test_strict_format_match_uses_device_path_when_filesystem_uuid_is_duplicated(
    tmp_path: Path,
) -> None:
    selected = _volume(
        "selected", tmp_path / "first", uuid="same", device_path="/dev/sdb1"
    )
    current = _volume(
        "current", tmp_path / "first", uuid="same", device_path="/dev/sdb1"
    )
    clone = _volume(
        "clone", tmp_path / "second", uuid="same", device_path="/dev/sdc1"
    )

    match = VolumeService().find_matching_format_target(selected, [clone, current])

    assert match.ok
    assert match.volume is current


def test_strict_format_match_rejects_size_change(tmp_path: Path) -> None:
    selected = _volume("selected", tmp_path / "card", uuid="same", size_bytes=64_000)
    current = _volume("current", tmp_path / "card", uuid="same", size_bytes=128_000)

    match = VolumeService().find_matching_format_target(selected, [current])

    assert not match.ok
    assert match.reason == "changed"


def test_strict_format_match_requires_strong_identity(tmp_path: Path) -> None:
    match = VolumeService().find_matching_format_target(
        _volume("selected", tmp_path / "card", uuid=None, partition_uuid="00000000-01"), []
    )

    assert match.reason == "identity_unavailable"


def test_strict_format_match_rejects_ambiguous_identity(tmp_path: Path) -> None:
    selected = _volume("selected", tmp_path / "old", uuid="same")
    current = [
        _volume("one", tmp_path / "one", uuid="same"),
        _volume("two", tmp_path / "two", uuid="same"),
    ]

    match = VolumeService().find_matching_format_target(selected, current)

    assert match.reason == "ambiguous"


def test_strict_format_match_identifies_reused_path(tmp_path: Path) -> None:
    selected = _volume("selected", tmp_path / "card", uuid="original", device_path="/dev/sdb1")
    replacement = _volume(
        "replacement", tmp_path / "card", uuid="replacement", device_path="/dev/sdb1"
    )

    match = VolumeService().find_matching_format_target(selected, [replacement])

    assert match.reason == "changed"


def test_strict_format_match_reports_disconnected_card(tmp_path: Path) -> None:
    selected = _volume("selected", tmp_path / "card", uuid="original", device_path="/dev/sdb1")

    match = VolumeService().find_matching_format_target(selected, [])

    assert match.reason == "disconnected"


def test_strict_format_match_reports_enumeration_failure(tmp_path: Path) -> None:
    class BrokenVolumeService(VolumeService):
        def list_volumes(self) -> list[VolumeInfo]:
            raise RuntimeError("WMI failed")

    match = BrokenVolumeService().find_matching_format_target(
        _volume("selected", tmp_path / "card", uuid="original")
    )

    assert match.reason == "enumeration_failed"


def test_format_service_uses_freshly_resolved_target(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    selected = _volume("selected", tmp_path / "old", uuid="same", device_path="/dev/sdb1")
    current = _volume("current", tmp_path / "new", uuid="same", device_path="/dev/sdc1")

    class FakeVolumeService(VolumeService):
        def list_volumes(self) -> list[VolumeInfo]:
            return [current]

    service = FormatService(volume_service_factory=FakeVolumeService)
    service.available_filesystems = lambda: {"exFAT": None}  # type: ignore[method-assign]
    formatted: list[VolumeInfo] = []
    announced: list[VolumeInfo] = []
    service._format_linux = lambda target, filesystem: (  # type: ignore[method-assign]
        formatted.append(target) or FormatResult(True, "formatted")
    )
    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")

    result = service.format_volume(
        selected,
        "exFAT",
        CopyReport(started_at=datetime.now()),
        before_format=announced.append,
    )

    assert result.ok
    assert formatted == [current]
    assert announced == [current]


def test_format_service_rejects_changed_target_without_formatting(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    selected = _volume("selected", tmp_path / "card", uuid="original", device_path="/dev/sdb1")
    replacement = _volume(
        "replacement", tmp_path / "card", uuid="replacement", device_path="/dev/sdb1"
    )

    class FakeVolumeService(VolumeService):
        def list_volumes(self) -> list[VolumeInfo]:
            return [replacement]

    service = FormatService(volume_service_factory=FakeVolumeService)
    service.available_filesystems = lambda: {"exFAT": None}  # type: ignore[method-assign]
    service._format_linux = lambda *_args: (_ for _ in ()).throw(  # type: ignore[method-assign]
        AssertionError("format command must not run")
    )
    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")

    result = service.format_volume(selected, "exFAT", CopyReport(started_at=datetime.now()))

    assert not result.ok
    assert result.target_rejected
    assert result.rejection_reason == "changed"
    assert "No formatting command was run" in result.message


def test_whitespace_only_volume_keywords_do_not_hide_volumes(tmp_path: Path) -> None:
    volume = VolumeInfo(id="card", display_name="Camera card", mount_path=tmp_path)

    assert volume.matches_keywords([])
    assert volume.matches_keywords([" ", "\t", "\n"])


def test_refresh_ignores_partition_uuid_and_uses_current_attachment(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import find_same_volume_after_refresh

    selected = VolumeInfo(
        id="selected",
        display_name="Selected",
        mount_path=tmp_path / "selected",
        device_path="/dev/sdb1",
        uuid="OLD-UUID",
        partition_uuid="00000000-01",
        size_bytes=64_000,
    )
    same_attachment = VolumeInfo(
        id="refreshed",
        display_name="Refreshed",
        mount_path=tmp_path / "refreshed",
        device_path="/dev/sdb1",
        uuid="NEW-UUID",
        partition_uuid="different-value",
        size_bytes=64_000,
    )
    same_partition_metadata_only = VolumeInfo(
        id="other-card",
        display_name="Other card",
        mount_path=tmp_path / "other",
        device_path="/dev/sdc1",
        partition_uuid="00000000-01",
        size_bytes=64_000,
    )

    assert find_same_volume_after_refresh([same_attachment], selected) == "refreshed"
    assert find_same_volume_after_refresh([same_partition_metadata_only], selected) == ""


def test_refresh_does_not_follow_filesystem_uuid_to_a_new_attachment(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import find_same_volume_after_refresh

    selected = VolumeInfo(
        id="old",
        display_name="Old",
        mount_path=tmp_path / "old",
        device_path="/dev/sdb1",
        uuid="DUPLICATED",
        size_bytes=64_000,
    )
    replacement = VolumeInfo(
        id="new",
        display_name="New",
        mount_path=tmp_path / "new",
        device_path="/dev/sdc1",
        uuid="DUPLICATED",
        size_bytes=64_000,
    )

    assert find_same_volume_after_refresh([replacement], selected) == ""


def test_refresh_rejects_reused_attachment_when_hardware_metadata_conflicts(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import find_same_volume_after_refresh

    selected = VolumeInfo(
        id="same-id",
        display_name="Selected",
        mount_path=tmp_path / "card",
        device_path="/dev/sdb1",
        uuid="SAME-UUID",
        device_serial="CARD-A",
        hardware_path="reader-slot-a",
        size_bytes=64_000,
    )
    replacement = VolumeInfo(
        id="same-id",
        display_name="Replacement",
        mount_path=tmp_path / "card",
        device_path="/dev/sdb1",
        uuid="SAME-UUID",
        device_serial="CARD-B",
        hardware_path="reader-slot-b",
        size_bytes=64_000,
    )

    assert find_same_volume_after_refresh([replacement], selected) == ""
