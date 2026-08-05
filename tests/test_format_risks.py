from __future__ import annotations

from pathlib import Path

from cameracopy2.core.copy_engine import CopyEngine
from cameracopy2.core.format_warnings import format_risk_warning_paragraphs
from cameracopy2.models import (
    CameraCopyConfig,
    CloneMismatchResponse,
    CopyCallbacks,
    CopyJob,
    VolumeInfo,
)


def _volume(name: str, mount: Path) -> VolumeInfo:
    return VolumeInfo(
        id=name,
        display_name=name,
        mount_path=mount,
        size_bytes=64_000,
        removable=True,
        platform="test",
    )


def _config(destination: Path, **overrides: object) -> CameraCopyConfig:
    values: dict[str, object] = {
        "source": "",
        "destination": str(destination),
        "includedfiles": ["*.JPG"],
        "datetimestring": "",
    }
    values.update(overrides)
    return CameraCopyConfig(**values)  # type: ignore[arg-type]


def _clone_job(
    primary: Path,
    secondary: Path,
    destination: Path,
    **config_overrides: object,
) -> CopyJob:
    return CopyJob(
        primary=_volume("first", primary),
        secondary=_volume("second", secondary),
        config=_config(destination, **config_overrides),
        clone_mode=True,
    )


def test_rating_exclusions_are_tracked_for_both_clone_volumes(tmp_path: Path) -> None:
    primary = tmp_path / "first"
    secondary = tmp_path / "second"
    destination = tmp_path / "pictures"
    for path in (primary, secondary, destination):
        path.mkdir()
    for source in (primary, secondary):
        (source / "LOW.JPG").write_bytes(b"same")
        (source / "LOW.xmp").write_text(
            '<rdf:Description xmp:Rating="1" />', encoding="utf-8"
        )

    job = _clone_job(primary, secondary, destination, minrating=5)
    report = CopyEngine().run(job)

    assert report.format_risk_counts_for(job.primary) == {"rating_excluded": 1}
    assert job.secondary is not None
    assert report.format_risk_counts_for(job.secondary) == {"rating_excluded": 1}


def test_clone_choice_risks_are_assigned_to_the_correct_volume(tmp_path: Path) -> None:
    for decision, expected_volume, expected_kind in (
        ("skip", "second", "clone_kept_first_source"),
        ("replace", "first", "clone_used_second_source"),
    ):
        case = tmp_path / decision
        primary = case / "first"
        secondary = case / "second"
        destination = case / "pictures"
        for path in (primary, secondary, destination):
            path.mkdir(parents=True)
        (primary / "DIFFERENT.JPG").write_bytes(b"first")
        (secondary / "DIFFERENT.JPG").write_bytes(b"second")
        job = _clone_job(primary, secondary, destination)

        report = CopyEngine().run(
            job,
            CopyCallbacks(
                clone_mismatch_decision=lambda *_args, d=decision: (
                    CloneMismatchResponse(decision=d)
                )
            ),
        )

        assert job.secondary is not None
        expected = job.primary if expected_volume == "first" else job.secondary
        other = job.secondary if expected_volume == "first" else job.primary
        assert report.format_risk_counts_for(expected) == {expected_kind: 1}
        assert report.format_risk_counts_for(other) == {}


def test_volume_mismatch_risks_are_clone_only_and_destination_aware(
    tmp_path: Path,
) -> None:
    for decision, expected_kind, existing in (
        ("skip", "volume_mismatch_skipped", False),
        ("keep_existing", "volume_mismatch_kept_existing", True),
    ):
        case = tmp_path / decision
        primary = case / "first"
        secondary = case / "second"
        destination = case / "pictures"
        for path in (primary, secondary, destination):
            path.mkdir(parents=True)
        (secondary / "EXTRA.JPG").write_bytes(b"second-only")
        if existing:
            (destination / "EXTRA.JPG").write_bytes(b"different destination")
        job = _clone_job(primary, secondary, destination)

        report = CopyEngine().run(
            job,
            CopyCallbacks(volume_mismatch_decision=lambda *_args, d=decision: d),
        )

        assert job.secondary is not None
        assert report.format_risk_counts_for(job.primary) == {}
        assert report.format_risk_counts_for(job.secondary) == {expected_kind: 1}


def test_volume_mismatch_warning_counts_enabled_companion_sidecars(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "first"
    secondary = tmp_path / "second"
    destination = tmp_path / "pictures"
    for path in (primary, secondary, destination):
        path.mkdir()
    (secondary / "EXTRA.JPG").write_bytes(b"second-only")
    (secondary / "EXTRA.xmp").write_text("metadata", encoding="utf-8")
    job = _clone_job(primary, secondary, destination, copysidecars=True)

    report = CopyEngine().run(
        job,
        CopyCallbacks(volume_mismatch_decision=lambda *_args: "skip"),
    )

    assert job.secondary is not None
    assert report.format_risk_counts_for(job.secondary) == {
        "volume_mismatch_skipped": 2
    }


def test_successfully_copied_volume_mismatch_creates_no_format_risk(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "first"
    secondary = tmp_path / "second"
    destination = tmp_path / "pictures"
    for path in (primary, secondary, destination):
        path.mkdir()
    (secondary / "EXTRA.JPG").write_bytes(b"second-only")
    job = _clone_job(primary, secondary, destination)

    report = CopyEngine().run(
        job,
        CopyCallbacks(volume_mismatch_decision=lambda *_args: "copy"),
    )

    assert job.secondary is not None
    assert report.format_risk_counts_for(job.secondary) == {}


def test_removed_first_source_does_not_trigger_format_warning(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "first"
    secondary = tmp_path / "second"
    destination = tmp_path / "pictures"
    for path in (primary, secondary, destination):
        path.mkdir()
    first_source = primary / "DIFFERENT.JPG"
    first_source.write_bytes(b"first")
    (secondary / "DIFFERENT.JPG").write_bytes(b"second")
    job = _clone_job(primary, secondary, destination)
    job.autoremove = True

    report = CopyEngine().run(
        job,
        CopyCallbacks(
            clone_mismatch_decision=lambda *_args: CloneMismatchResponse(
                decision="replace"
            )
        ),
    )

    assert not first_source.exists()
    assert report.format_risk_counts_for(job.primary) == {}


def test_normal_mode_tracks_rating_exclusions_without_clone_warnings(
    tmp_path: Path,
) -> None:
    source = tmp_path / "card"
    destination = tmp_path / "pictures"
    source.mkdir()
    destination.mkdir()
    (source / "LOW.JPG").write_bytes(b"image")
    (source / "LOW.xmp").write_text(
        '<rdf:Description xmp:Rating="1" />', encoding="utf-8"
    )
    volume = _volume("card", source)

    report = CopyEngine().run(
        CopyJob(
            primary=volume,
            secondary=None,
            config=_config(destination, minrating=5),
        )
    )

    assert report.format_risk_counts_for(volume) == {"rating_excluded": 1}
    paragraphs = format_risk_warning_paragraphs(
        report.format_risk_counts_for(volume)
    )
    assert len(paragraphs) == 1
    assert "minimum rating" in paragraphs[0]
    assert "clone mismatch" not in paragraphs[0]


def test_preserving_both_clone_versions_creates_no_format_risk(tmp_path: Path) -> None:
    primary = tmp_path / "first"
    secondary = tmp_path / "second"
    destination = tmp_path / "pictures"
    for path in (primary, secondary, destination):
        path.mkdir()
    (primary / "DIFFERENT.JPG").write_bytes(b"first")
    (secondary / "DIFFERENT.JPG").write_bytes(b"second")
    job = _clone_job(primary, secondary, destination)

    report = CopyEngine().run(
        job,
        CopyCallbacks(
            clone_mismatch_decision=lambda *_args: CloneMismatchResponse(
                decision="keep_both"
            )
        ),
    )

    assert job.secondary is not None
    assert report.format_risk_counts_for(job.primary) == {}
    assert report.format_risk_counts_for(job.secondary) == {}


def test_normal_collision_skip_does_not_create_clone_specific_warning(
    tmp_path: Path,
) -> None:
    source = tmp_path / "card"
    destination = tmp_path / "pictures"
    source.mkdir()
    destination.mkdir()
    (source / "EXISTING.JPG").write_bytes(b"source")
    (destination / "EXISTING.JPG").write_bytes(b"destination")
    volume = _volume("card", source)

    report = CopyEngine().run(
        CopyJob(
            primary=volume,
            secondary=None,
            config=_config(destination, collisionpolicy="skip"),
        )
    )

    assert report.format_risk_counts_for(volume) == {}
