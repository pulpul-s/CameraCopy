from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from cameracopy2.services.metadata_service import (
    RATING_TAGS,
    TIMESTAMP_TAGS,
    ExifToolService,
)


def _available_exiftool() -> ExifToolService:
    service = ExifToolService()
    service.available = True
    service.executable = "exiftool"
    return service


def test_exiftool_cache_fetches_missing_tags_after_narrow_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    service = _available_exiftool()
    calls: list[tuple[str, ...]] = []

    def fake_read(
        tags: tuple[str, ...], _paths: list[Path], **_kwargs: object
    ) -> object:
        calls.append(tags)
        if tags == RATING_TAGS:
            return [{"SourceFile": str(media), "XMP:Rating": 4}]
        if tags == TIMESTAMP_TAGS:
            return [
                {
                    "SourceFile": str(media),
                    "EXIF:DateTimeOriginal": "2026:07:29 10:11:12",
                }
            ]
        raise AssertionError(f"unexpected tags: {tags!r}")

    monkeypatch.setattr(service, "_run_json_command", fake_read)

    assert service.read_rating(media) == 4
    assert service.read_capture_timestamp(media) == datetime(2026, 7, 29, 10, 11, 12)
    assert service.read_rating(media) == 4
    assert calls == [RATING_TAGS, TIMESTAMP_TAGS]


def test_exiftool_failed_read_can_be_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    service = _available_exiftool()
    call_count = 0

    def fake_read(
        _tags: tuple[str, ...], _paths: list[Path], **_kwargs: object
    ) -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return []
        return [{"SourceFile": str(media), "XMP:Rating": 5}]

    monkeypatch.setattr(service, "_run_json_command", fake_read)

    assert service.read_rating(media) is None
    assert service.read_rating(media) == 5
    assert call_count == 2


def test_exiftool_warm_cache_tracks_requested_tag_coverage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = tmp_path / "DSC0001.JPG"
    media.write_bytes(b"image")
    service = _available_exiftool()
    calls: list[tuple[str, ...]] = []

    def fake_read(
        tags: tuple[str, ...], _paths: list[Path], **_kwargs: object
    ) -> object:
        calls.append(tags)
        if tags == RATING_TAGS:
            return [{"SourceFile": str(media), "XMP:Rating": 3}]
        return [
            {
                "SourceFile": str(media),
                "EXIF:DateTimeOriginal": "2025:01:02 03:04:05",
            }
        ]

    monkeypatch.setattr(service, "_run_json_command", fake_read)

    service.warm_cache([media, media], tags=RATING_TAGS)
    assert service.read_rating(media) == 3
    assert service.read_capture_timestamp(media) == datetime(2025, 1, 2, 3, 4, 5)
    assert calls == [RATING_TAGS, TIMESTAMP_TAGS]
