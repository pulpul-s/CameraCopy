from __future__ import annotations

import json
import os
from pathlib import Path

from cameracopy2.config import config_from_dict
from cameracopy2.core.copy_engine import CopyEngine
from cameracopy2.core.rating_reader import read_sidecar_rating_with_source
from cameracopy2.core.sidecars import SidecarIndex
from cameracopy2.models import CameraCopyConfig, CopyCallbacks, CopyJob, VolumeInfo


def make_volume(path: Path) -> VolumeInfo:
    return VolumeInfo(id=str(path), display_name=path.name, mount_path=path, platform="test")


def run_copy(source: Path, destination: Path, **config_values) -> object:
    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        **config_values,
    )
    return CopyEngine().run(CopyJob(primary=make_volume(source), secondary=None, config=config))


def test_config_copy_sidecars_defaults_off() -> None:
    assert config_from_dict({"version": 2}).copysidecars is False


def test_config_reads_copy_sidecars() -> None:
    assert config_from_dict({"version": 2, "copysidecars": True}).copysidecars is True


def test_sidecar_lookup_is_case_insensitive_and_finds_all_variants(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    media.write_bytes(b"raw")
    names = [
        "DSC0001.ARW.XMP",
        "DSC0001.xMp",
        "DSC0001.ARW.RRDATA",
        "DSC0001.rrData",
    ]
    for name in names:
        (tmp_path / name).write_text("{}", encoding="utf-8")

    matches = SidecarIndex().matching(media)

    assert {match.path.name for match in matches} == set(names)


def test_rapidraw_rating_is_read_from_top_level_json(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    media.write_bytes(b"raw")
    (tmp_path / "DSC0001.ARW.rrdata").write_text(
        json.dumps({"version": 1, "rating": 5, "adjustments": {"exposure": 1.0}}),
        encoding="utf-8",
    )

    lookup = read_sidecar_rating_with_source(media)

    assert lookup is not None
    assert lookup.rating == 5
    assert lookup.source == "rrdata_sidecar:DSC0001.ARW.rrdata"


def test_invalid_rapidraw_ratings_are_ignored(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    media.write_bytes(b"raw")
    sidecar = tmp_path / "DSC0001.ARW.rrdata"

    for invalid in (True, "5", 4.5, -1, 6, None):
        sidecar.write_text(json.dumps({"rating": invalid}), encoding="utf-8")
        assert read_sidecar_rating_with_source(media) is None

    sidecar.write_text("not json", encoding="utf-8")
    assert read_sidecar_rating_with_source(media) is None


def test_newest_valid_sidecar_rating_wins_and_xmp_wins_ties(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    xmp = tmp_path / "DSC0001.ARW.xmp"
    rrdata = tmp_path / "DSC0001.ARW.rrdata"
    media.write_bytes(b"raw")
    xmp.write_text('<rdf:Description xmp:Rating="2" />', encoding="utf-8")
    rrdata.write_text(json.dumps({"rating": 5}), encoding="utf-8")

    os.utime(xmp, ns=(1_000_000_000, 1_000_000_000))
    os.utime(rrdata, ns=(2_000_000_000, 2_000_000_000))
    lookup = read_sidecar_rating_with_source(media)
    assert lookup is not None
    assert (lookup.rating, lookup.source) == (5, "rrdata_sidecar:DSC0001.ARW.rrdata")

    os.utime(xmp, ns=(3_000_000_000, 3_000_000_000))
    os.utime(rrdata, ns=(3_000_000_000, 3_000_000_000))
    lookup = read_sidecar_rating_with_source(media)
    assert lookup is not None
    assert (lookup.rating, lookup.source) == (2, "xmp_sidecar:DSC0001.ARW.xmp")


def test_rapidraw_rating_filters_media_without_copying_sidecar(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    (source / "DSC0001.ARW.rrdata").write_text(json.dumps({"rating": 5}), encoding="utf-8")

    report = run_copy(source, destination, minrating=5, copysidecars=False)

    assert not report.has_failures
    assert (destination / "DSC0001.ARW").read_bytes() == b"raw"
    assert not (destination / "DSC0001.ARW.rrdata").exists()
    assert report.results[0].rating_source == "rrdata_sidecar:DSC0001.ARW.rrdata"


def test_copy_sidecars_copies_xmp_and_rapidraw_variants(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    sidecars = {
        "DSC0001.ARW.xmp": b"full xmp",
        "DSC0001.xmp": b"stem xmp",
        "DSC0001.ARW.rrdata": b"full rrdata",
        "DSC0001.rrdata": b"stem rrdata",
    }
    for name, content in sidecars.items():
        (source / name).write_bytes(content)

    report = run_copy(source, destination, copysidecars=True)

    assert not report.has_failures
    assert (destination / "DSC0001.ARW").read_bytes() == b"raw"
    for name, content in sidecars.items():
        assert (destination / name).read_bytes() == content
    assert report.copied_count == 5


def test_sidecars_are_not_copied_for_rating_filtered_media(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    (source / "DSC0001.ARW.rrdata").write_text(json.dumps({"rating": 2}), encoding="utf-8")

    report = run_copy(source, destination, minrating=3, copysidecars=True)

    assert not report.has_failures
    assert report.skipped_count == 1
    assert not any(destination.iterdir())


def test_keep_both_renames_media_and_sidecars_as_a_group(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"new raw")
    (source / "DSC0001.ARW.xmp").write_bytes(b"new xmp")
    (source / "DSC0001.rrdata").write_bytes(b"new rrdata")
    (destination / "DSC0001.ARW").write_bytes(b"old raw")
    (destination / "DSC0001_001.ARW.xmp").write_bytes(b"occupied")

    report = run_copy(
        source,
        destination,
        copysidecars=True,
        collisionpolicy="rename",
    )

    assert not report.has_failures
    assert (destination / "DSC0001_002.ARW").read_bytes() == b"new raw"
    assert (destination / "DSC0001_002.ARW.xmp").read_bytes() == b"new xmp"
    assert (destination / "DSC0001_002.rrdata").read_bytes() == b"new rrdata"


def test_source_removal_removes_copied_sidecars(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    (source / "DSC0001.ARW.xmp").write_bytes(b"xmp")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(primary=make_volume(source), secondary=None, config=config, autoremove=True)
    )

    assert not report.has_failures
    assert not (source / "DSC0001.ARW").exists()
    assert not (source / "DSC0001.ARW.xmp").exists()
    assert (destination / "DSC0001.ARW.xmp").read_bytes() == b"xmp"


def test_clone_mode_does_not_verify_companion_only_sidecars(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.ARW").write_bytes(b"raw")
        (root / "DSC0001.ARW.rrdata").write_text(json.dumps({"rating": 5}), encoding="utf-8")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert sum(result.copy_mode == "clone_verified" for result in report.results) == 1
    assert (destination / "DSC0001.ARW.rrdata").exists()
    assert not any(
        result.source.suffix.casefold() == ".rrdata"
        and result.copy_mode == "clone_verified"
        for result in report.results
    )


def test_copy_preserves_sidecar_extension_case(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    (source / "DSC0001.ARW.XMP").write_bytes(b"xmp")

    report = run_copy(source, destination, copysidecars=True)

    assert not report.has_failures
    assert (destination / "DSC0001.ARW.XMP").read_bytes() == b"xmp"
    destination_names = {path.name for path in destination.iterdir()}
    assert "DSC0001.ARW.XMP" in destination_names
    assert "DSC0001.ARW.xmp" not in destination_names


def test_everything_pattern_does_not_copy_matching_sidecar_twice(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    (source / "DSC0001.ARW.rrdata").write_text(json.dumps({"rating": 5}), encoding="utf-8")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
        collisionpolicy="ask",
    )
    report = CopyEngine().run(CopyJob(primary=make_volume(source), secondary=None, config=config))

    assert not report.has_failures
    assert report.copied_count == 2
    assert [result.source.name for result in report.results] == [
        "DSC0001.ARW",
        "DSC0001.ARW.rrdata",
    ]


def test_minimum_rating_off_does_not_read_sidecar_ratings(tmp_path: Path) -> None:
    class FailingRatingReader:
        exiftool = object()

        def read_rating_with_source(self, *_args, **_kwargs):  # noqa: ANN201
            raise AssertionError("rating lookup should not run")

    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    (source / "DSC0001.ARW").write_bytes(b"raw")
    (source / "DSC0001.ARW.rrdata").write_text("not json", encoding="utf-8")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        minrating=0,
    )
    report = CopyEngine(rating_reader=FailingRatingReader()).run(
        CopyJob(primary=make_volume(source), secondary=None, config=config)
    )

    assert not report.has_failures
    assert (destination / "DSC0001.ARW").exists()


def test_clone_mode_allows_missing_companion_only_sidecar(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    (first / "DSC0001.ARW").write_bytes(b"raw")
    (second / "DSC0001.ARW").write_bytes(b"raw")
    (first / "DSC0001.ARW.xmp").write_bytes(b"xmp")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert (destination / "DSC0001.ARW.xmp").read_bytes() == b"xmp"
    log_text = "\n".join(report.logs)
    assert "CLONE VERIFIED:" in log_text
    assert "CLONE FAILED:" not in log_text


def test_clone_mode_verifies_explicit_sidecar_once_when_companion_copy_is_enabled(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.ARW").write_bytes(b"raw")
        (root / "DSC0001.ARW.xmp").write_bytes(b"xmp")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW", "*.xmp"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert report.clone_verified_count == 2
    assert (destination / "DSC0001.ARW.xmp").read_bytes() == b"xmp"
    assert sum(result.source.name == "DSC0001.ARW.xmp" for result in report.results) == 2


def test_clone_mode_verifies_explicit_sidecar_when_companion_copy_is_disabled(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.xmp").write_bytes(b"xmp")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.xmp"],
        datetimestring="",
        minrating=5,
        checkhash=False,
        copysidecars=False,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert report.clone_verified_count == 1
    assert (destination / "DSC0001.xmp").read_bytes() == b"xmp"


def test_clone_mode_reports_missing_explicit_sidecar_as_failure(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.ARW").write_bytes(b"raw")
    (first / "DSC0001.ARW.xmp").write_bytes(b"xmp")

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW", "*.xmp"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        )
    )

    assert report.has_failures
    assert any(
        result.source.name == "DSC0001.ARW.xmp"
        and result.reason
        == "selected on the first-source volume, not found on the second-source volume"
        for result in report.failures
    )


def test_explicit_sidecar_remains_selected_when_parent_media_is_rating_filtered(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.ARW").write_bytes(b"raw")
        (root / "DSC0001.ARW.xmp").write_text(
            '<rdf:Description xmp:Rating="1" />', encoding="utf-8"
        )

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW", "*.xmp"],
        datetimestring="",
        minrating=5,
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        )
    )

    assert not report.has_failures
    assert not (destination / "DSC0001.ARW").exists()
    assert (destination / "DSC0001.ARW.xmp").exists()
    assert report.clone_verified_count == 1


def test_sidecar_lookup_ignores_symlinks(tmp_path: Path) -> None:
    media = tmp_path / "DSC0001.ARW"
    target = tmp_path / "rating.xmp"
    sidecar = tmp_path / "DSC0001.ARW.xmp"
    media.write_bytes(b"raw")
    target.write_text('<rdf:Description xmp:Rating="5" />', encoding="utf-8")
    try:
        sidecar.symlink_to(target)
    except OSError:
        return

    assert SidecarIndex().matching(media) == []


def test_clone_extra_second_volume_media_copies_matching_sidecars_as_group(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "SHARED.ARW").write_bytes(b"shared")
    (second / "EXTRA.ARW").write_bytes(b"extra raw")
    (second / "EXTRA.ARW.rrdata").write_text(
        json.dumps({"rating": 5}), encoding="utf-8"
    )

    decisions: list[tuple[str, int]] = []
    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(
            volume_mismatch_decision=lambda source, _destination, _size, companions, _exists: (
                decisions.append((source.name, companions)) or "copy"
            )
        ),
    )

    assert not report.has_failures
    assert decisions == [("EXTRA.ARW", 1)]
    assert (destination / "EXTRA.ARW").read_bytes() == b"extra raw"
    assert json.loads((destination / "EXTRA.ARW.rrdata").read_text()) == {"rating": 5}
    mismatch_results = [result for result in report.results if result.volume_mismatch]
    assert len(mismatch_results) == 2
    assert all(result.hash_ok is True for result in mismatch_results)
    log_text = "\n".join(report.logs)
    assert "VOLUME MISMATCH — COPIED" in log_text
    assert "EXTRA.ARW.rrdata" in log_text


def test_clone_ignores_extra_companion_only_sidecar_on_second_volume(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.ARW").write_bytes(b"raw")
    extra_sidecar = second / "DSC0001.ARW.rrdata"
    extra_sidecar.write_text(json.dumps({"rating": 4}), encoding="utf-8")

    prompted: list[Path] = []
    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )

    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(
            volume_mismatch_decision=lambda source, _destination, _size, _companions, _exists: (
                prompted.append(source) or "copy"
            )
        ),
    )

    assert not report.has_failures
    assert prompted == []
    assert not (destination / "DSC0001.ARW.rrdata").exists()
    assert f"VOLUME MISMATCH: {extra_sidecar}" not in "\n".join(report.logs)


def test_clone_treats_extra_explicit_sidecar_as_volume_mismatch(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    for root in (first, second):
        (root / "DSC0001.ARW").write_bytes(b"raw")
    extra_sidecar = second / "DSC0001.ARW.rrdata"
    extra_sidecar.write_text(json.dumps({"rating": 4}), encoding="utf-8")

    prompted: list[Path] = []
    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW", "*.rrdata"],
        datetimestring="",
        checkhash=False,
        copysidecars=True,
    )
    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(
            volume_mismatch_decision=lambda source, _destination, _size, _companions, _exists: (
                prompted.append(source) or "copy"
            )
        ),
    )

    assert not report.has_failures
    assert prompted == [extra_sidecar]
    assert (destination / "DSC0001.ARW.rrdata").exists()
    assert f"VOLUME MISMATCH: {extra_sidecar}" in "\n".join(report.logs)


def test_rating_summary_aggregates_sources_without_sidecar_filenames(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    source.mkdir()
    destination.mkdir()
    for index in (1, 2):
        (source / f"DSC{index:04d}.ARW").write_bytes(b"raw")
        (source / f"DSC{index:04d}.ARW.rrdata").write_text(
            json.dumps({"rating": 5}), encoding="utf-8"
        )
    (source / "DSC0003.ARW").write_bytes(b"raw")

    report = run_copy(source, destination, minrating=1)

    assert report.rating_source_counts == {"rrdata_sidecar": 2, "missing": 1}
    summary = report.summary_lines()
    assert "Ratings:" in summary
    assert "  RapidRaw sidecar: 2 files" in summary
    assert "  No rating found (treated as 0): 1 file" in summary
    assert not any("DSC0001" in line or "DSC0002" in line for line in summary)


def test_clone_ignores_second_volume_media_and_sidecars_filtered_on_first(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    destination = tmp_path / "destination"
    first.mkdir()
    second.mkdir()
    destination.mkdir()
    (first / "FILTERED.ARW").write_bytes(b"first version")
    (first / "FILTERED.ARW.rrdata").write_text(
        json.dumps({"rating": 1}), encoding="utf-8"
    )
    (second / "FILTERED.ARW").write_bytes(b"different second version")
    (second / "FILTERED.ARW.xmp").write_text(
        '<rdf:Description xmp:Rating="5" />', encoding="utf-8"
    )

    config = CameraCopyConfig(
        source="",
        destination=str(destination),
        includedfiles=["*.ARW"],
        datetimestring="",
        minrating=3,
        checkhash=False,
        copysidecars=True,
    )

    def unexpected_prompt(*_args) -> str:
        raise AssertionError("filtered second-volume files must not prompt")

    report = CopyEngine().run(
        CopyJob(
            primary=make_volume(first),
            secondary=make_volume(second),
            config=config,
            clone_mode=True,
        ),
        CopyCallbacks(volume_mismatch_decision=unexpected_prompt),
    )

    assert not report.has_failures
    assert not any(destination.iterdir())
    assert "VOLUME MISMATCH" not in "\n".join(report.logs)
