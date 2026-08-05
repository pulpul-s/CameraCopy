from __future__ import annotations

from pathlib import Path
from threading import Event

from cameracopy2.core.copy_engine import CopyEngine
from cameracopy2.core.copy_messages import result_message
from cameracopy2.core.format_warnings import (
    format_risk_warning_paragraphs,
    irreversible_format_warning,
)
from cameracopy2.core.log_messages import (
    LogMessageType,
    classify_file_result,
    classify_log_message,
)
from cameracopy2.models import (
    CameraCopyConfig,
    CopyCallbacks,
    CopyJob,
    FileCopyResult,
    VolumeInfo,
)


def test_log_message_classification() -> None:
    assert classify_log_message("Scanning /media/camera") is LogMessageType.INFORMATION
    assert classify_log_message("COPIED: DSC0001.ARW") is LogMessageType.COPIED
    assert classify_log_message("CLONE VERIFIED: DSC0001.ARW") is LogMessageType.COPIED
    assert (
        classify_log_message("VOLUME MISMATCH — COPIED: DSC0002.ARW")
        is LogMessageType.CONFIRMED
    )
    assert classify_log_message("VOLUME MISMATCH: DSC0002.ARW") is LogMessageType.WARNING
    assert (
        classify_log_message("CLONE MISMATCH — KEPT FIRST-SOURCE: DSC0003.ARW")
        is LogMessageType.CONFIRMED
    )
    assert classify_log_message("FAILED: copy error") is LogMessageType.ERROR
    assert classify_log_message("CLONE FAILED: missing") is LogMessageType.ERROR
    assert classify_log_message("FORMAT FAILED: mount error") is LogMessageType.ERROR
    assert classify_log_message("Status: finished with failures") is LogMessageType.ERROR
    assert classify_log_message("Formatted /dev/sdb1 as exFAT") is LogMessageType.COPIED


def _run_empty_copy(tmp_path: Path, minrating: int) -> list[str]:
    source_mount = tmp_path / "source"
    (source_mount / "DCIM").mkdir(parents=True)
    destination = tmp_path / "destination"
    logs: list[str] = []
    config = CameraCopyConfig(
        destination=str(destination),
        minrating=minrating,
    )
    job = CopyJob(
        primary=VolumeInfo(
            id="source",
            display_name="Source",
            mount_path=source_mount,
        ),
        secondary=None,
        config=config,
    )
    CopyEngine().run(job, CopyCallbacks(log=logs.append), Event())
    return logs


def test_start_summary_includes_enabled_minimum_rating(tmp_path: Path) -> None:
    logs = _run_empty_copy(tmp_path, 4)
    assert "Minimum rating: 4" in logs


def test_start_summary_omits_disabled_minimum_rating(tmp_path: Path) -> None:
    logs = _run_empty_copy(tmp_path, 0)
    assert not any(line.startswith("Minimum rating:") for line in logs)


def test_structured_result_classification_covers_new_outcomes(tmp_path: Path) -> None:
    source = tmp_path / "source.JPG"
    destination = tmp_path / "destination.JPG"

    cases = {
        "volume_mismatch_copied": LogMessageType.CONFIRMED,
        "volume_mismatch_kept_both": LogMessageType.CONFIRMED,
        "volume_mismatch_replaced": LogMessageType.CONFIRMED,
        "volume_mismatch_verified_existing": LogMessageType.COPIED,
        "volume_mismatch_kept_existing": LogMessageType.WARNING,
        "volume_mismatch_skipped": LogMessageType.WARNING,
        "clone_mismatch_kept": LogMessageType.CONFIRMED,
        "clone_mismatch_replaced": LogMessageType.CONFIRMED,
        "clone_mismatch_skipped": LogMessageType.CONFIRMED,
        "overwrote": LogMessageType.CONFIRMED,
        "verified_existing": LogMessageType.COPIED,
    }
    for copy_mode, expected in cases.items():
        action = "skipped" if copy_mode.endswith(("skipped", "kept_existing")) else "copied"
        result = FileCopyResult(
            source=source,
            destination=destination,
            action=action,  # type: ignore[arg-type]
            copy_mode=copy_mode,  # type: ignore[arg-type]
        )
        assert classify_file_result(result) is expected

    rating_skip = FileCopyResult(
        source=source,
        destination=None,
        action="skipped",
        reason="rating 2 below minimum 4",
    )
    assert classify_file_result(rating_skip) is LogMessageType.INFORMATION


def _result(
    tmp_path: Path,
    copy_mode: str,
    *,
    destination_name: str = "DSC04755.ARW",
    action: str = "copied",
) -> FileCopyResult:
    return FileCopyResult(
        source=tmp_path / "second" / "DSC04755.ARW",
        destination=tmp_path / "pictures" / destination_name,
        action=action,  # type: ignore[arg-type]
        size_bytes=1,
        copy_mode=copy_mode,  # type: ignore[arg-type]
        first_source_destination=tmp_path / "pictures" / "DSC04755.ARW",
        first_source_size_bytes=36_144 * 1024,
    )


def test_clone_mismatch_keep_both_message_describes_both_versions(tmp_path: Path) -> None:
    message = result_message(  # noqa: SLF001
        _result(tmp_path, "clone_mismatch_kept", destination_name="DSC04755_001.ARW")
    )

    assert message.splitlines() == [
        "CLONE MISMATCH — KEPT BOTH:",
        f"    First-source copy remains: {tmp_path / 'pictures' / 'DSC04755.ARW'} (36,144 KB)",
        (
            f"    Second-source copied: {tmp_path / 'second' / 'DSC04755.ARW'} -> "
            f"{tmp_path / 'pictures' / 'DSC04755_001.ARW'} (1 byte)"
        ),
    ]


def test_clone_mismatch_keep_first_message_has_no_copy_arrow(tmp_path: Path) -> None:
    message = result_message(  # noqa: SLF001
        _result(tmp_path, "clone_mismatch_skipped", action="skipped")
    )

    assert message.splitlines() == [
        "CLONE MISMATCH — KEPT FIRST-SOURCE:",
        f"    First-source copy remains: {tmp_path / 'pictures' / 'DSC04755.ARW'} (36,144 KB)",
        f"    Second-source not copied: {tmp_path / 'second' / 'DSC04755.ARW'} (1 byte)",
    ]
    assert " -> " not in message


def test_clone_mismatch_use_second_message_identifies_replacement(tmp_path: Path) -> None:
    message = result_message(  # noqa: SLF001
        _result(tmp_path, "clone_mismatch_replaced")
    )

    assert message.splitlines() == [
        "CLONE MISMATCH — USED SECOND-SOURCE:",
        f"    First-source copy replaced: {tmp_path / 'pictures' / 'DSC04755.ARW'} (36,144 KB)",
        (
            f"    Second-source copied: {tmp_path / 'second' / 'DSC04755.ARW'} -> "
            f"{tmp_path / 'pictures' / 'DSC04755.ARW'} (1 byte)"
        ),
    ]


def test_identical_volume_mismatch_message_names_destination(tmp_path: Path) -> None:
    result = FileCopyResult(
        source=tmp_path / "second" / "EXTRA.JPG",
        destination=tmp_path / "pictures" / "EXTRA.JPG",
        action="verified_existing",
        size_bytes=5,
        copy_mode="volume_mismatch_verified_existing",
        volume_mismatch=True,
    )

    message = result_message(result)  # noqa: SLF001

    assert message.splitlines() == [
        "VOLUME MISMATCH — FOUND IDENTICAL COPY IN DESTINATION:",
        f"    Second-source file: {tmp_path / 'second' / 'EXTRA.JPG'} (5 bytes)",
        f"    Existing destination: {tmp_path / 'pictures' / 'EXTRA.JPG'} (5 bytes)",
    ]
    assert classify_log_message(message) is LogMessageType.COPIED


def test_single_byte_uses_singular_unit(tmp_path: Path) -> None:
    result = FileCopyResult(
        source=tmp_path / "one.bin",
        destination=tmp_path / "copy.bin",
        action="copied",
        size_bytes=1,
        copy_mode="copied",
    )

    assert result_message(result).endswith("(1 byte)")  # noqa: SLF001


def test_all_resolved_clone_mismatch_choices_use_confirmed_color(tmp_path: Path) -> None:
    for copy_mode, action in (
        ("clone_mismatch_kept", "copied"),
        ("clone_mismatch_replaced", "copied"),
        ("clone_mismatch_skipped", "skipped"),
    ):
        result = FileCopyResult(
            source=tmp_path / "second.ARW",
            destination=tmp_path / "destination.ARW",
            action=action,  # type: ignore[arg-type]
            copy_mode=copy_mode,  # type: ignore[arg-type]
        )
        assert classify_file_result(result) is LogMessageType.CONFIRMED

    assert (
        classify_log_message("CLONE MISMATCH — KEPT FIRST-SOURCE:")
        is LogMessageType.CONFIRMED
    )


def test_format_warning_text_is_specific_and_pluralized() -> None:
    paragraphs = format_risk_warning_paragraphs(
        {
            "rating_excluded": 2,
            "clone_kept_first_source": 1,
            "clone_used_second_source": 3,
            "volume_mismatch_skipped": 1,
            "volume_mismatch_kept_existing": 2,
        }
    )

    assert paragraphs == [
        "2 files were excluded by the minimum rating and were not copied.\n"
        "Formatting will erase them.",
        "1 clone mismatch was resolved by keeping the first-source copy.\n"
        "The differing second-source file remains only on this volume and will be "
        "erased.",
        "3 clone mismatches were resolved by using the second-source copies.\n"
        "The differing first-source files remain only on this volume and will be erased.",
        "1 file found only on the second-source volume was skipped by your choice.\n"
        "It remains only on this volume and will be erased.",
        "2 files found only on the second-source volume were not copied because the "
        "existing destinations were kept.\n"
        "The differing files remain only on this volume and will be erased.",
    ]


def test_irreversible_warning_mentions_unscanned_and_excluded_files() -> None:
    warning = irreversible_format_warning()

    assert "FILES OUTSIDE THE CONFIGURED SOURCE FOLDER" in warning
    assert "FILES EXCLUDED FROM COPYING" in warning
    assert warning.endswith("THIS IS IRREVERSIBLE.")


def test_skipped_volume_mismatch_warning_names_the_user_choice() -> None:
    singular = format_risk_warning_paragraphs({"volume_mismatch_skipped": 1})
    plural = format_risk_warning_paragraphs({"volume_mismatch_skipped": 2})

    assert singular == [
        "1 file found only on the second-source volume was skipped by your choice.\n"
        "It remains only on this volume and will be erased."
    ]
    assert plural == [
        "2 files found only on the second-source volume were skipped by your choice.\n"
        "They remain only on this volume and will be erased."
    ]
