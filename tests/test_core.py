from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Event

from cameracopy2.config import (
    ConfigReadError,
    UnsupportedConfigError,
    config_from_dict,
    default_config_path,
    load_config,
    translate_datetime_format,
)
from cameracopy2.core.metadata_diagnostics import diagnose_files, format_diagnostics_report
from cameracopy2.core.copy_engine import CopyEngine, _source_stat_changed
from cameracopy2.core.copy_messages import result_message
from cameracopy2.core.hash import compare_sha256_cancellable
from cameracopy2.core.naming import build_destination_directory
from cameracopy2.core.rating_reader import read_sidecar_rating_with_source
from cameracopy2.core.scanner import find_candidate_files
from cameracopy2.core.timestamp_resolver import read_sony_xml_creation_date, sony_xml_candidate
from cameracopy2.models import (
    CameraCopyConfig,
    CloneMismatchResponse,
    CopyCallbacks,
    CopyJob,
    CopyReport,
    DEFAULT_INCLUDE_PATTERNS,
    FileCopyResult,
    VolumeInfo,
    VolumeMatch,
)
from cameracopy2.operation_log import CopyOperationLog, operation_log_directory
from cameracopy2.services.format_service import FormatResult, FormatService
from cameracopy2.services.linux_udisks import LinuxUDisksClient, UDisksCallResult
from cameracopy2.services.volume_service import (
    VolumeService,
    volumes_refer_to_same_mounted_volume,
)


def make_volume(path: Path) -> VolumeInfo:
    return VolumeInfo(id=str(path), display_name="Test card", mount_path=path, platform="test")


def test_source_change_detection_uses_size_and_modification_time() -> None:
    class Stat:
        def __init__(
            self,
            *,
            size: int = 5,
            modified_ns: int = 10,
            changed_ns: int = 20,
            inode: int = 100,
        ) -> None:
            self.st_size = size
            self.st_mtime_ns = modified_ns
            self.st_ctime_ns = changed_ns
            self.st_ino = inode

    assert not _source_stat_changed(Stat(), Stat(changed_ns=21, inode=200))  # type: ignore[arg-type]
    assert _source_stat_changed(Stat(), Stat(size=6))  # type: ignore[arg-type]
    assert _source_stat_changed(Stat(), Stat(modified_ns=11))  # type: ignore[arg-type]


def test_config_from_dict_requires_current_version() -> None:
    try:
        config_from_dict({"minrating": "3", "autoformat": "exfat"})
    except UnsupportedConfigError:
        pass
    else:
        raise AssertionError("old config without version should be unsupported")


def test_translate_datetime_format() -> None:
    assert translate_datetime_format("yyyy-MM-dd_HH-mm-ss") == "%Y-%m-%d_%H-%M-%S"


def test_destination_directory(tmp_path: Path) -> None:
    config = CameraCopyConfig(
        source="",
        destination=str(tmp_path),
        folderprefix="pre_",
        datetimestring="yyyy-MM-dd",
        folderpostfix="_post",
    )
    directory = build_destination_directory(config, datetime(2026, 5, 9, 10, 30))
    assert directory.name == "pre_2026-05-09_post"


def test_scanner_include_exclude(tmp_path: Path) -> None:
    (tmp_path / "DSC0001.ARW").write_text("raw")
    (tmp_path / "MEDIAPRO.XML").write_text("xml")
    (tmp_path / "C0001.MP4").write_text("mp4")
    files = find_candidate_files(tmp_path, ["DSC*.ARW", "C*.MP4"], ["MEDIAPRO.XML"])
    assert {file.name for file in files} == {"DSC0001.ARW", "C0001.MP4"}


def test_xmp_rating_sidecar(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    media.write_text("raw")
    (tmp_path / "DSC0001.xmp").write_text('<rdf:Description xmp:Rating="4" />')
    lookup = read_sidecar_rating_with_source(media)
    assert lookup is not None
    assert lookup.rating == 4


def test_sony_xml_timestamp(tmp_path: Path) -> None:
    mp4 = tmp_path / "C0001.MP4"
    xml = sony_xml_candidate(mp4)
    xml.write_text('<CreationDate value="2024-01-02T03:04:05+02:00" />')
    timestamp = read_sony_xml_creation_date(xml)
    assert timestamp is not None
    assert timestamp.year == 2024
    assert timestamp.month == 1
    assert timestamp.day == 2


def test_hash_compare(tmp_path: Path) -> None:
    source = tmp_path / "a.bin"
    dest = tmp_path / "b.bin"
    source.write_bytes(b"abc")
    dest.write_bytes(b"abc")
    assert compare_sha256_cancellable(source, dest, None).ok


def test_copy_engine_uses_temp_file_and_removes_no_temp_left(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")
    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring=""
    )
    job = CopyJob(primary=make_volume(card), secondary=None, config=config)

    report = CopyEngine().run(job)

    assert not report.has_failures
    assert (dest / "DSC0001.JPG").read_bytes() == b"image"
    assert not list(dest.glob("*.cameracopy.tmp"))


def test_overwrite_with_autoremove_removes_source(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    source = card / "DSC0001.JPG"
    destination = dest / "DSC0001.JPG"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        collisionpolicy="overwrite",
        checkhash=True,
    )
    job = CopyJob(primary=make_volume(card), secondary=None, config=config, autoremove=True)

    report = CopyEngine().run(job)

    assert not report.has_failures
    assert report.removed_count == 1
    assert destination.read_bytes() == b"new"
    assert not source.exists()


def test_clone_mode_removes_matching_secondary_existing_file(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    secondary_source = secondary / "DSC0001.JPG"
    secondary_source.write_bytes(b"image")
    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    job = CopyJob(
        primary=make_volume(primary),
        secondary=make_volume(secondary),
        config=config,
        clone_mode=True,
        autoremove=True,
    )

    report = CopyEngine().run(job)

    assert not report.has_failures
    assert (dest / "DSC0001.JPG").read_bytes() == b"image"
    assert not secondary_source.exists()
    assert report.removed_count == 2


def test_format_service_blocks_failed_report(tmp_path: Path) -> None:
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(tmp_path / "missing"),
            secondary=None,
            config=CameraCopyConfig(source="", destination=str(tmp_path), includedfiles=["*.JPG"]),
        )
    )
    result = FormatService().format_volume(make_volume(tmp_path), "exFAT", report)
    assert not result.ok
    assert "blocked" in result.message.lower()


def test_format_target_description_includes_specific_volume_details(tmp_path: Path) -> None:
    volume = VolumeInfo(
        id="camera",
        display_name="Sony DSC - exfat - usb - 119.1 GB",
        mount_path=tmp_path / "A9F1-F451",
        device_path="/dev/sdf1",
        label=None,
        filesystem="exfat",
        size_bytes=127_848_677_376,
    )

    description = FormatService.format_target_description(volume, "exFAT")

    assert description.startswith("Sony DSC - exfat - usb - 119.1 GB as exFAT")
    assert f"mount: {tmp_path / 'A9F1-F451'}" in description
    assert "device: /dev/sdf1" in description
    assert "current label: unlabeled" in description
    assert "current filesystem: exfat" in description
    assert "size: 119.1 GB" in description


def test_rename_collision_policy_creates_numbered_destination(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"new")
    (dest / "DSC0001.JPG").write_bytes(b"old")
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        collisionpolicy="rename",
        checkhash=True,
    )

    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    assert not report.has_failures
    assert (dest / "DSC0001.JPG").read_bytes() == b"old"
    assert (dest / "DSC0001_001.JPG").read_bytes() == b"new"


def test_byte_progress_callbacks_are_emitted(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    payload = b"x" * (1024 * 1024 + 7)
    (card / "DSC0001.JPG").write_bytes(payload)
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    seen: list[tuple[int, int, int]] = []

    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config),
        CopyCallbacks(byte_progress=lambda done, total, index: seen.append((done, total, index))),
    )

    assert not report.has_failures
    assert seen
    assert seen[-1] == (len(payload), len(payload), 1)


def test_cancel_during_large_file_cleans_temp(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"x" * (3 * 1024 * 1024))
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )

    from threading import Event

    cancel_event = Event()

    def cancel_after_first_chunk(done: int, _total: int, _index: int) -> None:
        if done >= 1024 * 1024:
            cancel_event.set()

    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config),
        CopyCallbacks(byte_progress=cancel_after_first_chunk),
        cancel_event,
    )

    assert report.cancelled
    assert not (dest / "DSC0001.JPG").exists()
    assert not list(dest.glob("*.cameracopy.tmp"))


def test_malformed_xmp_rating_returns_none(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    media.write_text("raw")
    (tmp_path / "DSC0001.xmp").write_text('<rdf:Description xmp:Rating="not-a-number">')
    assert read_sidecar_rating_with_source(media) is None


def test_malformed_sony_xml_timestamp_returns_none(tmp_path: Path) -> None:
    mp4 = tmp_path / "C0001.MP4"
    xml = sony_xml_candidate(mp4)
    xml.write_text('<CreationDate value="not-a-date" />')
    assert read_sony_xml_creation_date(xml) is None


def test_missing_source_is_reported_as_failure(tmp_path: Path) -> None:
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(tmp_path / "missing-card"),
            secondary=None,
            config=CameraCopyConfig(
                source="", destination=str(tmp_path / "pictures"), includedfiles=["*.JPG"]
            ),
        )
    )

    assert report.has_failures
    assert not report.completed_cleanly
    assert report.failures[0].reason == "volume_unavailable"


def test_existing_temp_like_file_is_not_removed(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")
    preexisting_temp = dest / ".DSC0001.JPG.cameracopy.tmp"
    preexisting_temp.write_bytes(b"do not delete")
    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring=""
    )

    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    assert not report.has_failures
    assert preexisting_temp.read_bytes() == b"do not delete"
    assert (dest / "DSC0001.JPG").read_bytes() == b"image"


def test_clean_report_allows_format_gate(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")
    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring=""
    )

    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    assert report.completed_cleanly


def test_unavailable_volume_is_reported_before_source_scan(tmp_path: Path) -> None:
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(tmp_path / "missing-card"),
            secondary=None,
            config=CameraCopyConfig(
                source="", destination=str(tmp_path / "pictures"), includedfiles=["*.JPG"]
            ),
        )
    )

    assert report.has_failures
    assert report.failures[0].reason == "volume_unavailable"


def test_report_tracks_metadata_sources(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    media = card / "DSC0001.JPG"
    media.write_bytes(b"image")
    (card / "DSC0001.xmp").write_text('<rdf:Description xmp:Rating="4" />')
    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", minrating=3
    )

    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    assert not report.has_failures
    result = report.results[0]
    assert result.rating == 4
    assert result.rating_source == "xmp_sidecar:DSC0001.xmp"
    assert result.timestamp_source == "file_mtime"
    assert report.rating_source_counts == {"xmp_sidecar": 1}
    assert "Ratings:" in report.summary_lines()
    assert "  XMP sidecar: 1 file" in report.summary_lines()


def test_volume_keyword_matching_uses_uuid_and_transport(tmp_path: Path) -> None:
    volume = VolumeInfo(
        id="test",
        display_name="Camera card",
        mount_path=tmp_path,
        uuid="ABC-123",
        transport="usb",
        platform="test",
    )

    assert volume.matches_keywords(["abc-123"])
    assert volume.matches_keywords(["USB"])


def test_exiftool_batch_cache_handles_grouped_json(tmp_path: Path, monkeypatch) -> None:
    from subprocess import CompletedProcess

    from cameracopy2.services.metadata_service import ExifToolService

    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")

    def fake_run(command, **_kwargs):
        payload = [
            {
                "SourceFile": str(media),
                "XMP:Rating": 5,
                "EXIF:DateTimeOriginal": "2024:01:02 03:04:05",
            }
        ]
        return CompletedProcess(command, 0, stdout=__import__("json").dumps(payload), stderr="")

    service = ExifToolService()
    service.available = True
    service.executable = "exiftool"
    monkeypatch.setattr("cameracopy2.services.metadata_service.subprocess.run", fake_run)

    service.warm_cache([media])

    assert service.read_rating(media) == 5
    timestamp = service.read_capture_timestamp(media)
    assert timestamp is not None
    assert timestamp.year == 2024


def test_missing_source_subfolder_is_reported_after_volume_check(tmp_path: Path) -> None:
    card = tmp_path / "card"
    card.mkdir()
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(card),
            secondary=None,
            config=CameraCopyConfig(
                source="DCIM", destination=str(tmp_path / "pictures"), includedfiles=["*.JPG"]
            ),
        )
    )

    assert report.has_failures
    assert report.failures[0].reason == "source_missing"


def test_absolute_source_text_is_joined_under_selected_volume(tmp_path: Path) -> None:
    card = tmp_path / "card"
    source_dir = card / "olli" / "test"
    source_dir.mkdir(parents=True)
    (source_dir / "DSC0001.JPG").write_bytes(b"image")
    destination = tmp_path / "pictures"

    config = CameraCopyConfig(
        source="/olli/test",
        destination=str(destination),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    assert not report.has_failures
    assert (destination / "DSC0001.JPG").exists()
    assert "Scanning /olli/test" not in "\n".join(report.logs)


def test_scanner_uses_natural_filename_sort(tmp_path: Path) -> None:
    for name in ["image1.jpg", "image10.jpg", "image2.jpg"]:
        (tmp_path / name).write_bytes(b"x")

    files = find_candidate_files(tmp_path, ["*.jpg"])

    assert [path.name for path in files] == ["image1.jpg", "image2.jpg", "image10.jpg"]


def test_summary_counts_copied_files_even_when_sources_are_removed(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"image")
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )

    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config, autoremove=True)
    )

    assert not report.has_failures
    assert report.copied_count == 1
    assert report.removed_count == 1
    assert report.bytes_copied == len(b"image")
    assert "1 copied" in "\n".join(report.summary_lines())


def test_removed_after_copy_logs_as_copied_plus_removed(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )

    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config, autoremove=True)
    )

    log_text = "\n".join(report.logs)
    assert "COPIED + REMOVED:" in log_text
    assert not any(line.startswith("REMOVED:") for line in report.logs)
    assert "source files removed" in log_text


def test_removed_source_marker_is_shown_for_all_removed_success_modes(tmp_path: Path) -> None:
    source = tmp_path / "source.ARW"
    destination = tmp_path / "destination.ARW"
    modes_and_labels = {
        "copied": "COPIED + REMOVED",
        "overwrote": "REPLACED + REMOVED",
        "renamed_copy": "RENAMED COPY + REMOVED",
        "verified_existing": "VERIFIED EXISTING + REMOVED",
        "clone_verified": "CLONE VERIFIED + REMOVED",
        "clone_mismatch_kept": "CLONE MISMATCH — KEPT BOTH + REMOVED",
        "clone_mismatch_replaced": "CLONE MISMATCH — USED SECOND-SOURCE + REMOVED",
    }

    for copy_mode, expected_label in modes_and_labels.items():
        result = FileCopyResult(
            source=source,
            destination=destination,
            action="removed",
            size_bytes=123,
            copy_mode=copy_mode,  # type: ignore[arg-type]
        )

        assert result_message(result).startswith(f"{expected_label}:")  # noqa: SLF001


def test_skipped_results_do_not_show_removed_marker(tmp_path: Path) -> None:
    result = FileCopyResult(
        source=tmp_path / "source.ARW",
        destination=tmp_path / "destination.ARW",
        action="skipped",
        size_bytes=123,
        copy_mode="clone_mismatch_skipped",
    )

    assert result_message(result).startswith("CLONE MISMATCH — KEPT FIRST-SOURCE:")  # noqa: SLF001


def test_default_config_path_uses_standard_config_dir() -> None:
    path = default_config_path()
    assert path.name == "cameracopy.json"
    assert "CameraCopy" in str(path)


def test_windows_config_path_uses_roaming_appdata(monkeypatch, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2 import config as config_module

    appdata = tmp_path / "Roaming"
    monkeypatch.setattr(config_module.sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(appdata))

    path = config_module.default_config_path()

    assert path == appdata / "CameraCopy" / "cameracopy.json"


def test_unsupported_config_is_renamed_to_backup_and_replaced(tmp_path: Path) -> None:
    config_path = tmp_path / "cameracopy.json"
    config_path.write_text('{"destination": "/tmp/pictures"}', encoding="utf-8")

    config = load_config(config_path)

    assert config.version == 2
    assert (tmp_path / "cameracopy.json.bak").exists()
    assert config_path.exists()
    assert '"version": 2' in config_path.read_text(encoding="utf-8")


def test_missing_config_is_created(tmp_path: Path) -> None:
    config_path = tmp_path / "cameracopy.json"

    config = load_config(config_path)

    assert config.version == 2
    assert config_path.exists()


def test_defaults_are_common_camera_workflow() -> None:
    config = CameraCopyConfig()
    assert config.source == "DCIM"
    assert config.collisionpolicy == "ask"
    assert config.includedfiles == DEFAULT_INCLUDE_PATTERNS
    assert config.excludedfiles == []
    assert config.includeddevices == []
    assert config.useembeddedmetadata is False
    assert config.folderprefix == ""
    assert config.folderpostfix == ""


def test_config_loads_volume_identity_defaults() -> None:
    config = config_from_dict(
        {
            "version": 2,
            "defaultprimaryvolumeid": "uuid:primary",
            "defaultsecondaryvolumeid": "uuid:secondary",
        }
    )
    assert config.defaultprimaryvolumeid == "uuid:primary"
    assert config.defaultsecondaryvolumeid == "uuid:secondary"


def test_first_and_second_volumes_resolve_to_different_source_roots() -> None:
    from cameracopy2.core.source_paths import resolve_source_root

    first = VolumeInfo(id="first", display_name="First", mount_path=Path("/first"), platform="test")
    second = VolumeInfo(
        id="second", display_name="Second", mount_path=Path("/second"), platform="test"
    )

    assert resolve_source_root(first, "DCIM") == Path("/first/DCIM")
    assert resolve_source_root(second, "DCIM") == Path("/second/DCIM")


def test_dual_volume_logs_blank_line_between_scans(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    dest = tmp_path / "pictures"
    first.mkdir()
    second.mkdir()
    dest.mkdir()
    (first / "image1.jpg").write_bytes(b"one")
    (second / "image2.jpg").write_bytes(b"two")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.jpg"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
        )
    )

    assert not report.has_failures
    scan_lines = [index for index, line in enumerate(report.logs) if line.startswith("Scanning ")]
    assert len(scan_lines) == 2
    assert report.logs[scan_lines[1] - 1] == ""


def test_source_resolution_never_invents_home_path() -> None:
    from cameracopy2.core.source_paths import resolve_source_root

    root_volume = VolumeInfo(id="root", display_name="Root", mount_path=Path("/"), platform="test")
    home_volume = VolumeInfo(
        id="home", display_name="Home", mount_path=Path("/home"), platform="test"
    )
    external_volume = VolumeInfo(
        id="external", display_name="External", mount_path=Path("/var/tmp"), platform="test"
    )

    assert resolve_source_root(root_volume, "olli/test") == Path("/olli/test")
    assert resolve_source_root(home_volume, "olli/test") == Path("/home/olli/test")
    assert resolve_source_root(external_volume, "olli/test") == Path("/var/tmp/olli/test")


def test_dual_volume_source_resolution_is_strictly_mount_relative(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "pictures"
    (first / "olli" / "test").mkdir(parents=True)
    (second / "olli" / "test").mkdir(parents=True)
    destination.mkdir()
    (first / "olli" / "test" / "image1.jpg").write_bytes(b"first")
    (second / "olli" / "test" / "image2.jpg").write_bytes(b"second")

    config = CameraCopyConfig(
        source="olli/test",
        destination=str(destination),
        includedfiles=["*.jpg"],
        datetimestring="",
        checkhash=False,
        collisionpolicy="rename",
    )
    report = CopyEngine().run(
        CopyJob(primary=make_volume(first), secondary=make_volume(second), config=config)
    )

    joined_logs = "\n".join(report.logs)
    assert not report.has_failures
    assert f"Scanning {first / 'olli' / 'test'}" in joined_logs
    assert f"Scanning {second / 'olli' / 'test'}" in joined_logs


def test_config_with_unknown_field_is_unsupported() -> None:
    try:
        config_from_dict({"version": 2, "overwrite": True})
    except UnsupportedConfigError:
        pass
    else:
        raise AssertionError("unknown old field should be unsupported")


def test_windows_format_refuses_system_drive(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = FormatService()
    monkeypatch.setenv("SystemDrive", "C:")
    volume = VolumeInfo(id="c", display_name="C", mount_path=Path("C:\\"), platform="windows")

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert "system drive" in result.message


def test_windows_format_command_uses_powershell_force(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = FormatService()
    captured: dict[str, list[str]] = {}

    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(service, "_windows_shell", lambda: "powershell.exe")
    monkeypatch.setattr(service, "_windows_wmi_available", lambda: True)

    def fake_run_command(
        command: list[str], success_message: str, allow_not_mounted: bool = False, **_kwargs
    ):  # type: ignore[no-untyped-def]
        captured["command"] = command
        return type("FakeResult", (), {"ok": True, "message": success_message})()

    monkeypatch.setattr(service, "_run_command", fake_run_command)
    volume = VolumeInfo(
        id="e",
        display_name="E",
        mount_path=Path("E:\\"),
        removable=True,
        platform="windows",
    )

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert result.ok
    assert captured["command"][0] == "powershell.exe"
    assert "-ExecutionPolicy" in captured["command"]
    assert "-Force" in captured["command"][-1]
    assert "Format-Volume -DriveLetter E" in captured["command"][-1]


def test_windows_format_command_preserves_label_and_escapes_quotes() -> None:
    command = FormatService._windows_format_command("E", "exFAT", "Olli's Card")  # noqa: SLF001

    assert "-NewFileSystemLabel 'Olli''s Card'" in command
    assert "-ErrorAction Stop" in command
    assert "__CAMERACOPY_ELEVATION_REQUIRED__" in command
    assert "PermissionDenied" in command
    assert command.startswith("try {")
    assert command.endswith("exit 1 }")


def test_summary_includes_copy_time_without_folder_date_noise(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    summary = "\n".join(report.summary_lines())
    assert "Copy time:" in summary
    assert "Elapsed time:" not in summary
    assert "Folder dates:" not in summary
    assert "Date sources:" not in summary




def test_summary_includes_average_write_speed() -> None:
    report = CopyReport(
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        finished_at=datetime(2026, 1, 1, 12, 0, 2),
        results=[
            FileCopyResult(
                source=Path("source.ARW"),
                destination=Path("dest.ARW"),
                action="copied",
                copy_mode="copied",
                bytes_copied=20 * 1024 * 1024,
            )
        ],
    )

    summary = "\n".join(report.summary_lines())

    assert "Average write speed: 10.0 MB/s" in summary


def test_summary_includes_sha256_failure_count() -> None:
    report = CopyReport(
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        finished_at=datetime(2026, 1, 1, 12, 0, 1),
        results=[
            FileCopyResult(
                source=Path("source.ARW"),
                destination=Path("dest.ARW"),
                action="failed",
                hash_ok=False,
                reason="SHA256 FAILED",
            )
        ],
    )

    summary = "\n".join(report.summary_lines())

    assert "SHA256 failures: 1" in summary


def test_metadata_diagnostics_reports_candidates_and_exiftool_metadata(tmp_path: Path) -> None:
    class FakeExifTool:
        def warm_cache(self, paths):  # noqa: ANN001
            self.paths = paths

        def read_capture_timestamp(self, path):  # noqa: ANN001
            return datetime(2026, 5, 11, 13, 0, 0)

        def read_all_json(self, path):  # noqa: ANN001
            return {
                "EXIF:DateTimeOriginal": "2026:05:11 13:00:00",
                "File:ImageWidth": 6000,
            }

    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    (tmp_path / "DSC0001.xmp").write_text(
        '<rdf:Description xmp:CreateDate="2026-05-10T12:00:00" />'
    )

    diagnostics = diagnose_files(
        [media], CameraCopyConfig(useembeddedmetadata=True), FakeExifTool()
    )
    report = format_diagnostics_report(diagnostics)

    assert "File modified time:" in report
    assert "Filesystem created time:" in report
    assert "Sony XML timestamp:" in report
    assert "Embedded metadata time:" in report
    assert "XMP sidecar time:" in report
    assert "Chosen folder date:" in report
    assert "Chosen source:            embedded metadata" in report
    assert "ExifTool metadata:" in report
    assert "  EXIF:DateTimeOriginal: 2026:05:11 13:00:00" in report
    assert "  File:ImageWidth: 6000" in report


def test_metadata_diagnostics_default_copy_policy_prefers_sidecar_over_embedded(
    tmp_path: Path,
) -> None:
    class FakeExifTool:
        def warm_cache(self, paths):  # noqa: ANN001
            pass

        def read_capture_timestamp(self, path):  # noqa: ANN001
            return datetime(2026, 5, 11, 13, 0, 0)

        def read_all_json(self, path):  # noqa: ANN001
            return {"EXIF:DateTimeOriginal": "2026:05:11 13:00:00"}

    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    (tmp_path / "DSC0001.xmp").write_text(
        '<rdf:Description xmp:CreateDate="2026-05-10T12:00:00" />'
    )

    diagnostics = diagnose_files(
        [media], CameraCopyConfig(useembeddedmetadata=False), FakeExifTool()
    )
    report = format_diagnostics_report(diagnostics)

    assert "Chosen source:            XMP sidecar" in report


def test_copy_engine_does_not_warm_exiftool_when_embedded_metadata_disabled(tmp_path: Path) -> None:
    class FakeTimestampResolver:
        def __init__(self) -> None:
            self.warmed = False

        def warm_cache(self, paths):  # noqa: ANN001
            self.warmed = True

        def resolve(
            self,
            path,
            fix_sony_timestamps=True,
            use_embedded_metadata=False,
            sidecars=None,
        ):  # noqa: ANN001
            assert use_embedded_metadata is False
            return datetime.fromtimestamp(Path(path).stat().st_mtime), "file_mtime"

    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")

    resolver = FakeTimestampResolver()
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        checkhash=False,
        useembeddedmetadata=False,
    )
    report = CopyEngine(timestamp_resolver=resolver).run(
        CopyJob(primary=make_volume(card), secondary=None, config=config)
    )

    assert not report.has_failures
    assert resolver.warmed is False


def test_existing_file_prompt_skip_keeps_summary_clean(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    source = card / "DSC0001.JPG"
    destination = dest / "DSC0001.JPG"
    source.write_bytes(b"new")
    destination.write_bytes(b"old")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
        collisionpolicy="ask",
    )
    callbacks = CopyCallbacks(collision_decision=lambda source, destination: "skip")
    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config), callbacks
    )

    summary = "\n".join(report.summary_lines())
    assert "Copy time:" in summary
    assert report.skipped_count == 1


def test_config_theme_defaults_to_system() -> None:
    config = CameraCopyConfig()
    assert config.theme == "system"


def test_config_from_dict_reads_theme() -> None:
    config = config_from_dict({"version": 2, "theme": "dark"})
    assert config.theme == "dark"


def test_removed_clone_skip_setting_is_not_part_of_config() -> None:
    config = config_from_dict({"version": 2})
    assert not hasattr(config, "verifycloneskippedfiles")


def test_legacy_clone_skip_setting_is_accepted_and_ignored() -> None:
    config = config_from_dict({"version": 2, "verifycloneskippedfiles": True})
    assert not hasattr(config, "verifycloneskippedfiles")


def test_config_rejects_unknown_theme() -> None:
    try:
        config_from_dict({"version": 2, "theme": "blue"})
    except UnsupportedConfigError:
        pass
    else:
        raise AssertionError("unknown theme should be unsupported")


def test_clone_mode_uses_sha256_even_when_normal_verification_disabled(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "DSC0001.JPG").write_bytes(b"image")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert any(result.copy_mode == "clone_verified" for result in report.results)
    assert "Clone verification: SHA256" in "\n".join(report.logs)


def test_clone_summary_labels_verified_second_source_as_verified_clones(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "DSC0001.JPG").write_bytes(b"image")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        )
    )

    summary = "\n".join(report.summary_lines())
    assert "1 verified clones" in summary
    assert "verified existing" not in summary


def test_clone_mode_logs_phase_after_each_scan(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "DSC0001.JPG").write_bytes(b"image")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        )
    )

    log_text = "\n".join(report.logs)
    assert "Starting first-source file copy..." in log_text
    assert "Starting clone verification..." in log_text


def test_normal_two_source_copy_logs_first_and_second_source_phases(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"primary")
    (secondary / "DSC0002.JPG").write_bytes(b"secondary")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=False,
        )
    )

    log_text = "\n".join(report.logs)
    assert "Starting first-source file copy..." in log_text
    assert "Starting second-source file copy..." in log_text
    assert "Starting clone verification..." not in log_text


def test_single_source_copy_logs_file_copy_phase(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    log_text = "\n".join(report.logs)
    assert "Starting file copy..." in log_text
    assert "Starting first-source file copy..." not in log_text


def test_clone_skip_existing_excludes_file_from_clone_verification(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"first-source-version")
    (secondary / "DSC0001.JPG").write_bytes(b"second-source-version")
    (dest / "DSC0001.JPG").write_bytes(b"existing-destination-version")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        collisionpolicy="ask",
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(collision_decision=lambda source, destination: "skip"),
    )

    assert not report.has_failures
    assert (dest / "DSC0001.JPG").read_bytes() == b"existing-destination-version"
    assert report.skipped_count == 1
    assert not any(result.copy_mode == "clone_verified" for result in report.results)
    log_text = "\n".join(report.logs)
    assert "No first-source files are available for clone verification." in log_text
    assert "first-source copy was not available for clone verification" not in log_text
    assert "destination does not match first source" not in log_text


def test_clone_skip_existing_does_not_hash_destination(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.core import copy_engine as copy_module

    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    for root in (primary, secondary):
        (root / "DSC0001.JPG").write_bytes(b"image")
    (dest / "DSC0001.JPG").write_bytes(b"different")

    calls: list[tuple[Path, Path]] = []
    real_compare = copy_module.compare_sha256_cancellable

    def tracked_compare(source: Path, destination: Path, *args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        calls.append((source, destination))
        return real_compare(source, destination, *args, **kwargs)

    monkeypatch.setattr(copy_module, "compare_sha256_cancellable", tracked_compare)
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        collisionpolicy="ask",
    )
    report = CopyEngine().run(
        CopyJob(make_volume(primary), make_volume(secondary), config, clone_mode=True),
        CopyCallbacks(collision_decision=lambda *_: "skip"),
    )

    assert not report.has_failures
    assert calls == []


def test_clone_mismatch_keep_both_copies_second_source_version(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"first")
    (secondary / "DSC0001.JPG").write_bytes(b"second")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    callbacks = CopyCallbacks(
        clone_mismatch_decision=lambda source, destination, allow_remove: CloneMismatchResponse(
            decision="keep_both",
            remove_source=False,
        )
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        callbacks,
    )

    assert not report.has_failures
    kept = dest / "DSC0001_001.JPG"
    assert kept.read_bytes() == b"second"
    assert (dest / "DSC0001.JPG").read_bytes() == b"first"
    assert "CLONE MISMATCH — KEPT BOTH" in "\n".join(report.logs)


def test_clone_mismatch_replace_uses_second_source_version(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"first")
    (secondary / "DSC0001.JPG").write_bytes(b"second")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    callbacks = CopyCallbacks(
        clone_mismatch_decision=lambda source, destination, allow_remove: CloneMismatchResponse(
            decision="replace",
            remove_source=False,
        )
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        callbacks,
    )

    assert not report.has_failures
    assert (dest / "DSC0001.JPG").read_bytes() == b"second"
    assert "CLONE MISMATCH — USED SECOND-SOURCE" in "\n".join(report.logs)


def test_clone_mismatch_remove_checkbox_response_removes_second_source_after_copy(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"first")
    second_source = secondary / "DSC0001.JPG"
    second_source.write_bytes(b"second")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    callbacks = CopyCallbacks(
        clone_mismatch_decision=lambda source, destination, allow_remove: CloneMismatchResponse(
            decision="keep_both",
            remove_source=allow_remove,
        )
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
            autoremove=True,
        ),
        callbacks,
    )

    assert not report.has_failures
    assert not second_source.exists()
    assert "CLONE MISMATCH — KEPT BOTH + REMOVED" in "\n".join(report.logs)


def test_clone_second_source_extra_file_prompts_and_can_be_skipped(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "DSC0001.JPG").write_bytes(b"image")
    extra = secondary / "EXTRA.JPG"
    extra.write_bytes(b"extra")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    seen: list[tuple[Path, int, int]] = []
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(
            volume_mismatch_decision=lambda source, _destination, size, companions, _exists: (
                seen.append((source, size, companions)) or "skip"
            )
        ),
    )

    assert not report.has_failures
    assert seen == [(extra, 5, 0)]
    assert not (dest / "EXTRA.JPG").exists()
    log_text = "\n".join(report.logs)
    assert f"VOLUME MISMATCH: {extra}" in log_text
    assert f"VOLUME MISMATCH — SKIPPED: {extra}" in log_text


def test_clone_second_source_extra_file_can_be_copied_after_verification(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "EXTRA.JPG").write_bytes(b"extra")

    observed_logs: list[str] = []

    def copy_extra(
        _source: Path,
        _destination: Path,
        _size: int,
        _companions: int,
        _destination_exists: bool,
    ) -> str:
        assert any(line.startswith("CLONE VERIFIED:") for line in observed_logs)
        return "copy"

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(log=observed_logs.append, volume_mismatch_decision=copy_extra),
    )

    assert not report.has_failures
    assert (dest / "EXTRA.JPG").read_bytes() == b"extra"
    assert "VOLUME MISMATCH — COPIED" in "\n".join(report.logs)
    mismatch_result = next(
        result for result in report.results if result.source.name == "EXTRA.JPG"
    )
    assert mismatch_result.hash_ok is True
    assert mismatch_result.volume_mismatch is True


def test_clone_second_source_extra_file_can_cancel_job(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "DSC0001.JPG").write_bytes(b"image")
    (secondary / "EXTRA.JPG").write_bytes(b"extra")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring=""
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(volume_mismatch_decision=lambda *_args: "cancel"),
    )

    assert report.cancelled
    assert not (dest / "EXTRA.JPG").exists()


def test_clone_rating_selection_comes_only_from_first_source_volume(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    for root in (primary, secondary):
        (root / "SELECTED.JPG").write_bytes(b"selected")
        (root / "FILTERED.JPG").write_bytes(b"filtered")
    (primary / "SELECTED.xmp").write_text(
        '<rdf:Description xmp:Rating="5" />', encoding="utf-8"
    )
    (primary / "FILTERED.xmp").write_text(
        '<rdf:Description xmp:Rating="1" />', encoding="utf-8"
    )

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        minrating=3,
        checkhash=False,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert (dest / "SELECTED.JPG").exists()
    assert not (dest / "FILTERED.JPG").exists()
    assert report.clone_verified_count == 1
    assert "VOLUME MISMATCH" not in "\n".join(report.logs)


def test_clone_second_source_missing_file_is_failure(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    missing = primary / "DSC0001.JPG"
    missing.write_bytes(b"image")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        )
    )

    assert report.has_failures
    assert (
        f"CLONE FAILED: {secondary / 'DSC0001.JPG'} "
        "(5 bytes, selected on the first-source volume, not found on the "
        "second-source volume)"
    ) in "\n".join(report.logs)


def test_summary_counts_written_files_and_places_copy_time_last(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"first")
    (secondary / "DSC0001.JPG").write_bytes(b"second")
    (dest / "DSC0001.JPG").write_bytes(b"old")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        collisionpolicy="overwrite",
    )
    callbacks = CopyCallbacks(
        clone_mismatch_decision=lambda source, destination, allow_remove: CloneMismatchResponse(
            decision="keep_both",
            remove_source=False,
        )
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        callbacks,
    )

    summary = report.summary_lines()
    assert "  2 copied" in summary
    assert "  0 new" not in summary
    assert "  1 replaced" in summary
    assert "  1 kept both" in summary
    assert "  0 failed" in summary
    assert "  0 skipped" not in summary
    assert summary[-1].startswith("Copy time:")


def test_summary_hides_zero_categories_except_failed(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"image")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
    )
    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))
    summary = report.summary_lines()

    assert "  1 total" in summary
    assert "  1 copied" in summary
    assert "  1 new" in summary
    assert "  0 failed" in summary
    assert "  0 replaced" not in summary
    assert "  0 kept both" not in summary
    assert "  0 skipped" not in summary
    assert "  0 source files removed" not in summary


def test_copy_time_excludes_existing_file_prompt_wait(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    (card / "DSC0001.JPG").write_bytes(b"new")
    (dest / "DSC0001.JPG").write_bytes(b"old")

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=False,
        collisionpolicy="ask",
    )

    def slow_skip(source, destination):  # noqa: ANN001
        import time

        time.sleep(0.15)
        return "skip"

    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config),
        CopyCallbacks(collision_decision=slow_skip),
    )

    assert report.prompt_wait_seconds >= 0.1
    assert report.finished_at is not None
    assert report.copy_seconds is not None
    elapsed = (report.finished_at - report.started_at).total_seconds()
    assert elapsed - report.copy_seconds >= 0.1
    summary = "\n".join(report.summary_lines())
    assert "Copy time:" in summary
    assert "User decision time:" not in summary


def test_copy_time_excludes_clone_mismatch_prompt_wait(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    (primary / "DSC0001.JPG").write_bytes(b"first")
    (secondary / "DSC0001.JPG").write_bytes(b"second")

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )

    def slow_keep_both(source, destination, allow_remove):  # noqa: ANN001
        import time

        time.sleep(0.15)
        return CloneMismatchResponse(decision="keep_both", remove_source=False)

    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(clone_mismatch_decision=slow_keep_both),
    )

    assert report.prompt_wait_seconds >= 0.1
    assert report.finished_at is not None
    assert report.copy_seconds is not None
    elapsed = (report.finished_at - report.started_at).total_seconds()
    assert elapsed - report.copy_seconds >= 0.1
    summary = "\n".join(report.summary_lines())
    assert "Copy time:" in summary
    assert "User decision time:" not in summary


def test_cancelled_primary_clone_copy_does_not_verify_missing_destination(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    payload = b"x" * (1024 * 1024)
    (primary / "DSC0001.JPG").write_bytes(payload)
    (secondary / "DSC0001.JPG").write_bytes(payload)

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    cancel_event = Event()

    def cancel_after_first_chunk(done: int, total: int, index: int) -> None:
        if done:
            cancel_event.set()

    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(byte_progress=cancel_after_first_chunk),
        cancel_event,
    )

    log_text = "\n".join(report.logs)
    assert report.cancelled
    assert "destination does not match first source" not in log_text
    assert "No such file or directory" not in log_text
    assert "copy cancelled" in log_text
    assert not (dest / "DSC0001.JPG").exists()


def test_cancellable_hash_compare_stops_when_cancelled(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    destination = tmp_path / "destination.bin"
    source.write_bytes(b"x" * (1024 * 1024))
    destination.write_bytes(b"x" * (1024 * 1024))
    cancel_event = Event()
    cancel_event.set()

    from cameracopy2.core.hash import compare_sha256_cancellable

    result = compare_sha256_cancellable(source, destination, cancel_event)

    assert not result.ok
    assert result.error == "cancelled"


def test_clone_skip_existing_does_not_emit_metered_hash_progress(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    payload = b"x" * (1024 * 1024)
    (primary / "DSC0001.JPG").write_bytes(payload)
    (secondary / "DSC0001.JPG").write_bytes(payload)
    (dest / "DSC0001.JPG").write_bytes(payload)

    metered_progress: list[tuple[int, int, bool]] = []

    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        collisionpolicy="ask",
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(
            collision_decision=lambda source, destination: "skip",
            source_progress=lambda done, total, metered: metered_progress.append(
                (done, total, metered)
            ),
        ),
    )

    assert not report.has_failures
    assert not any(metered and done > 0 for done, total, metered in metered_progress)


def test_clone_second_source_verification_emits_metered_hash_and_byte_progress(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    dest = tmp_path / "pictures"
    primary.mkdir()
    secondary.mkdir()
    dest.mkdir()
    payload = b"x" * (1024 * 1024)
    (primary / "DSC0001.JPG").write_bytes(payload)
    (secondary / "DSC0001.JPG").write_bytes(payload)

    metered_progress: list[tuple[int, int, bool]] = []
    byte_progress: list[tuple[int, int, int]] = []

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(primary),
            secondary=make_volume(secondary),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(
            source_progress=lambda done, total, metered: metered_progress.append(
                (done, total, metered)
            ),
            byte_progress=lambda done, total, index: byte_progress.append((done, total, index)),
        ),
    )

    assert not report.has_failures
    assert any(metered and done > 0 for done, total, metered in metered_progress)
    assert (0, len(payload), 1) in byte_progress
    assert (len(payload), len(payload), 1) in byte_progress


def test_scanner_skips_symlinked_files_outside_source_root(tmp_path: Path) -> None:
    card = tmp_path / "card"
    outside = tmp_path / "outside"
    card.mkdir()
    outside.mkdir()
    real = card / "REAL.JPG"
    real.write_bytes(b"real")
    target = outside / "SECRET.JPG"
    target.write_bytes(b"secret")
    link = card / "LINK.JPG"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        return

    files = find_candidate_files(card, ["*.JPG"])

    assert files == [real]


def test_sha256_copy_source_progress_is_monotonic(tmp_path: Path) -> None:
    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    payload = b"x" * (1024 * 1024 + 3)
    (card / "DSC0001.JPG").write_bytes(payload)
    seen: list[int] = []

    config = CameraCopyConfig(
        source="", destination=str(dest), includedfiles=["*.JPG"], datetimestring="", checkhash=True
    )
    report = CopyEngine().run(
        CopyJob(primary=make_volume(card), secondary=None, config=config),
        CopyCallbacks(source_progress=lambda done, total, metered: seen.append(done)),
    )

    assert not report.has_failures
    assert seen == sorted(seen)
    assert seen[-1] == len(payload)


def test_copy_engine_shares_default_exiftool_service() -> None:
    engine = CopyEngine()
    assert engine.rating_reader.exiftool is engine.timestamp_resolver.exiftool


def test_exiftool_warm_cache_chunks_batches_and_uses_option_terminator(
    tmp_path: Path, monkeypatch
) -> None:
    from subprocess import CompletedProcess
    import json

    from cameracopy2.services.metadata_service import ExifToolService

    files = []
    for name in ["-dash.JPG", "normal.JPG", "third.JPG"]:
        path = tmp_path / name
        path.write_bytes(b"image")
        files.append(path)

    commands: list[list[str]] = []

    def fake_run(command, **_kwargs):  # noqa: ANN001
        commands.append(command)
        separator = command.index("--")
        payload = [{"SourceFile": path, "XMP:Rating": 5} for path in command[separator + 1 :]]
        return CompletedProcess(command, 0, stdout=json.dumps(payload), stderr="")

    service = ExifToolService()
    service.available = True
    service.executable = "exiftool"
    monkeypatch.setattr("cameracopy2.services.metadata_service.subprocess.run", fake_run)

    service.warm_cache(files, batch_size=2)

    assert len(commands) == 2
    assert all("--" in command for command in commands)
    first_separator = commands[0].index("--")
    assert commands[0][first_separator + 1].endswith("-dash.JPG")
    assert service.read_rating(files[0]) == 5


def test_exiftool_warm_cache_honors_pre_cancelled_event(tmp_path: Path, monkeypatch) -> None:
    from cameracopy2.services.metadata_service import ExifToolService

    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    cancel_event = Event()
    cancel_event.set()
    called = False

    def fake_run(*_args, **_kwargs):  # noqa: ANN001
        nonlocal called
        called = True
        raise AssertionError("subprocess should not run after cancellation")

    service = ExifToolService()
    service.available = True
    service.executable = "exiftool"
    monkeypatch.setattr("cameracopy2.services.metadata_service.subprocess.run", fake_run)

    service.warm_cache([media], cancel_event=cancel_event)

    assert not called


def test_unreadable_or_unparseable_xmp_rating_returns_none(tmp_path: Path, monkeypatch) -> None:
    import xml.etree.ElementTree as ET

    media = tmp_path / "DSC0001.ARW"
    sidecar = tmp_path / "DSC0001.xmp"
    media.write_text("raw")
    sidecar.write_text('<rdf:Description xmp:Rating="4" />')

    def fake_parse(path):  # noqa: ANN001
        raise OSError("cannot read")

    def fake_read_text(*_args, **_kwargs):  # noqa: ANN001
        raise OSError("cannot read")

    monkeypatch.setattr(ET, "parse", fake_parse)
    monkeypatch.setattr(Path, "read_text", fake_read_text)

    assert read_sidecar_rating_with_source(media) is None


def test_linux_format_refuses_non_removable_volume(tmp_path: Path) -> None:
    service = FormatService()
    volume = VolumeInfo(
        id="internal",
        display_name="Internal",
        mount_path=tmp_path,
        device_path="/dev/nvme0n1p1",
        removable=False,
        platform="linux",
    )

    result = service._format_linux(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert "non-removable" in result.message


def test_linux_format_refuses_critical_mount() -> None:
    service = FormatService()
    volume = VolumeInfo(
        id="root",
        display_name="Root",
        mount_path=Path("/"),
        device_path="/dev/sda1",
        removable=True,
        platform="linux",
    )

    result = service._format_linux(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert "critical Linux mount" in result.message


def test_exiftool_cancellable_warm_cache_uses_communicate_loop(tmp_path: Path, monkeypatch) -> None:
    import json
    import subprocess

    from cameracopy2.services.metadata_service import ExifToolService

    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    cancel_event = Event()
    calls = {"communicate": 0}

    class FakeProcess:
        returncode = 0

        def communicate(self, timeout=None):  # noqa: ANN001, ANN201
            calls["communicate"] += 1
            if calls["communicate"] == 1:
                raise subprocess.TimeoutExpired(cmd="exiftool", timeout=timeout)
            return json.dumps([{"SourceFile": str(media), "XMP:Rating": 5}]), ""

        def poll(self):  # noqa: ANN201
            return 0

        def terminate(self):  # noqa: ANN201
            raise AssertionError(
                "process should not be terminated on a transient communicate timeout"
            )

        def kill(self):  # noqa: ANN201
            raise AssertionError("process should not be killed on a transient communicate timeout")

    def fake_popen(*_args, **_kwargs):  # noqa: ANN001, ANN201
        return FakeProcess()

    service = ExifToolService()
    service.available = True
    service.executable = "exiftool"
    monkeypatch.setattr("cameracopy2.services.metadata_service.subprocess.Popen", fake_popen)

    service.warm_cache([media], cancel_event=cancel_event)

    assert calls["communicate"] == 2
    assert service.read_rating(media) == 5


def test_exiftool_cancellable_warm_cache_terminates_after_cancel(
    tmp_path: Path, monkeypatch
) -> None:
    import subprocess

    from cameracopy2.services.metadata_service import ExifToolService

    media = tmp_path / "DSC0002.JPG"
    media.write_bytes(b"image")
    cancel_event = Event()
    state = {"terminated": False, "communicate": 0}

    class FakeProcess:
        returncode = None

        def communicate(self, timeout=None):  # noqa: ANN001, ANN201
            state["communicate"] += 1
            if not state["terminated"]:
                cancel_event.set()
                raise subprocess.TimeoutExpired(cmd="exiftool", timeout=timeout)
            return "", ""

        def poll(self):  # noqa: ANN201
            return None if not state["terminated"] else -15

        def terminate(self):  # noqa: ANN201
            state["terminated"] = True
            self.returncode = -15

        def kill(self):  # noqa: ANN201
            state["terminated"] = True
            self.returncode = -9

    def fake_popen(*_args, **_kwargs):  # noqa: ANN001, ANN201
        return FakeProcess()

    service = ExifToolService()
    service.available = True
    service.executable = "exiftool"
    monkeypatch.setattr("cameracopy2.services.metadata_service.subprocess.Popen", fake_popen)

    service.warm_cache([media], cancel_event=cancel_event)

    assert state["terminated"]
    assert state["communicate"] == 2


def test_config_accepts_linux_native_format_choice() -> None:
    config = config_from_dict({"version": 2, "autoformat": "ext4"})

    assert config.autoformat == "ext4"


def test_linux_available_filesystems_excludes_ntfs_and_includes_ext(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    available_tools = {
        "mkfs.exfat",
        "mkfs.vfat",
        "mkfs.ext2",
        "mkfs.ext3",
        "mkfs.ext4",
    }
    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")
    monkeypatch.setattr(format_module, "qt_dbus_support_error", lambda: None)
    monkeypatch.setattr(
        format_module.shutil,
        "which",
        lambda tool: f"/usr/bin/{tool}" if tool in available_tools else None,
    )
    monkeypatch.setattr(FormatService, "_linux_udisks_service_error", lambda self: None)

    availability = FormatService().available_filesystems()

    assert list(availability) == ["exFAT", "FAT32", "ext2", "ext3", "ext4"]
    assert "NTFS" not in availability
    assert all(reason is None for reason in availability.values())
    assert not FormatService().can_format("NTFS")


def test_windows_available_filesystems_keeps_ntfs_and_excludes_ext(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        format_module.shutil,
        "which",
        lambda tool: "powershell.exe" if tool == "powershell.exe" else None,
    )
    monkeypatch.setattr(FormatService, "_windows_wmi_available", staticmethod(lambda: True))

    availability = FormatService().available_filesystems()

    assert list(availability) == ["exFAT", "FAT32", "NTFS"]
    assert "ext4" not in availability
    assert FormatService().can_format("NTFS")
    assert not FormatService().can_format("ext4")


def test_windows_available_filesystems_does_not_require_admin(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        format_module.shutil,
        "which",
        lambda tool: "powershell.exe" if tool == "powershell.exe" else None,
    )
    monkeypatch.setattr(FormatService, "_windows_wmi_available", staticmethod(lambda: True))
    monkeypatch.setattr(FormatService, "_windows_is_admin", staticmethod(lambda: False))

    availability = FormatService().available_filesystems()

    assert all(reason is None for reason in availability.values())
    assert FormatService().can_format("exFAT")


def test_windows_available_filesystems_requires_wmi(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Windows")
    monkeypatch.setattr(
        format_module.shutil,
        "which",
        lambda tool: "powershell.exe" if tool == "powershell.exe" else None,
    )
    monkeypatch.setattr(FormatService, "_windows_wmi_available", staticmethod(lambda: False))
    monkeypatch.setattr(FormatService, "_windows_is_admin", staticmethod(lambda: True))

    availability = FormatService().available_filesystems()

    assert all("WMI" in reason for reason in availability.values() if reason)
    assert not FormatService().can_format("exFAT")


def test_windows_compatibility_statuses_include_on_demand_elevation_and_wmi(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Windows")
    monkeypatch.setattr(FormatService, "_windows_shell", staticmethod(lambda: "powershell.exe"))
    monkeypatch.setattr(FormatService, "_windows_wmi_available", staticmethod(lambda: True))
    monkeypatch.setattr(FormatService, "_windows_is_admin", staticmethod(lambda: False))

    lines = FormatService().compatibility_report_lines()

    assert any(line.startswith("PowerShell: ✅") for line in lines)
    assert any(
        line == "Administrator access: ✅ UAC requested only if required" for line in lines
    )
    assert any(line.startswith("WMI support: ✅") for line in lines)
    assert any(line.startswith("Removable detection: ✅") for line in lines)


def test_linux_format_volume_rejects_ntfs_before_command(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")
    monkeypatch.setattr(format_module.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    service = FormatService()
    monkeypatch.setattr(
        service,
        "_run_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("should not run command")),
    )
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=tmp_path,
        device_path="/dev/sdb1",
        removable=True,
        platform="linux",
    )
    report = CopyReport(started_at=datetime.now())

    result = service.format_volume(volume, "NTFS", report)

    assert not result.ok
    assert "not supported on linux" in result.message


def test_linux_format_ext4_uses_one_udisks_client_and_remounts(tmp_path: Path) -> None:
    calls: list[tuple[object, ...]] = []

    class FakeClient:
        def unmount_device(self, device: str):  # noqa: ANN202
            calls.append(("unmount", device))
            return UDisksCallResult(True)

        def format_device(self, device: str, filesystem_type: str, label: str | None):  # noqa: ANN202
            calls.append(("format", device, filesystem_type, label))
            return UDisksCallResult(True)

        def mount_device(self, device: str):  # noqa: ANN202
            calls.append(("mount", device))
            return UDisksCallResult(True, arguments=("/run/media/test/CARD",))

    client = FakeClient()
    service = FormatService(linux_udisks_factory=lambda: client)  # type: ignore[arg-type]
    mount = tmp_path / "card"
    mount.mkdir()
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=mount,
        device_path="/dev/sdb1",
        label="CARD",
        removable=True,
        platform="linux",
    )

    result = service._format_linux(volume, "ext4")  # noqa: SLF001

    assert result.ok
    assert calls == [
        ("unmount", "/dev/sdb1"),
        ("format", "/dev/sdb1", "ext4", "CARD"),
        ("mount", "/dev/sdb1"),
    ]


def test_linux_available_filesystems_requires_qt_dbus(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        format_module,
        "qt_dbus_support_error",
        lambda: "Qt D-Bus support is unavailable.",
    )

    availability = FormatService().available_filesystems()

    assert all(
        reason == "Qt D-Bus support is unavailable." for reason in availability.values()
    )


def test_linux_available_filesystems_requires_udisks_service(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    available_tools = {
        "mkfs.exfat",
        "mkfs.vfat",
        "mkfs.ext2",
        "mkfs.ext3",
        "mkfs.ext4",
    }
    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")
    monkeypatch.setattr(format_module, "qt_dbus_support_error", lambda: None)
    monkeypatch.setattr(
        format_module.shutil,
        "which",
        lambda tool: f"/usr/bin/{tool}" if tool in available_tools else None,
    )
    monkeypatch.setattr(
        FormatService,
        "_linux_udisks_service_error",
        lambda self: "UDisks2 service is not available: unavailable",
    )

    availability = FormatService().available_filesystems()

    assert all(
        reason == "UDisks2 service is not available: unavailable"
        for reason in availability.values()
    )


def test_linux_format_passes_label_without_shell_quoting(tmp_path: Path) -> None:
    captured: dict[str, object] = {}

    class FakeClient:
        def unmount_device(self, _device: str):  # noqa: ANN202
            return UDisksCallResult(True)

        def format_device(self, device: str, filesystem_type: str, label: str | None):  # noqa: ANN202
            captured.update(device=device, filesystem_type=filesystem_type, label=label)
            return UDisksCallResult(True)

        def mount_device(self, _device: str):  # noqa: ANN202
            return UDisksCallResult(True)

    mount = tmp_path / "card"
    mount.mkdir()
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=mount,
        device_path="/dev/sdb1",
        label="CAM'ERA",
        removable=True,
        platform="linux",
    )
    service = FormatService(linux_udisks_factory=FakeClient)  # type: ignore[arg-type]

    result = service._format_linux(volume, "exFAT")  # noqa: SLF001

    assert result.ok
    assert captured == {
        "device": "/dev/sdb1",
        "filesystem_type": "exfat",
        "label": "CAM'ERA",
    }


def test_format_run_command_uses_noninteractive_hidden_windows_process(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    captured: dict[str, object] = {}

    def fake_run(_command, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return subprocess.CompletedProcess([], 0, stdout="", stderr="")

    monkeypatch.setattr("cameracopy2.services.format_service.subprocess.run", fake_run)
    monkeypatch.setattr(
        "cameracopy2.services.format_service.py_platform.system", lambda: "Windows"
    )

    result = FormatService._run_command(["format-command"], "formatted")  # noqa: SLF001

    assert result.ok
    assert captured["stdin"] is subprocess.DEVNULL
    assert captured["creationflags"] == getattr(
        subprocess, "CREATE_NO_WINDOW", 0x08000000
    )


def test_format_run_command_reports_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import subprocess

    def fake_run(*_args, **_kwargs):  # noqa: ANN001, ANN202
        raise subprocess.TimeoutExpired(cmd=["slow-command"], timeout=5)

    monkeypatch.setattr("cameracopy2.services.format_service.subprocess.run", fake_run)

    result = FormatService._run_command(["slow-command"], "", timeout_seconds=5)  # noqa: SLF001

    assert not result.ok
    assert "timed out" in result.message


def test_linux_format_service_reuses_one_client_instance() -> None:
    created: list[object] = []

    class FakeClient:
        def __init__(self) -> None:
            created.append(self)

    service = FormatService(linux_udisks_factory=FakeClient)  # type: ignore[arg-type]

    assert service._linux_udisks() is service._linux_udisks()  # noqa: SLF001
    assert len(created) == 1


def test_windows_format_refuses_non_removable_volume(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = FormatService()
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(service, "_windows_shell", lambda: "powershell.exe")
    volume = VolumeInfo(
        id="e",
        display_name="Internal E",
        mount_path=Path("E:\\"),
        removable=False,
        platform="windows",
    )

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert "non-removable Windows volume" in result.message


def test_windows_format_refuses_unknown_removable_volume(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = FormatService()
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(service, "_windows_shell", lambda: "powershell.exe")
    volume = VolumeInfo(
        id="e",
        display_name="Unknown E",
        mount_path=Path("E:\\"),
        removable=None,
        platform="windows",
    )

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert "could not confirm it is removable" in result.message


def test_windows_format_retries_with_uac_after_access_denied(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services.format_service import WINDOWS_ELEVATION_REQUIRED

    service = FormatService()
    calls: list[list[str]] = []
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(service, "_windows_shell", lambda: "powershell.exe")
    monkeypatch.setattr(service, "_windows_wmi_available", lambda: True)

    def fake_run(command, success_message, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(command)
        if len(calls) == 1:
            return FormatResult(False, f"{WINDOWS_ELEVATION_REQUIRED}\nAccess is denied.")
        return FormatResult(True, success_message)

    monkeypatch.setattr(service, "_run_command", fake_run)
    volume = VolumeInfo(
        id="e",
        display_name="Removable E",
        mount_path=Path("E:\\"),
        removable=True,
        platform="windows",
    )

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert result.ok
    assert len(calls) == 2
    assert "Format-Volume -DriveLetter E" in calls[0][-1]
    assert "Start-Process" in calls[1][-1]
    assert "-Verb RunAs" in calls[1][-1]
    assert "-WindowStyle Hidden" in calls[1][-1]
    assert "-EncodedCommand" in calls[1][-1]


def test_windows_format_does_not_request_uac_for_other_failures(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    service = FormatService()
    calls: list[list[str]] = []
    monkeypatch.setenv("SystemDrive", "C:")
    monkeypatch.setattr(service, "_windows_shell", lambda: "powershell.exe")
    monkeypatch.setattr(service, "_windows_wmi_available", lambda: True)

    def fake_run(command, _success_message, **_kwargs):  # noqa: ANN001, ANN202
        calls.append(command)
        return FormatResult(False, "The volume is in use.")

    monkeypatch.setattr(service, "_run_command", fake_run)
    volume = VolumeInfo(
        id="e",
        display_name="Removable E",
        mount_path=Path("E:\\"),
        removable=True,
        platform="windows",
    )

    result = service._format_windows(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert result.message == "The volume is in use."
    assert len(calls) == 1


def test_linux_udisks_object_path_uses_device_name() -> None:
    assert (
        LinuxUDisksClient.object_path_for_device("/dev/sdf1")
        == "/org/freedesktop/UDisks2/block_devices/sdf1"
    )


def test_linux_udisks_object_path_supports_mmc_partitions() -> None:
    assert (
        LinuxUDisksClient.object_path_for_device("/dev/mmcblk0p1")
        == "/org/freedesktop/UDisks2/block_devices/mmcblk0p1"
    )


def test_linux_udisks_object_path_encodes_device_name() -> None:
    assert (
        LinuxUDisksClient.object_path_for_device("/dev/dm-0")
        == "/org/freedesktop/UDisks2/block_devices/dm_2d0"
    )


def test_linux_format_compatibility_report_lists_qt_dbus_and_tools(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import format_service as format_module

    available_tools = {"mkfs.exfat", "mkfs.vfat", "mkfs.ext4"}
    monkeypatch.setattr(format_module.py_platform, "system", lambda: "Linux")
    monkeypatch.setattr(format_module, "qt_dbus_support_error", lambda: None)
    monkeypatch.setattr(
        format_module.shutil,
        "which",
        lambda tool: f"/usr/bin/{tool}" if tool in available_tools else None,
    )
    monkeypatch.setattr(FormatService, "_linux_udisks_service_error", lambda self: None)

    report = FormatService().compatibility_report()

    assert "Qt D-Bus: ✅ available via PySide6" in report
    assert "UDisks2 service: ✅ available" in report
    assert "ext2 formatter: ❌" in report
    assert "ext4 formatter: ✅ mkfs.ext4" in report
    assert "gdbus" not in report
    assert "udisksctl" not in report
    assert "  " not in report
    assert "\n" in report


def test_linux_volume_service_does_not_treat_mount_name_as_label(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform import linux as linux_module
    from cameracopy2.platform.linux import LinuxVolumeService

    mount = tmp_path / "A9F1-F451"
    mount.mkdir()

    class Partition:
        device = "/dev/sdf1"
        mountpoint = str(mount)
        fstype = "exfat"

    monkeypatch.setattr(linux_module.psutil, "disk_partitions", lambda all=False: [Partition()])
    monkeypatch.setattr(
        LinuxVolumeService, "_usage_total", staticmethod(lambda _mountpoint: 119_100_000_000)
    )
    monkeypatch.setattr(
        LinuxVolumeService,
        "_lsblk_by_path",
        staticmethod(
            lambda: {
                "/dev/sdf1": {
                    "path": "/dev/sdf1",
                    "label": None,
                    "model": "DSC",
                    "fstype": "exfat",
                    "rm": True,
                    "uuid": "A9F1-F451",
                    "tran": "usb",
                }
            }
        ),
    )
    monkeypatch.setattr(LinuxVolumeService, "_udev_context", staticmethod(lambda: None))

    [volume] = LinuxVolumeService().list_volumes()

    assert volume.label is None
    assert volume.uuid == "A9F1-F451"
    assert volume.id == f"uuid:A9F1-F451:{mount}"
    assert volume.display_name.count("A9F1-F451") == 1


def test_linux_volume_service_preserves_real_filesystem_label(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform import linux as linux_module
    from cameracopy2.platform.linux import LinuxVolumeService

    mount = tmp_path / "CAMCARD"
    mount.mkdir()

    class Partition:
        device = "/dev/sdg1"
        mountpoint = str(mount)
        fstype = "exfat"

    monkeypatch.setattr(linux_module.psutil, "disk_partitions", lambda all=False: [Partition()])
    monkeypatch.setattr(LinuxVolumeService, "_usage_total", staticmethod(lambda _mountpoint: None))
    monkeypatch.setattr(
        LinuxVolumeService,
        "_lsblk_by_path",
        staticmethod(
            lambda: {
                "/dev/sdg1": {
                    "path": "/dev/sdg1",
                    "label": "CAMCARD",
                    "fstype": "exfat",
                    "rm": True,
                    "uuid": "1234-ABCD",
                    "tran": "usb",
                }
            }
        ),
    )
    monkeypatch.setattr(LinuxVolumeService, "_udev_context", staticmethod(lambda: None))

    [volume] = LinuxVolumeService().list_volumes()

    assert volume.label == "CAMCARD"
    assert "CAMCARD" in volume.display_name


def test_linux_format_passes_empty_label_as_none(tmp_path: Path) -> None:
    labels: list[str | None] = []

    class FakeClient:
        def unmount_device(self, _device: str):  # noqa: ANN202
            return UDisksCallResult(True)

        def format_device(self, _device: str, _filesystem_type: str, label: str | None):  # noqa: ANN202
            labels.append(label)
            return UDisksCallResult(True)

        def mount_device(self, _device: str):  # noqa: ANN202
            return UDisksCallResult(True)

    mount = tmp_path / "card"
    mount.mkdir()
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=mount,
        device_path="/dev/sdb1",
        label=None,
        removable=True,
        platform="linux",
    )
    service = FormatService(linux_udisks_factory=FakeClient)  # type: ignore[arg-type]

    assert service._format_linux(volume, "ext4").ok  # noqa: SLF001
    assert labels == [None]


def test_config_loads_default_volume_match_objects() -> None:
    config = config_from_dict(
        {
            "version": 2,
            "defaultprimaryvolumematch": {"method": "device_serial", "value": "Sony_DSC_1-0:0"},
            "defaultsecondaryvolumematch": {"method": "size", "value": 127848677376},
        }
    )

    assert config.defaultprimaryvolumematch.method == "device_serial"
    assert config.defaultprimaryvolumematch.value == "Sony_DSC_1-0:0"
    assert config.defaultsecondaryvolumematch.method == "size"
    assert config.defaultsecondaryvolumematch.value == 127848677376


def test_config_ignores_legacy_partition_uuid_default_match() -> None:
    config = config_from_dict(
        {
            "version": 2,
            "defaultprimaryvolumematch": {
                "method": "partition_uuid",
                "value": "00000000-01",
            },
        }
    )

    assert config.defaultprimaryvolumematch == VolumeMatch()


def test_config_rejects_unknown_default_volume_match_method() -> None:
    try:
        config_from_dict(
            {
                "version": 2,
                "defaultprimaryvolumematch": {"method": "automatic", "value": "anything"},
            }
        )
    except UnsupportedConfigError:
        pass
    else:
        raise AssertionError("unknown default-volume match method should be unsupported")


def test_volume_match_label_uses_human_readable_name_and_current_value(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import volume_match_label

    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=tmp_path / "card",
        device_serial="Sony_DSC_D080E039A120-0:1",
        uuid="5524-C0BB",
        partition_uuid="00000000-01",
        label=None,
        size_bytes=127848677376,
        hardware_path="pci-0000:00:14.0-usb-0:10.2.2:1.0-scsi-0:0:0:1",
    )

    assert (
        volume_match_label(volume, "device_serial") == "Device serial — Sony_DSC_D080E039A120-0:1"
    )
    assert volume_match_label(volume, "fs_uuid") == "Filesystem UUID — 5524-C0BB"
    assert volume_match_label(volume, "label") == "Label — unavailable"
    assert volume_match_label(volume, "mount_point").startswith("Mount point — ")
    assert "127848677376 bytes" in volume_match_label(volume, "size")


def test_default_volume_matching_by_device_serial_survives_uuid_and_mount_change(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import AUTO_PENDING, resolve_refreshed_volume_ids

    old_primary_id = f"uuid:B37A-C96B:{tmp_path / 'old-primary'}"
    old_secondary_id = f"uuid:5524-C0BB:{tmp_path / 'old-secondary'}"
    primary = VolumeInfo(
        id=f"uuid:AAAA-BBBB:{tmp_path / 'new-primary'}",
        display_name="Primary",
        mount_path=tmp_path / "new-primary",
        uuid="AAAA-BBBB",
        device_serial="Sony_DSC_D080E039A120-0:0",
    )
    secondary = VolumeInfo(
        id=f"uuid:CCCC-DDDD:{tmp_path / 'new-secondary'}",
        display_name="Secondary",
        mount_path=tmp_path / "new-secondary",
        uuid="CCCC-DDDD",
        device_serial="Sony_DSC_D080E039A120-0:1",
    )

    assert resolve_refreshed_volume_ids(
        [secondary, primary],
        "",
        "",
        AUTO_PENDING,
        AUTO_PENDING,
        old_primary_id,
        old_secondary_id,
        VolumeMatch("device_serial", "Sony_DSC_D080E039A120-0:0"),
        VolumeMatch("device_serial", "Sony_DSC_D080E039A120-0:1"),
    )[:2] == (primary.id, secondary.id)


def test_default_volume_matching_falls_back_to_legacy_id_when_match_missing(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import AUTO_PENDING, resolve_refreshed_volume_ids

    primary = VolumeInfo(id="legacy-primary", display_name="Primary", mount_path=tmp_path / "p")
    secondary = VolumeInfo(
        id="legacy-secondary", display_name="Secondary", mount_path=tmp_path / "s"
    )

    assert resolve_refreshed_volume_ids(
        [secondary, primary],
        "",
        "",
        AUTO_PENDING,
        AUTO_PENDING,
        "legacy-primary",
        "legacy-secondary",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", ""),
    )[:2] == ("legacy-primary", "legacy-secondary")


def test_default_volume_matching_does_not_offer_partition_uuid() -> None:
    from cameracopy2.ui.volume_selection import MATCH_METHODS

    assert "partition_uuid" not in MATCH_METHODS


def test_linux_udev_metadata_uses_full_serial_not_short_serial(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform.linux import LinuxVolumeService

    class FakeAttrs:
        def get(self, key):  # noqa: ANN001
            return b"1" if key == "removable" else None

    class FakeDevice:
        attributes = FakeAttrs()

        def __init__(self):
            self.values = {
                "ID_SERIAL": "Sony_DSC_D080E039A120-0:1",
                "ID_SERIAL_SHORT": "D080E039A120",
                "ID_USB_SERIAL_SHORT": "D080E039A120",
                "PARTUUID": "00000000-01",
                "ID_PATH": "pci-path-slot-1",
                "ID_FS_UUID": "5524-C0BB",
            }

        @property
        def properties(self):  # noqa: ANN201
            return self.values

        def get(self, key):  # noqa: ANN001
            return self.values.get(key)

        def find_parent(self, _subsystem):  # noqa: ANN001
            return self

    class FakeDevices:
        @staticmethod
        def from_device_file(_context, _device_file):  # noqa: ANN001
            return FakeDevice()

    import sys

    monkeypatch.setitem(sys.modules, "pyudev", type("FakePyUdev", (), {"Devices": FakeDevices}))

    metadata = LinuxVolumeService._udev_metadata(object(), "/dev/sdg1")  # noqa: SLF001

    assert metadata["device_serial"] == "Sony_DSC_D080E039A120-0:1"
    assert metadata["device_serial"] != "D080E039A120"
    assert metadata["partition_uuid"] == "00000000-01"
    assert metadata["hardware_path"] == "pci-path-slot-1"


def test_linux_volume_service_populates_matching_fields(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.platform import linux as linux_module
    from cameracopy2.platform.linux import LinuxVolumeService

    mount = tmp_path / "5524-C0BB"
    mount.mkdir()

    class Partition:
        device = "/dev/sdg1"
        mountpoint = str(mount)
        fstype = "exfat"

    monkeypatch.setattr(linux_module.psutil, "disk_partitions", lambda all=False: [Partition()])
    monkeypatch.setattr(LinuxVolumeService, "_usage_total", staticmethod(lambda _mountpoint: 1))
    monkeypatch.setattr(
        LinuxVolumeService,
        "_lsblk_by_path",
        staticmethod(
            lambda: {
                "/dev/sdg1": {
                    "path": "/dev/sdg1",
                    "label": None,
                    "model": "DSC",
                    "fstype": "exfat",
                    "rm": True,
                    "uuid": "5524-C0BB",
                    "tran": "usb",
                    "partuuid": "00000000-01",
                    "size": "127848677376",
                }
            }
        ),
    )
    monkeypatch.setattr(
        LinuxVolumeService,
        "_udev_metadata",
        staticmethod(
            lambda _context, _device: {
                "device_serial": "Sony_DSC_D080E039A120-0:1",
                "hardware_path": "pci-0000:00:14.0-usb-0:10.2.2:1.0-scsi-0:0:0:1",
                "partition_uuid": "00000000-01",
            }
        ),
    )
    monkeypatch.setattr(LinuxVolumeService, "_udev_context", staticmethod(lambda: object()))

    [volume] = LinuxVolumeService().list_volumes()

    assert volume.device_serial == "Sony_DSC_D080E039A120-0:1"
    assert volume.partition_uuid == "00000000-01"
    assert volume.hardware_path.endswith("0:0:0:1")
    assert volume.size_bytes == 127848677376


def test_generic_volume_service_does_not_treat_mount_name_as_label(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.services import volume_service as volume_module
    from cameracopy2.services.volume_service import VolumeService

    mount = tmp_path / "A9F1-F451"
    mount.mkdir()

    class Partition:
        device = "/dev/sdz1"
        mountpoint = str(mount)
        fstype = "exfat"

    monkeypatch.setattr(volume_module.psutil, "disk_partitions", lambda all=False: [Partition()])
    monkeypatch.setattr(VolumeService, "_usage_total", staticmethod(lambda _mountpoint: 1))

    [volume] = VolumeService().list_volumes()

    assert volume.label is None
    assert "A9F1-F451" in volume.display_name


def test_linux_format_maps_exfat_to_udisks_filesystem_type(tmp_path: Path) -> None:
    filesystem_types: list[str] = []

    class FakeClient:
        def unmount_device(self, _device: str):  # noqa: ANN202
            return UDisksCallResult(True)

        def format_device(self, _device: str, filesystem_type: str, _label: str | None):  # noqa: ANN202
            filesystem_types.append(filesystem_type)
            return UDisksCallResult(True)

        def mount_device(self, _device: str):  # noqa: ANN202
            return UDisksCallResult(True)

    mount = tmp_path / "card"
    mount.mkdir()
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=mount,
        device_path="/dev/sdb1",
        removable=True,
        platform="linux",
    )
    service = FormatService(linux_udisks_factory=FakeClient)  # type: ignore[arg-type]

    result = service._format_linux(volume, "exFAT")  # noqa: SLF001

    assert result.ok
    assert filesystem_types == ["exfat"]


def test_linux_format_safety_does_not_treat_plain_mnt_as_removable(tmp_path: Path) -> None:
    volume = VolumeInfo(
        id="disk",
        display_name="Disk",
        mount_path=Path("/mnt/internal-disk"),
        device_path="/dev/sdb1",
        removable=None,
        platform="linux",
    )

    error = FormatService._linux_format_safety_error(volume)  # noqa: SLF001

    assert error is not None
    assert "/media or /run/media" in error


def test_volume_match_warning_reports_unavailable_and_ambiguous_values(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import volume_match_warning

    a = VolumeInfo(id="a", display_name="A", mount_path=tmp_path / "a", uuid="SAME")
    b = VolumeInfo(id="b", display_name="B", mount_path=tmp_path / "b", uuid="SAME")

    assert volume_match_warning([a], a, "label") == "Label is unavailable for A."
    warning = volume_match_warning([a, b], a, "fs_uuid")
    assert warning is not None
    assert "shared by 2 mounted volumes" in warning


def test_hotplug_refresh_auto_pending_selects_default_when_it_appears(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import (
        AUTO_PENDING,
        AUTO_SELECTED,
        resolve_refreshed_volume_ids,
    )

    primary = VolumeInfo(
        id="new-primary",
        display_name="Primary",
        mount_path=tmp_path / "primary",
        device_serial="Sony_DSC_D080E039A120-0:0",
    )
    secondary = VolumeInfo(
        id="new-secondary",
        display_name="Secondary",
        mount_path=tmp_path / "secondary",
        device_serial="Sony_DSC_D080E039A120-0:1",
    )

    assert resolve_refreshed_volume_ids(
        [secondary, primary],
        "",
        "",
        AUTO_PENDING,
        AUTO_PENDING,
        "",
        "",
        VolumeMatch("device_serial", "Sony_DSC_D080E039A120-0:0"),
        VolumeMatch("device_serial", "Sony_DSC_D080E039A120-0:1"),
    ) == ("new-primary", "new-secondary", AUTO_SELECTED, AUTO_SELECTED)


def test_hotplug_refresh_keeps_user_selected_volume_when_default_appears(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import (
        AUTO_PENDING,
        AUTO_SELECTED,
        USER_SELECTED,
        resolve_refreshed_volume_ids,
    )

    manual = VolumeInfo(
        id="manual", display_name="Manual", mount_path=tmp_path / "manual", device_serial="manual"
    )
    default = VolumeInfo(
        id="default",
        display_name="Default",
        mount_path=tmp_path / "default",
        device_serial="default",
    )

    assert resolve_refreshed_volume_ids(
        [manual, default],
        "manual",
        "",
        USER_SELECTED,
        AUTO_PENDING,
        "",
        "",
        VolumeMatch("device_serial", "default"),
        VolumeMatch("device_serial", ""),
    ) == ("manual", "", USER_SELECTED, AUTO_PENDING)

    assert resolve_refreshed_volume_ids(
        [manual, default],
        "default",
        "",
        AUTO_SELECTED,
        AUTO_PENDING,
        "",
        "",
        VolumeMatch("device_serial", "default"),
        VolumeMatch("device_serial", ""),
    ) == ("default", "", AUTO_SELECTED, AUTO_PENDING)


def test_hotplug_refresh_user_cleared_secondary_stays_none(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import (
        AUTO_SELECTED,
        USER_CLEARED,
        resolve_refreshed_volume_ids,
    )

    primary = VolumeInfo(id="primary", display_name="Primary", mount_path=tmp_path / "primary")
    secondary = VolumeInfo(
        id="secondary",
        display_name="Secondary",
        mount_path=tmp_path / "secondary",
        device_serial="secondary",
    )

    assert resolve_refreshed_volume_ids(
        [primary, secondary],
        "primary",
        "",
        AUTO_SELECTED,
        USER_CLEARED,
        "primary",
        "",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", "secondary"),
    ) == ("primary", "", AUTO_SELECTED, USER_CLEARED)


def test_hotplug_refresh_missing_user_selection_returns_to_auto_pending(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import (
        AUTO_PENDING,
        USER_SELECTED,
        resolve_refreshed_volume_ids,
    )

    other = VolumeInfo(id="other", display_name="Other", mount_path=tmp_path / "other")

    assert resolve_refreshed_volume_ids(
        [other],
        "missing-user-selection",
        "",
        USER_SELECTED,
        AUTO_PENDING,
        "",
        "",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", ""),
    ) == ("", "", AUTO_PENDING, AUTO_PENDING)


def test_hotplug_refresh_preserves_user_selected_after_format_on_same_device(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import USER_SELECTED, resolve_refreshed_volume_ids

    old_volume = VolumeInfo(
        id="uuid:OLD:/run/media/olli/OLD",
        display_name="Old",
        mount_path=tmp_path / "OLD",
        uuid="OLD",
        device_path="/dev/sdb1",
        device_serial="Sony_DSC_D080E039A120-0:1",
    )
    refreshed = VolumeInfo(
        id="uuid:NEW:/run/media/olli/NEW",
        display_name="New",
        mount_path=tmp_path / "NEW",
        uuid="NEW",
        device_path="/dev/sdb1",
        device_serial="Sony_DSC_D080E039A120-0:1",
    )

    assert resolve_refreshed_volume_ids(
        [refreshed],
        old_volume.id,
        "",
        USER_SELECTED,
        "auto_pending",
        "",
        "",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", ""),
        previous_primary_volume=old_volume,
    ) == (refreshed.id, "", USER_SELECTED, "auto_pending")


def test_hotplug_refresh_preserves_auto_selected_after_format_on_same_device(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import AUTO_SELECTED, resolve_refreshed_volume_ids

    old_volume = VolumeInfo(
        id="uuid:OLD:/run/media/olli/OLD",
        display_name="Old",
        mount_path=tmp_path / "OLD",
        uuid="OLD",
        device_path="/dev/sdb1",
        device_serial="Sony_DSC_D080E039A120-0:1",
    )
    refreshed = VolumeInfo(
        id="uuid:NEW:/run/media/olli/NEW",
        display_name="New",
        mount_path=tmp_path / "NEW",
        uuid="NEW",
        device_path="/dev/sdb1",
        device_serial="Sony_DSC_D080E039A120-0:1",
    )

    assert resolve_refreshed_volume_ids(
        [refreshed],
        old_volume.id,
        "",
        AUTO_SELECTED,
        "auto_pending",
        "",
        "",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", ""),
        previous_primary_volume=old_volume,
    ) == (refreshed.id, "", AUTO_SELECTED, "auto_pending")


def test_hotplug_refresh_does_not_preserve_ambiguous_previous_identifier(tmp_path: Path) -> None:
    from cameracopy2.ui.volume_selection import (
        AUTO_PENDING,
        USER_SELECTED,
        resolve_refreshed_volume_ids,
    )

    old_volume = VolumeInfo(
        id="old",
        display_name="Old",
        mount_path=tmp_path / "old",
        partition_uuid="00000000-01",
    )
    a = VolumeInfo(
        id="a", display_name="A", mount_path=tmp_path / "a", partition_uuid="00000000-01"
    )
    b = VolumeInfo(
        id="b", display_name="B", mount_path=tmp_path / "b", partition_uuid="00000000-01"
    )

    assert resolve_refreshed_volume_ids(
        [a, b],
        "old",
        "",
        USER_SELECTED,
        AUTO_PENDING,
        "",
        "",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", ""),
        previous_primary_volume=old_volume,
    ) == ("", "", AUTO_PENDING, AUTO_PENDING)


def test_hotplug_refresh_does_not_preserve_reused_id_for_replacement_card(
    tmp_path: Path,
) -> None:
    from cameracopy2.ui.volume_selection import (
        AUTO_PENDING,
        USER_SELECTED,
        resolve_refreshed_volume_ids,
    )

    selected = VolumeInfo(
        id="reused-id",
        display_name="Selected",
        mount_path=tmp_path / "card",
        device_path="/dev/sdb1",
        uuid="DUPLICATED",
        device_serial="CARD-A",
        size_bytes=64_000,
    )
    replacement = VolumeInfo(
        id="reused-id",
        display_name="Replacement",
        mount_path=tmp_path / "card",
        device_path="/dev/sdb1",
        uuid="DUPLICATED",
        device_serial="CARD-B",
        size_bytes=64_000,
    )

    assert resolve_refreshed_volume_ids(
        [replacement],
        selected.id,
        "",
        USER_SELECTED,
        AUTO_PENDING,
        "",
        "",
        VolumeMatch("device_serial", ""),
        VolumeMatch("device_serial", ""),
        previous_primary_volume=selected,
    ) == ("", "", AUTO_PENDING, AUTO_PENDING)


def test_config_loads_durable_writes_setting() -> None:
    config = config_from_dict({"version": 2, "durablewrites": True})
    assert config.durablewrites is True


def test_durable_writes_calls_file_and_directory_fsync(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.core import copy_engine as copy_module

    card = tmp_path / "card"
    dest = tmp_path / "pictures"
    card.mkdir()
    dest.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"image")
    file_syncs: list[int] = []
    directory_syncs: list[Path] = []

    monkeypatch.setattr(copy_module.os, "fsync", file_syncs.append)
    monkeypatch.setattr(
        CopyEngine,
        "_fsync_directory_best_effort",
        staticmethod(directory_syncs.append),
    )
    config = CameraCopyConfig(
        source="",
        destination=str(dest),
        includedfiles=["*.JPG"],
        datetimestring="",
        durablewrites=True,
    )
    report = CopyEngine().run(CopyJob(primary=make_volume(card), secondary=None, config=config))

    assert report.completed_cleanly
    assert file_syncs
    assert directory_syncs == [dest]


def test_verified_copy_hashes_source_during_transfer_without_rereading_it(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.core import copy_engine as copy_module

    card = tmp_path / "card"
    destination = tmp_path / "pictures"
    card.mkdir()
    destination.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"verified image")

    hashed_paths: list[Path] = []
    real_sha256_file = copy_module.sha256_file

    def tracked_sha256(path: Path, *args, **kwargs) -> str:  # noqa: ANN002, ANN003
        hashed_paths.append(path)
        return real_sha256_file(path, *args, **kwargs)

    monkeypatch.setattr(copy_module, "sha256_file", tracked_sha256)
    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        durablewrites=False,
    )

    report = CopyEngine().run(CopyJob(make_volume(card), None, config))

    assert report.completed_cleanly
    assert (destination / source.name).read_bytes() == source.read_bytes()
    assert source not in hashed_paths
    assert len(hashed_paths) == 1
    assert hashed_paths[0].name.endswith(".cameracopy.tmp")


def test_copy_fails_if_source_changes_during_destination_verification(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.core import copy_engine as copy_module

    card = tmp_path / "card"
    destination = tmp_path / "pictures"
    card.mkdir()
    destination.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"verified image")
    real_sha256_file = copy_module.sha256_file

    def change_source_then_hash(path: Path, *args, **kwargs) -> str:  # noqa: ANN002, ANN003
        with source.open("ab") as handle:
            handle.write(b"changed")
        return real_sha256_file(path, *args, **kwargs)

    monkeypatch.setattr(copy_module, "sha256_file", change_source_then_hash)
    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        durablewrites=False,
    )

    report = CopyEngine().run(CopyJob(make_volume(card), None, config))

    assert report.has_failures
    assert not (destination / source.name).exists()
    assert any(result.error == "source changed during copy" for result in report.results)
    assert not list(destination.glob("*.cameracopy.tmp"))


def test_copy_fails_if_source_changes_during_transfer(tmp_path: Path) -> None:
    card = tmp_path / "card"
    destination = tmp_path / "pictures"
    card.mkdir()
    destination.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"x" * (3 * 1024 * 1024))
    changed = False

    def change_source(done: int, _total: int, _index: int) -> None:
        nonlocal changed
        if not changed and done >= 1024 * 1024:
            with source.open("ab") as handle:
                handle.write(b"changed")
            changed = True

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.JPG"],
        datetimestring="",
        checkhash=True,
        durablewrites=False,
    )
    report = CopyEngine().run(
        CopyJob(make_volume(card), None, config),
        CopyCallbacks(byte_progress=change_source),
    )

    assert changed
    assert report.has_failures
    assert not (destination / source.name).exists()
    assert any(result.error == "source changed during copy" for result in report.results)
    assert not list(destination.glob("*.cameracopy.tmp"))


def test_config_read_error_preserves_existing_file(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    config_path = tmp_path / "cameracopy.json"
    original = '{"version": 2, "destination": "pictures"}'
    config_path.write_text(original, encoding="utf-8")
    real_read_text = Path.read_text

    def failing_read_text(path: Path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        if path == config_path:
            raise OSError("temporarily unavailable")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", failing_read_text)

    try:
        load_config(config_path)
    except ConfigReadError as exc:
        assert exc.path == config_path
    else:
        raise AssertionError("an unreadable existing config must not be replaced")

    monkeypatch.setattr(Path, "read_text", real_read_text)
    assert config_path.read_text(encoding="utf-8") == original
    assert not (tmp_path / "cameracopy.json.bak").exists()


def test_strict_copy_source_match_rejects_reused_mount_path(tmp_path: Path) -> None:
    selected = VolumeInfo(
        id="old",
        display_name="Selected",
        mount_path=tmp_path / "card",
        uuid="CARD-A",
        device_serial="SERIAL-A",
        size_bytes=64_000,
    )
    replacement = VolumeInfo(
        id="new",
        display_name="Replacement",
        mount_path=tmp_path / "card",
        uuid="CARD-B",
        device_serial="SERIAL-B",
        size_bytes=64_000,
    )

    assert VolumeService.find_matching_copy_source(selected, [replacement]) is None


def test_strict_copy_source_match_uses_mount_only_without_strong_identity(
    tmp_path: Path,
) -> None:
    selected = VolumeInfo(
        id="old",
        display_name="Selected",
        mount_path=tmp_path / "card",
        size_bytes=64_000,
    )
    current = VolumeInfo(
        id="new",
        display_name="Current",
        mount_path=tmp_path / "card",
        size_bytes=64_000,
    )

    assert VolumeService.find_matching_copy_source(selected, [current]) is current


def test_strict_copy_source_match_ignores_shared_partition_uuid(
    tmp_path: Path,
) -> None:
    selected = VolumeInfo(
        id="selected",
        display_name="Selected",
        mount_path=tmp_path / "first",
        device_path="/dev/sdb1",
        uuid="6A6A-E415",
        partition_uuid="00000000-01",
        size_bytes=74_500,
    )
    current = VolumeInfo(
        id="current",
        display_name="Current",
        mount_path=tmp_path / "first",
        device_path="/dev/sdb1",
        uuid="6A6A-E415",
        partition_uuid="00000000-01",
        size_bytes=74_500,
    )
    other_card = VolumeInfo(
        id="other",
        display_name="Other card",
        mount_path=tmp_path / "second",
        device_path="/dev/sdc1",
        uuid="FB7E-FAF6",
        partition_uuid="00000000-01",
        size_bytes=119_100,
    )

    assert VolumeService.find_matching_copy_source(
        selected, [other_card, current]
    ) is current


def test_strict_copy_source_match_does_not_use_device_identity_as_partition_identity(
    tmp_path: Path,
) -> None:
    selected = VolumeInfo(
        id="first",
        display_name="Selected",
        mount_path=tmp_path / "first",
        device_serial="CAMERA-CARD-READER",
        hardware_path="USB-PORT-1",
        size_bytes=64_000,
    )
    other_partition = VolumeInfo(
        id="second",
        display_name="Other partition",
        mount_path=tmp_path / "second",
        device_serial="CAMERA-CARD-READER",
        hardware_path="USB-PORT-1",
        size_bytes=64_000,
    )

    assert VolumeService.find_matching_copy_source(selected, [other_partition]) is None


def test_same_mounted_volume_check_uses_attachment_not_uuid_metadata(
    tmp_path: Path,
) -> None:
    first = VolumeInfo(
        id="first",
        display_name="First",
        mount_path=tmp_path / "first",
        device_path="/dev/sdb1",
        uuid="SAME-FS-UUID",
        partition_uuid="00000000-01",
    )
    second = VolumeInfo(
        id="second",
        display_name="Second",
        mount_path=tmp_path / "second",
        device_path="/dev/sdc1",
        uuid="SAME-FS-UUID",
        partition_uuid="00000000-01",
    )
    duplicate = VolumeInfo(
        id="duplicate",
        display_name="Duplicate",
        mount_path=tmp_path / "duplicate",
        device_path="/dev/sdb1",
    )

    assert not volumes_refer_to_same_mounted_volume(first, second)
    assert volumes_refer_to_same_mounted_volume(first, duplicate)


def test_copy_engine_preserves_partial_results_after_unexpected_failure(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    card = tmp_path / "card"
    destination = tmp_path / "pictures"
    card.mkdir()
    destination.mkdir()
    source = card / "DSC0001.JPG"
    source.write_bytes(b"image")
    engine = CopyEngine()

    def failing_run_volume(
        _volume,
        _config,
        _autoremove,
        _callbacks,
        report,
        _cancel_event,
        **_kwargs,
    ):  # noqa: ANN001, ANN202
        report.add_result(
            FileCopyResult(
                source=source,
                destination=destination / source.name,
                action="copied",
                copy_mode="copied",
            )
        )
        raise RuntimeError("unexpected failure")

    monkeypatch.setattr(engine, "_run_volume", failing_run_volume)
    report = engine.run(
        CopyJob(
            primary=make_volume(card),
            secondary=None,
            config=CameraCopyConfig(source="", destination=str(destination)),
        )
    )

    assert len(report.results) == 2
    assert report.results[0].action == "copied"
    assert report.results[1].reason == "internal copy error"
    assert "unexpected failure" in (report.results[1].error or "")


def test_copy_operation_logs_are_kept_beside_windows_settings_and_limited_to_five(
    tmp_path: Path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2 import operation_log as operation_log_module

    class FixedDateTime:
        @classmethod
        def now(cls) -> datetime:
            return datetime(2026, 8, 4, 7, 16)

    monkeypatch.setattr(operation_log_module.sys, "platform", "win32")
    monkeypatch.setattr(operation_log_module, "datetime", FixedDateTime)
    config_path = tmp_path / "CameraCopy" / "cameracopy.json"

    assert operation_log_directory(config_path) == config_path.parent / "logs"

    for index in range(6):
        operation_log = CopyOperationLog(config_path)
        operation_log.info("operation %s", index)
        operation_log.close()

    logs = sorted((config_path.parent / "logs").glob("copy-*.log"))
    assert len(logs) == 5
    assert all("CameraCopy" in path.read_text(encoding="utf-8") for path in logs)


def test_linux_operation_logs_use_xdg_state_directory(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2 import operation_log as operation_log_module

    state_home = tmp_path / "state"
    monkeypatch.setattr(operation_log_module.sys, "platform", "linux")
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))

    assert operation_log_directory() == state_home / "CameraCopy" / "logs"


def test_durable_writes_are_enabled_by_default() -> None:
    assert CameraCopyConfig().durablewrites is True
