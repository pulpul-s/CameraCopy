from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from cameracopy2.core.sidecars import SidecarIndex, SidecarMatch
from cameracopy2.services.metadata_service import ExifToolService

RATING_RE = re.compile(r"(?:xmp:)?Rating\s*=\s*[\"'](?P<rating>-?\d+)[\"']", re.IGNORECASE)


@dataclass(slots=True)
class RatingLookup:
    rating: int
    source: str


def _parse_rating_text(text: str) -> int | None:
    match = RATING_RE.search(text)
    if not match:
        return None
    try:
        rating = int(match.group("rating"))
    except ValueError:
        return None
    return rating if 0 <= rating <= 5 else None


def _parse_rating_xml(path: Path) -> int | None:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None
    for element in root.iter():
        for key, value in element.attrib.items():
            if key.lower().endswith("rating"):
                try:
                    rating = int(float(value))
                except ValueError:
                    continue
                if 0 <= rating <= 5:
                    return rating
    return None


def _read_xmp_rating(path: Path) -> int | None:
    rating = _parse_rating_xml(path)
    if rating is not None:
        return rating
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return _parse_rating_text(text)


def _read_rapidraw_rating(path: Path) -> int | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(payload, dict):
        return None
    rating = payload.get("rating")
    if isinstance(rating, bool) or not isinstance(rating, int):
        return None
    return rating if 0 <= rating <= 5 else None


def _rating_from_match(match: SidecarMatch) -> int | None:
    if match.kind == "xmp":
        return _read_xmp_rating(match.path)
    return _read_rapidraw_rating(match.path)


def read_sidecar_rating_with_source(
    path: Path,
    sidecars: SidecarIndex | None = None,
) -> RatingLookup | None:
    index = sidecars or SidecarIndex()
    candidates: list[tuple[int, int, str, int]] = []
    for match in index.matching(path):
        rating = _rating_from_match(match)
        if rating is None:
            continue
        try:
            modified_ns = match.path.stat().st_mtime_ns
        except OSError:
            modified_ns = 0
        xmp_tie_break = 1 if match.kind == "xmp" else 0
        source = f"{match.kind}_sidecar:{match.path.name}"
        candidates.append((modified_ns, xmp_tie_break, source, rating))

    if not candidates:
        return None
    _, _, source, rating = max(candidates)
    return RatingLookup(rating=rating, source=source)


class RatingReader:
    def __init__(self, exiftool: ExifToolService | None = None) -> None:
        self.exiftool = exiftool or ExifToolService()

    def warm_cache(self, paths: list[Path], *, cancel_event: Event | None = None) -> None:
        self.exiftool.warm_cache(paths, cancel_event=cancel_event)

    def read_rating(self, path: Path, use_embedded_metadata: bool = False) -> int:
        return self.read_rating_with_source(path, use_embedded_metadata).rating

    def read_rating_with_source(
        self,
        path: Path,
        use_embedded_metadata: bool = False,
        sidecars: SidecarIndex | None = None,
    ) -> RatingLookup:
        sidecar_rating = read_sidecar_rating_with_source(path, sidecars)
        if sidecar_rating is not None:
            return sidecar_rating
        if use_embedded_metadata:
            embedded_rating = self.exiftool.read_rating(path)
            if embedded_rating is not None:
                return RatingLookup(rating=embedded_rating, source="embedded_xmp")
        return RatingLookup(rating=0, source="missing")
