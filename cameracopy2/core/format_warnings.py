from __future__ import annotations

from collections.abc import Mapping

from cameracopy2.models import FormatRiskKind


def format_risk_warning_paragraphs(
    counts: Mapping[FormatRiskKind, int],
) -> list[str]:
    """Describe files that remain only on the volume about to be formatted."""
    paragraphs: list[str] = []

    rating_count = counts.get("rating_excluded", 0)
    if rating_count:
        paragraphs.append(_rating_warning(rating_count))

    kept_first_count = counts.get("clone_kept_first_source", 0)
    if kept_first_count:
        paragraphs.append(_clone_kept_first_warning(kept_first_count))

    used_second_count = counts.get("clone_used_second_source", 0)
    if used_second_count:
        paragraphs.append(_clone_used_second_warning(used_second_count))

    volume_skipped_count = counts.get("volume_mismatch_skipped", 0)
    if volume_skipped_count:
        paragraphs.append(_volume_mismatch_skipped_warning(volume_skipped_count))

    kept_existing_count = counts.get("volume_mismatch_kept_existing", 0)
    if kept_existing_count:
        paragraphs.append(_volume_mismatch_kept_existing_warning(kept_existing_count))

    return paragraphs


def irreversible_format_warning() -> str:
    return (
        "THIS WILL REMOVE ALL DATA ON THE SELECTED PARTITION, INCLUDING FILES "
        "OUTSIDE THE CONFIGURED SOURCE FOLDER AND FILES EXCLUDED FROM COPYING. "
        "THIS IS IRREVERSIBLE."
    )


def _rating_warning(count: int) -> str:
    if count == 1:
        return (
            "1 file was excluded by the minimum rating and was not copied.\n"
            "Formatting will erase it."
        )
    return (
        f"{count} files were excluded by the minimum rating and were not copied.\n"
        "Formatting will erase them."
    )


def _clone_kept_first_warning(count: int) -> str:
    if count == 1:
        return (
            "1 clone mismatch was resolved by keeping the first-source copy.\n"
            "The differing second-source file remains only on this volume and will "
            "be erased."
        )
    return (
        f"{count} clone mismatches were resolved by keeping the first-source copies.\n"
        "The differing second-source files remain only on this volume and will be "
        "erased."
    )


def _clone_used_second_warning(count: int) -> str:
    if count == 1:
        return (
            "1 clone mismatch was resolved by using the second-source copy.\n"
            "The differing first-source file remains only on this volume and will be "
            "erased."
        )
    return (
        f"{count} clone mismatches were resolved by using the second-source copies.\n"
        "The differing first-source files remain only on this volume and will be "
        "erased."
    )


def _volume_mismatch_skipped_warning(count: int) -> str:
    if count == 1:
        return (
            "1 file found only on the second-source volume was skipped by your choice.\n"
            "It remains only on this volume and will be erased."
        )
    return (
        f"{count} files found only on the second-source volume were skipped by your "
        "choice.\n"
        "They remain only on this volume and will be erased."
    )


def _volume_mismatch_kept_existing_warning(count: int) -> str:
    if count == 1:
        return (
            "1 file found only on the second-source volume was not copied because "
            "the existing destination was kept.\n"
            "The differing file remains only on this volume and will be erased."
        )
    return (
        f"{count} files found only on the second-source volume were not copied "
        "because the existing destinations were kept.\n"
        "The differing files remain only on this volume and will be erased."
    )
