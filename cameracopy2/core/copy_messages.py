from __future__ import annotations

from pathlib import Path

from cameracopy2.models import FileCopyResult

_REMOVED_STATUS_COPY_MODES = frozenset(
    {
        "copied",
        "overwrote",
        "renamed_copy",
        "verified_existing",
        "clone_verified",
        "clone_mismatch_kept",
        "clone_mismatch_replaced",
        "volume_mismatch_copied",
        "volume_mismatch_kept_both",
        "volume_mismatch_replaced",
        "volume_mismatch_verified_existing",
    }
)

_ROUTINE_SKIP_REASONS = frozenset(
    {
        "destination exists; user chose skip",
        "destination exists and overwrite is disabled",
        "volume mismatch; user chose skip",
        "volume mismatch; user kept existing destination",
        "clone mismatch; user kept first-source version",
    }
)

_VOLUME_STATUSES = {
    "volume_mismatch_copied": "VOLUME MISMATCH — COPIED",
    "volume_mismatch_kept_both": "VOLUME MISMATCH — KEPT BOTH",
    "volume_mismatch_replaced": "VOLUME MISMATCH — REPLACED",
    "volume_mismatch_kept_existing": "VOLUME MISMATCH — KEPT EXISTING",
    "volume_mismatch_verified_existing": (
        "VOLUME MISMATCH — FOUND IDENTICAL COPY IN DESTINATION"
    ),
    "volume_mismatch_skipped": "VOLUME MISMATCH — SKIPPED",
}


def format_log_file_size(size_bytes: int) -> str:
    """Format a file size for compact copy-log messages."""
    if size_bytes < 1024:
        unit = "byte" if size_bytes == 1 else "bytes"
        return f"{size_bytes} {unit}"
    return f"{round(size_bytes / 1024):,} KB"


def result_message(result: FileCopyResult) -> str:
    status = display_status(result)
    if result.copy_mode == "volume_mismatch_verified_existing":
        destination = result.destination or Path("<unavailable>")
        size = format_log_file_size(result.size_bytes)
        return (
            f"{status}:\n"
            f"    Second-source file: {result.source} ({size})\n"
            f"    Existing destination: {destination} ({size})"
        )
    if result.copy_mode in {
        "clone_mismatch_kept",
        "clone_mismatch_replaced",
        "clone_mismatch_skipped",
    }:
        return _clone_mismatch_result_message(result, status)

    destination = f" -> {result.destination}" if result.destination else ""
    return f"{status}: {result.source}{destination}{_result_detail(result)}"


def display_status(result: FileCopyResult) -> str:
    if result.failed:
        if result.reason == (
            "selected on the first-source volume, not found on the "
            "second-source volume"
        ):
            return "CLONE FAILED"
        return "FAILED"

    if result.copy_mode in _VOLUME_STATUSES:
        base = _VOLUME_STATUSES[result.copy_mode]
    elif result.copy_mode == "clone_verified":
        base = "CLONE VERIFIED"
    elif result.copy_mode == "clone_mismatch_kept":
        base = "CLONE MISMATCH — KEPT BOTH"
    elif result.copy_mode == "clone_mismatch_replaced":
        base = "CLONE MISMATCH — USED SECOND-SOURCE"
    elif result.copy_mode == "clone_mismatch_skipped":
        base = "CLONE MISMATCH — KEPT FIRST-SOURCE"
    elif result.action == "skipped":
        base = "SKIPPED"
    elif result.copy_mode == "overwrote":
        base = "REPLACED"
    elif result.copy_mode == "renamed_copy":
        base = "RENAMED COPY"
    elif result.copy_mode == "verified_existing":
        base = "VERIFIED EXISTING"
    elif result.copy_mode == "copied" or result.action in {"copied", "removed"}:
        base = "COPIED"
    else:
        base = result.action.upper()

    if result.action == "removed" and result.copy_mode in _REMOVED_STATUS_COPY_MODES:
        return f"{base} + REMOVED"
    return base


def _clone_mismatch_result_message(result: FileCopyResult, status: str) -> str:
    first_destination = result.first_source_destination or result.destination
    first_size_bytes = result.first_source_size_bytes or 0
    first_size = format_log_file_size(first_size_bytes)
    second_size = format_log_file_size(result.size_bytes)
    first_path = first_destination or Path("<unavailable>")

    if result.copy_mode == "clone_mismatch_kept":
        second_destination = result.destination or Path("<unavailable>")
        details = (
            f"    First-source copy remains: {first_path} ({first_size})\n"
            f"    Second-source copied: {result.source} -> {second_destination} "
            f"({second_size})"
        )
    elif result.copy_mode == "clone_mismatch_replaced":
        second_destination = result.destination or Path("<unavailable>")
        details = (
            f"    First-source copy replaced: {first_path} ({first_size})\n"
            f"    Second-source copied: {result.source} -> {second_destination} "
            f"({second_size})"
        )
    else:
        details = (
            f"    First-source copy remains: {first_path} ({first_size})\n"
            f"    Second-source not copied: {result.source} ({second_size})"
        )
    return f"{status}:\n{details}"


def _result_detail(result: FileCopyResult) -> str:
    parts = [format_log_file_size(result.size_bytes)]
    if result.failed:
        if result.reason:
            parts.append(result.reason)
        if (
            result.error
            and result.error != result.reason
            and not (
                result.reason == "SHA256 FAILED"
                and result.error == "verification failed"
            )
        ):
            parts.append(result.error)
    elif (
        result.action == "skipped"
        and result.reason
        and result.reason not in _ROUTINE_SKIP_REASONS
    ):
        parts.append(result.reason)
    return f" ({', '.join(parts)})"
