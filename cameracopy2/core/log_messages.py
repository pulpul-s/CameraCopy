from __future__ import annotations

from enum import Enum, auto

from cameracopy2.models import FileCopyResult


class LogMessageType(Enum):
    INFORMATION = auto()
    COPIED = auto()
    CONFIRMED = auto()
    WARNING = auto()
    ERROR = auto()


def classify_file_result(result: FileCopyResult) -> LogMessageType:
    """Classify a structured copy result without parsing its display text."""
    if result.failed:
        return LogMessageType.ERROR

    if result.copy_mode in {
        "overwrote",
        "renamed_copy",
        "clone_mismatch_kept",
        "clone_mismatch_replaced",
        "clone_mismatch_skipped",
        "volume_mismatch_copied",
        "volume_mismatch_kept_both",
        "volume_mismatch_replaced",
    }:
        return LogMessageType.CONFIRMED

    if result.copy_mode in {
        "copied",
        "verified_existing",
        "clone_verified",
        "volume_mismatch_verified_existing",
    }:
        return LogMessageType.COPIED

    if result.copy_mode in {
        "volume_mismatch_kept_existing",
        "volume_mismatch_skipped",
    }:
        return LogMessageType.WARNING

    if result.action == "skipped":
        reason = (result.reason or "").lower()
        if "rating " in reason and " below minimum " in reason:
            return LogMessageType.INFORMATION
        return LogMessageType.WARNING

    if result.action in {"copied", "removed", "verified_existing"}:
        return LogMessageType.COPIED
    return LogMessageType.INFORMATION


def classify_log_message(message: str) -> LogMessageType:
    """Classify free-form status text and provide a fallback for old log lines."""
    text = message.strip()
    upper = text.upper()

    if upper.startswith(
        (
            "FAILED:",
            "CLONE FAILED:",
            "FORMAT FAILED:",
            "INTERNAL ERROR:",
            "FORMATTING WORKER FAILED",
            "FORMATTING FINISHED WITH FAILURE",
        )
    ) or upper == "STATUS: FINISHED WITH FAILURES":
        return LogMessageType.ERROR

    if upper.startswith(
        (
            "VOLUME MISMATCH:",
            "CLONE MISMATCH:",
            "VOLUME MISMATCH — KEPT EXISTING:",
            "VOLUME MISMATCH — SKIPPED:",
            "FORMAT SKIPPED:",
            "FORMATTING SKIPPED",
            "FORMATTING CANCELLED",
            "FORMATTING FINISHED, BUT",
        )
    ) or upper == "STATUS: CANCELLED":
        return LogMessageType.WARNING

    if upper.startswith(
        (
            "RENAMED COPY:",
            "REPLACED:",
            "VOLUME MISMATCH — COPIED:",
            "VOLUME MISMATCH — KEPT BOTH:",
            "VOLUME MISMATCH — REPLACED:",
            "CLONE MISMATCH — KEPT BOTH:",
            "CLONE MISMATCH — KEPT FIRST-SOURCE:",
            "CLONE MISMATCH — USED SECOND-SOURCE:",
        )
    ):
        return LogMessageType.CONFIRMED

    if upper.startswith(
        (
            "COPIED:",
            "VERIFIED EXISTING:",
            "CLONE VERIFIED:",
            "VOLUME MISMATCH — FOUND IDENTICAL COPY IN DESTINATION:",
            "FORMATTED ",
        )
    ) or upper in {
        "STATUS: FINISHED SUCCESSFULLY",
        "FORMATTING FINISHED SUCCESSFULLY.",
    }:
        return LogMessageType.COPIED

    if upper.startswith("SKIPPED:"):
        if "RATING " in upper and " BELOW MINIMUM " in upper:
            return LogMessageType.INFORMATION
        return LogMessageType.WARNING

    return LogMessageType.INFORMATION
