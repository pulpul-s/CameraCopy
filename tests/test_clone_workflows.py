from __future__ import annotations

from datetime import datetime
from pathlib import Path
from threading import Event

import pytest

from cameracopy2.core.copy_engine import (
    CloneManifestEntry,
    CloneMismatchWork,
    CopyEngine,
)
from cameracopy2.core.copy_messages import result_message
from cameracopy2.core.hash import HashResult
from cameracopy2.models import (
    CameraCopyConfig,
    CloneMismatchResponse,
    CopyCallbacks,
    CopyJob,
    CopyReport,
    FileCopyResult,
    VolumeInfo,
)


def _volume(path: Path) -> VolumeInfo:
    return VolumeInfo(id=str(path), display_name=path.name, mount_path=path)


def _clone_job(primary: Path, secondary: Path, destination: Path) -> CopyJob:
    return CopyJob(
        primary=_volume(primary),
        secondary=_volume(secondary),
        config=CameraCopyConfig(
            source="",
            destination=str(destination),
            includedfiles=["*.JPG"],
            datetimestring="",
            checkhash=True,
        ),
        clone_mode=True,
    )


def test_clone_mismatch_prompt_is_deferred_until_normal_verification_finishes(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    destination = tmp_path / "destination"
    for path in (primary, secondary, destination):
        path.mkdir()
    (primary / "A.JPG").write_bytes(b"first-a")
    (secondary / "A.JPG").write_bytes(b"second-a")
    (primary / "B.JPG").write_bytes(b"same-b")
    (secondary / "B.JPG").write_bytes(b"same-b")

    logs: list[str] = []

    def choose_keep_first(*_args: object) -> CloneMismatchResponse:
        assert any(line.startswith("CLONE VERIFIED:") and "B.JPG" in line for line in logs)
        assert any(line.startswith("CLONE MISMATCH:") and "A.JPG" in line for line in logs)
        return CloneMismatchResponse(decision="skip")

    report = CopyEngine().run(
        _clone_job(primary, secondary, destination),
        CopyCallbacks(log=logs.append, clone_mismatch_decision=choose_keep_first),
    )

    assert not report.has_failures
    assert any(line.startswith("CLONE MISMATCH — KEPT FIRST-SOURCE:") for line in logs)
    assert (destination / "A.JPG").read_bytes() == b"first-a"


def test_clone_mismatch_prompts_before_volume_mismatch_prompts(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    destination = tmp_path / "destination"
    for path in (primary, secondary, destination):
        path.mkdir()
    (primary / "A.JPG").write_bytes(b"first")
    (secondary / "A.JPG").write_bytes(b"second")
    (secondary / "EXTRA.JPG").write_bytes(b"extra")

    prompt_order: list[str] = []
    callbacks = CopyCallbacks(
        clone_mismatch_decision=lambda *_: (
            prompt_order.append("clone") or CloneMismatchResponse(decision="keep_both")
        ),
        volume_mismatch_decision=lambda *_: prompt_order.append("volume") or "skip",
    )
    report = CopyEngine().run(_clone_job(primary, secondary, destination), callbacks)

    assert not report.has_failures
    assert prompt_order == ["clone", "volume"]


def test_volume_mismatch_with_identical_destination_is_verified_without_prompt(
    tmp_path: Path,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    destination = tmp_path / "destination"
    for path in (primary, secondary, destination):
        path.mkdir()
    (primary / "SHARED.JPG").write_bytes(b"shared")
    (secondary / "SHARED.JPG").write_bytes(b"shared")
    (secondary / "EXTRA.JPG").write_bytes(b"extra")
    (destination / "EXTRA.JPG").write_bytes(b"extra")

    report = CopyEngine().run(
        _clone_job(primary, secondary, destination),
        CopyCallbacks(
            volume_mismatch_decision=lambda *_: (_ for _ in ()).throw(
                AssertionError("identical destination must not prompt")
            )
        ),
    )

    assert not report.has_failures
    assert any(
        line.startswith("VOLUME MISMATCH — FOUND IDENTICAL COPY IN DESTINATION:")
        for line in report.logs
    )


@pytest.mark.parametrize(
    ("decision", "expected_status", "expected_main", "expected_renamed"),
    [
        ("keep_both", "VOLUME MISMATCH — KEPT BOTH:", b"old", b"second"),
        ("keep_existing", "VOLUME MISMATCH — KEPT EXISTING:", b"old", None),
        ("replace", "VOLUME MISMATCH — REPLACED:", b"second", None),
    ],
)
def test_volume_mismatch_with_different_destination_uses_explicit_outcome(
    tmp_path: Path,
    decision: str,
    expected_status: str,
    expected_main: bytes,
    expected_renamed: bytes | None,
) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    destination = tmp_path / "destination"
    for path in (primary, secondary, destination):
        path.mkdir()
    (primary / "SHARED.JPG").write_bytes(b"shared")
    (secondary / "SHARED.JPG").write_bytes(b"shared")
    (secondary / "EXTRA.JPG").write_bytes(b"second")
    (destination / "EXTRA.JPG").write_bytes(b"old")

    observed: list[tuple[Path, Path, bool]] = []

    def choose(
        source: Path,
        target: Path,
        _size: int,
        _companions: int,
        target_exists: bool,
    ) -> str:
        observed.append((source, target, target_exists))
        return decision

    report = CopyEngine().run(
        _clone_job(primary, secondary, destination),
        CopyCallbacks(volume_mismatch_decision=choose),
    )

    assert not report.has_failures
    assert observed == [(secondary / "EXTRA.JPG", destination / "EXTRA.JPG", True)]
    assert (destination / "EXTRA.JPG").read_bytes() == expected_main
    renamed = destination / "EXTRA_001.JPG"
    if expected_renamed is None:
        assert not renamed.exists()
    else:
        assert renamed.read_bytes() == expected_renamed
    assert any(line.startswith(expected_status) for line in report.logs)


def test_volume_mismatch_copy_status_comes_from_actual_copy_result(tmp_path: Path) -> None:
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    destination = tmp_path / "destination"
    for path in (primary, secondary, destination):
        path.mkdir()
    (primary / "SHARED.JPG").write_bytes(b"shared")
    (secondary / "SHARED.JPG").write_bytes(b"shared")
    (secondary / "EXTRA.JPG").write_bytes(b"extra")

    report = CopyEngine().run(
        _clone_job(primary, secondary, destination),
        CopyCallbacks(volume_mismatch_decision=lambda *_: "copy"),
    )

    assert any(line.startswith("VOLUME MISMATCH — COPIED:") for line in report.logs)
    assert not any("VOLUME MISMATCH — SKIPPED:" in line for line in report.logs)


@pytest.mark.parametrize(
    "reason",
    [
        "copy cancelled",
        "copy cancelled by existing-file prompt",
        "copy cancelled by clone mismatch dialog",
        "copy cancelled by volume mismatch dialog",
    ],
)
def test_file_result_exposes_all_copy_cancellation_variants(
    tmp_path: Path, reason: str
) -> None:
    result = FileCopyResult(
        source=tmp_path / "source.JPG",
        destination=tmp_path / "destination.JPG",
        action="skipped",
        reason=reason,
    )

    assert result.cancelled
    assert not result.failed


def test_clone_replacement_records_live_destination_size_before_copy(tmp_path: Path) -> None:
    source = tmp_path / "second" / "DSC0001.ARW"
    destination = tmp_path / "pictures" / "DSC0001.ARW"
    source.parent.mkdir()
    destination.parent.mkdir()
    source.write_bytes(b"second-source")
    destination.write_bytes(b"x")
    mismatch = CloneMismatchWork(
        source=source,
        source_size=source.stat().st_size,
        entry=CloneManifestEntry(
            relative_path="DSC0001.ARW",
            source=tmp_path / "first" / "DSC0001.ARW",
            destination=destination,
            size_bytes=39_116 * 1024,
        ),
        file_index=1,
    )

    result = CopyEngine()._resolve_clone_mismatch(  # noqa: SLF001
        mismatch,
        "replace",
        False,
        False,
        CopyCallbacks(),
        Event(),
    )

    assert result.copy_mode == "clone_mismatch_replaced"
    assert result.first_source_size_bytes == 1
    assert destination.read_bytes() == b"second-source"
    assert "First-source copy replaced" in result_message(result)
    assert "(1 byte)" in result_message(result).splitlines()[1]


def test_clone_hash_read_error_is_failure_not_mismatch_prompt(tmp_path: Path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from cameracopy2.core import copy_engine as copy_module

    source = tmp_path / "second.JPG"
    destination = tmp_path / "first.JPG"
    source.write_bytes(b"second")
    destination.write_bytes(b"first")
    monkeypatch.setattr(
        copy_module,
        "compare_sha256_cancellable",
        lambda *_args, **_kwargs: HashResult(None, None, False, "read failed"),
    )
    callbacks = CopyCallbacks(
        clone_mismatch_decision=lambda *_args: (_ for _ in ()).throw(
            AssertionError("mismatch prompt must not be shown")
        )
    )

    result = CopyEngine()._process_clone_second_file(  # noqa: SLF001
        source=source,
        source_size=source.stat().st_size,
        entry=CloneManifestEntry(
            relative_path="second.JPG",
            source=destination,
            destination=destination,
            size_bytes=destination.stat().st_size,
        ),
        autoremove=False,
        callbacks=callbacks,
        report=CopyReport(started_at=datetime.now()),
        cancel_event=Event(),
        file_index=1,
    )

    assert result.failed
    assert result.reason == "clone SHA256 comparison failed"
    assert result.error == "read failed"
