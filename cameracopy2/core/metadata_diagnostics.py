from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from cameracopy2.core.timestamp_resolver import (
    SONY_MP4_RE,
    read_sony_xml_creation_date,
    read_xmp_sidecar_timestamp,
    sony_xml_candidate,
)
from cameracopy2.models import CameraCopyConfig
from cameracopy2.services.metadata_service import ExifToolService


@dataclass(slots=True)
class MetadataDiagnostic:
    path: Path
    file_modified_time: datetime | None
    filesystem_created_time: datetime | None
    sony_xml_time: datetime | None
    embedded_time: datetime | None
    xmp_sidecar_time: datetime | None
    chosen_time: datetime | None
    chosen_source: str
    exiftool_metadata: dict[str, object]
    error: str | None = None


def diagnose_files(
    paths: list[Path],
    config: CameraCopyConfig,
    exiftool: ExifToolService | None = None,
) -> list[MetadataDiagnostic]:
    """Return timestamp diagnostics without changing copy behavior."""
    service = exiftool or ExifToolService()
    service.warm_cache(paths)
    return [_diagnose_file(Path(path), config, service) for path in paths]


def format_diagnostics_report(diagnostics: list[MetadataDiagnostic]) -> str:
    if not diagnostics:
        return "No files selected."

    lines: list[str] = []
    for index, item in enumerate(diagnostics, start=1):
        if index > 1:
            lines.append("")
        lines.append(f"File: {item.path}")
        if item.error:
            lines.append(f"ERROR: {item.error}")
            continue
        lines.append(f"File modified time:       {_format_dt(item.file_modified_time)}")
        lines.append(f"Filesystem created time:  {_format_dt(item.filesystem_created_time)}")
        lines.append(f"Sony XML timestamp:       {_format_dt(item.sony_xml_time)}")
        lines.append(f"Embedded metadata time:   {_format_dt(item.embedded_time)}")
        lines.append(f"XMP sidecar time:         {_format_dt(item.xmp_sidecar_time)}")
        lines.append(f"Chosen folder date:       {_format_dt(item.chosen_time)}")
        lines.append(f"Chosen source:            {_friendly_source(item.chosen_source)}")
        lines.append("")
        lines.extend(_format_exiftool_metadata(item.exiftool_metadata))
    return "\n".join(lines)


def _diagnose_file(
    path: Path, config: CameraCopyConfig, exiftool: ExifToolService
) -> MetadataDiagnostic:
    try:
        stat = path.stat()
        file_modified_time = datetime.fromtimestamp(stat.st_mtime)
        filesystem_created_time = _filesystem_created_time(stat)
        sony_xml_time = _sony_xml_time(path, config)
        embedded_time = exiftool.read_capture_timestamp(path)
        xmp_sidecar_time = read_xmp_sidecar_timestamp(path)
        exiftool_metadata = exiftool.read_all_json(path)

        chosen_time, chosen_source = _choose_folder_date(
            file_modified_time=file_modified_time,
            sony_xml_time=sony_xml_time,
            embedded_time=embedded_time,
            xmp_sidecar_time=xmp_sidecar_time,
            use_embedded_metadata=config.useembeddedmetadata,
        )
        return MetadataDiagnostic(
            path=path,
            file_modified_time=file_modified_time,
            filesystem_created_time=filesystem_created_time,
            sony_xml_time=sony_xml_time,
            embedded_time=embedded_time,
            xmp_sidecar_time=xmp_sidecar_time,
            chosen_time=chosen_time,
            chosen_source=chosen_source,
            exiftool_metadata=exiftool_metadata,
        )
    except OSError as exc:
        return MetadataDiagnostic(
            path=path,
            file_modified_time=None,
            filesystem_created_time=None,
            sony_xml_time=None,
            embedded_time=None,
            xmp_sidecar_time=None,
            chosen_time=None,
            chosen_source="unavailable",
            exiftool_metadata={},
            error=str(exc),
        )


def _filesystem_created_time(stat_result) -> datetime | None:  # noqa: ANN001
    birth_time = getattr(stat_result, "st_birthtime", None)
    if birth_time:
        return datetime.fromtimestamp(birth_time)
    if sys.platform == "win32":
        return datetime.fromtimestamp(stat_result.st_ctime)
    return None


def _sony_xml_time(path: Path, config: CameraCopyConfig) -> datetime | None:
    if not config.fixsonytimestamps:
        return None
    if path.suffix.lower() != ".mp4" or not SONY_MP4_RE.match(path.name):
        return None
    return read_sony_xml_creation_date(sony_xml_candidate(path))


def _choose_folder_date(
    *,
    file_modified_time: datetime,
    sony_xml_time: datetime | None,
    embedded_time: datetime | None,
    xmp_sidecar_time: datetime | None,
    use_embedded_metadata: bool,
) -> tuple[datetime, str]:
    if sony_xml_time is not None:
        return sony_xml_time, "sony_xml"
    if use_embedded_metadata and embedded_time is not None:
        return embedded_time, "embedded_metadata"
    if xmp_sidecar_time is not None:
        return xmp_sidecar_time, "xmp_sidecar"
    return file_modified_time, "file_mtime"


def _format_exiftool_metadata(metadata: dict[str, object]) -> list[str]:
    lines = ["ExifTool metadata:"]
    if not metadata:
        lines.append("  unavailable")
        return lines
    for key in sorted(metadata):
        value = metadata[key]
        lines.append(f"  {key}: {value}")
    return lines


def _format_dt(value: datetime | None) -> str:
    if value is None:
        return "unavailable"
    return value.isoformat(sep=" ", timespec="seconds")


def _friendly_source(source: str) -> str:
    labels = {
        "file_mtime": "file modified time",
        "sony_xml": "Sony XML",
        "embedded_metadata": "embedded metadata",
        "xmp_sidecar": "XMP sidecar",
        "unavailable": "unavailable",
    }
    return labels.get(source, source.replace("_", " "))
