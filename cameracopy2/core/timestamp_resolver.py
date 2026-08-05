from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from threading import Event

from dateutil import parser as date_parser

from cameracopy2.core.sidecars import SidecarIndex
from cameracopy2.services.metadata_service import ExifToolService

SONY_MP4_RE = re.compile(r"^C\d{4,}\.MP4$", re.IGNORECASE)
CREATION_DATE_RE = re.compile(
    r"CreationDate[^>]*(?:value\s*=\s*[\"'](?P<q>[^\"']+)[\"']|>(?P<t>[^<]+)<)",
    re.IGNORECASE,
)
XMP_DATE_ATTR_RE = re.compile(
    r"(?:CreateDate|DateCreated|DateTimeOriginal)\s*=\s*[\"'](?P<dt>[^\"']+)[\"']",
    re.IGNORECASE,
)


def sony_xml_candidate(mp4_path: Path) -> Path:
    stem = mp4_path.stem
    return mp4_path.with_name(f"{stem}M01.XML")


def read_sony_xml_creation_date(xml_path: Path) -> datetime | None:
    if not xml_path.exists():
        return None
    try:
        root = ET.parse(xml_path).getroot()
        for element in root.iter():
            tag = element.tag.lower()
            if tag.endswith("creationdate"):
                value = element.attrib.get("value") or (element.text or "").strip()
                if value:
                    return date_parser.parse(value)
    except (ET.ParseError, OSError, TypeError, ValueError, OverflowError):
        # Some Sony XML variants are not namespace-friendly or may be truncated.
        # Fall through to the text/regex parser below.
        pass
    try:
        text = xml_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    match = CREATION_DATE_RE.search(text)
    if not match:
        return None
    value = match.group("q") or match.group("t")
    try:
        return date_parser.parse(value)
    except (TypeError, ValueError, OverflowError):
        return None


def read_xmp_sidecar_timestamp(
    path: Path, sidecars: SidecarIndex | None = None
) -> datetime | None:
    index = sidecars or SidecarIndex()
    for match in index.matching(path, kinds={"xmp"}):
        candidate = match.path
        try:
            text = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        match = XMP_DATE_ATTR_RE.search(text)
        if not match:
            continue
        try:
            return date_parser.parse(match.group("dt"))
        except (TypeError, ValueError, OverflowError):
            continue
    return None


class TimestampResolver:
    def __init__(self, exiftool: ExifToolService | None = None) -> None:
        self.exiftool = exiftool or ExifToolService()

    def warm_cache(self, paths: list[Path], *, cancel_event: Event | None = None) -> None:
        self.exiftool.warm_cache(paths, cancel_event=cancel_event)

    def resolve(
        self,
        path: Path,
        fix_sony_timestamps: bool = True,
        use_embedded_metadata: bool = False,
        sidecars: SidecarIndex | None = None,
    ) -> tuple[datetime, str]:
        path = Path(path)
        if fix_sony_timestamps and path.suffix.lower() == ".mp4" and SONY_MP4_RE.match(path.name):
            xml_path = sony_xml_candidate(path)
            sony_timestamp = read_sony_xml_creation_date(xml_path)
            if sony_timestamp is not None:
                return sony_timestamp, "sony_xml"

        if use_embedded_metadata:
            embedded_timestamp = self.exiftool.read_capture_timestamp(path)
            if embedded_timestamp is not None:
                return embedded_timestamp, "embedded_metadata"

        sidecar_timestamp = read_xmp_sidecar_timestamp(path, sidecars)
        if sidecar_timestamp is not None:
            return sidecar_timestamp, "xmp_sidecar"

        return datetime.fromtimestamp(path.stat().st_mtime), "file_mtime"
