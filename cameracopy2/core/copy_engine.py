from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import Event
from typing import Literal

from cameracopy2.core.copy_messages import format_log_file_size, result_message
from cameracopy2.core.hash import (
    HashProgressCallback,
    compare_sha256_cancellable,
    sha256_file,
)
from cameracopy2.core.log_messages import classify_file_result, classify_log_message
from cameracopy2.core.naming import build_destination_file
from cameracopy2.core.rating_reader import RatingReader
from cameracopy2.core.scanner import find_candidate_files
from cameracopy2.core.sidecars import SidecarIndex, SidecarMatch, is_sidecar_path
from cameracopy2.core.source_paths import resolve_source_root
from cameracopy2.core.timestamp_resolver import TimestampResolver
from cameracopy2.services.metadata_service import ExifToolService
from cameracopy2.models import (
    CameraCopyConfig,
    CloneMismatchDecision,
    CollisionPolicy,
    CopyCallbacks,
    CopyJob,
    CopyMode,
    CopyReport,
    FileCopyResult,
    VolumeInfo,
    VolumeMismatchDecision,
)

COPY_CHUNK_SIZE = 256 * 1024
CloneRole = Literal["normal", "primary", "secondary"]
CloneInventoryState = Literal["selected", "rating_excluded", "skipped"]

logger = logging.getLogger(__name__)


def _friendly_permission_error(exc: PermissionError) -> str:
    path = getattr(exc, "filename", None) or getattr(exc, "filename2", None)
    return f"permission denied: {path}" if path else "permission denied"


def _friendly_exception_error(exc: Exception) -> str:
    if isinstance(exc, OSError):
        message = str(exc)
        return message.replace("[Errno 13] ", "").strip()
    return str(exc)


def _source_stat_changed(before: os.stat_result, after: os.stat_result) -> bool:
    return (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    )


@dataclass(slots=True)
class VerifiedCopy:
    ok: bool
    hash_ok: bool | None
    bytes_copied: int = 0
    cancelled: bool = False
    error: str | None = None


@dataclass(slots=True)
class FileContext:
    source: Path
    destination: Path
    rating: int | None
    rating_source: str | None
    timestamp: datetime
    timestamp_source: str
    source_size: int
    collision_policy: CollisionPolicy


@dataclass(slots=True)
class CloneManifestEntry:
    relative_path: str
    source: Path
    destination: Path
    size_bytes: int


@dataclass(slots=True)
class CloneMismatchWork:
    source: Path
    source_size: int
    entry: CloneManifestEntry
    file_index: int


@dataclass(slots=True)
class VolumeMismatchWork:
    source: Path
    source_size: int
    destination: Path | None = None
    companion_matches: tuple[SidecarMatch, ...] = ()


@dataclass(frozen=True, slots=True)
class SidecarWork:
    match: SidecarMatch
    explicitly_selected: bool


@dataclass(slots=True)
class SourceProgress:
    callbacks: CopyCallbacks
    total_items: int
    total_bytes: int
    item_index: int = 0
    done_bytes: int = 0

    def start_file(self, source: Path, size_bytes: int) -> int:
        self.item_index += 1
        self.callbacks.emit_file_started(
            source, self.item_index, self.total_items, size_bytes
        )
        self.callbacks.emit_progress(self.item_index - 1, self.total_items)
        self.callbacks.emit_byte_progress(0, size_bytes, self.item_index)
        return self.item_index

    def finish_file(self, size_bytes: int) -> None:
        self.done_bytes = min(self.total_bytes, self.done_bytes + size_bytes)
        self.callbacks.emit_source_progress(self.done_bytes, self.total_bytes, False)
        self.callbacks.emit_progress(self.item_index, self.total_items)

    def skip_files(self, sizes: list[int]) -> None:
        self.item_index += len(sizes)
        self.done_bytes = min(self.total_bytes, self.done_bytes + sum(sizes))
        self.callbacks.emit_source_progress(self.done_bytes, self.total_bytes, False)
        self.callbacks.emit_progress(self.item_index, self.total_items)


class CopyEngine:
    def __init__(
        self,
        rating_reader: RatingReader | None = None,
        timestamp_resolver: TimestampResolver | None = None,
    ) -> None:
        if rating_reader is None and timestamp_resolver is None:
            exiftool = ExifToolService()
            self.rating_reader = RatingReader(exiftool)
            self.timestamp_resolver = TimestampResolver(exiftool)
            return

        shared_exiftool = getattr(rating_reader, "exiftool", None) or getattr(
            timestamp_resolver, "exiftool", None
        )
        self.rating_reader = rating_reader or RatingReader(shared_exiftool)
        self.timestamp_resolver = timestamp_resolver or TimestampResolver(shared_exiftool)

    def run(
        self,
        job: CopyJob,
        callbacks: CopyCallbacks | None = None,
        cancel_event: Event | None = None,
    ) -> CopyReport:
        callbacks = callbacks or CopyCallbacks()
        cancel_event = cancel_event or Event()
        report = CopyReport(started_at=datetime.now())
        volume_modes = self._volume_modes(job)
        clone_manifest: dict[str, CloneManifestEntry] | None = (
            {} if job.clone_mode and job.secondary else None
        )
        clone_inventory: dict[str, CloneInventoryState] | None = (
            {} if clone_manifest is not None else None
        )

        self._log_start(callbacks, report, job, volume_modes)
        active_source = job.primary.mount_path

        try:
            source_count = len(volume_modes)
            for index, (volume, clone_mode) in enumerate(volume_modes, start=1):
                active_source = volume.mount_path
                if cancel_event.is_set():
                    report.cancelled = True
                    break
                if index > 1:
                    self._log(callbacks, report, "")

                clone_role: CloneRole = "normal"
                if clone_manifest is not None:
                    clone_role = "secondary" if clone_mode else "primary"

                self._run_volume(
                    volume,
                    job.config,
                    job.autoremove,
                    callbacks,
                    report,
                    cancel_event,
                    clone_manifest=clone_manifest,
                    clone_inventory=clone_inventory,
                    clone_role=clone_role,
                    source_index=index,
                    source_count=source_count,
                )
        except Exception as exc:  # noqa: BLE001 - preserve completed copy results
            logger.exception("Unexpected copy-engine failure")
            result = FileCopyResult(
                source=active_source,
                destination=None,
                action="failed",
                reason="internal copy error",
                error=str(exc),
            )
            self._emit_result(callbacks, report, result)
        finally:
            if cancel_event.is_set():
                report.cancelled = True
            report.finished_at = datetime.now()
            for line in report.summary_lines():
                report.add_log(line)
        return report

    @staticmethod
    def _volume_modes(job: CopyJob) -> list[tuple[VolumeInfo, bool]]:
        volume_modes: list[tuple[VolumeInfo, bool]] = [(job.primary, False)]
        if job.secondary is not None:
            volume_modes.append((job.secondary, job.clone_mode))
        return volume_modes

    def _log_start(
        self,
        callbacks: CopyCallbacks,
        report: CopyReport,
        job: CopyJob,
        volume_modes: list[tuple[VolumeInfo, bool]],
    ) -> None:
        source_roots = [
            str(resolve_source_root(volume, job.config.source)) for volume, _ in volume_modes
        ]
        source_text = source_roots[0] if len(source_roots) == 1 else " and ".join(source_roots)
        self._log(
            callbacks, report, f"Starting copy from {source_text} to {job.config.destination}"
        )
        self._log(
            callbacks,
            report,
            f"Verification: {'SHA256' if job.config.checkhash else 'basic size check'}",
        )
        if job.clone_mode and job.secondary is not None:
            self._log(callbacks, report, "Clone verification: SHA256")
        self._log(
            callbacks,
            report,
            f"Existing files: {self._collision_policy_label(job.config.collisionpolicy)}",
        )
        sidecar_policy = (
            "copy matching XMP and RapidRaw files"
            if job.config.copysidecars
            else "do not copy"
        )
        self._log(callbacks, report, f"Sidecars: {sidecar_policy}")
        if job.config.minrating > 0:
            self._log(callbacks, report, f"Minimum rating: {job.config.minrating}")
        cleanup = (
            "remove after successful copy" if job.autoremove else "leave source files in place"
        )
        self._log(callbacks, report, f"Source cleanup: {cleanup}")
        self._log(callbacks, report, "")

    @staticmethod
    def _collision_policy_label(policy: CollisionPolicy) -> str:
        labels = {
            "ask": "Always ask",
            "skip": "Skip existing files",
            "overwrite": "Replace existing files",
            "rename": "Keep both",
        }
        return labels.get(policy, policy)

    def _run_volume(
        self,
        volume: VolumeInfo,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        *,
        clone_manifest: dict[str, CloneManifestEntry] | None,
        clone_inventory: dict[str, CloneInventoryState] | None,
        clone_role: CloneRole,
        source_index: int,
        source_count: int,
    ) -> None:
        if not volume.is_available:
            self._emit_result(
                callbacks,
                report,
                FileCopyResult(
                    source=volume.mount_path,
                    destination=None,
                    action="failed",
                    error="volume is not available",
                    reason="volume_unavailable",
                ),
            )
            return

        source_root = resolve_source_root(volume, config.source)
        callbacks.emit_source_started(source_index, source_count, "Scanning", 0)
        self._log(callbacks, report, f"Scanning {source_root}")
        if not source_root.exists():
            self._emit_result(
                callbacks,
                report,
                FileCopyResult(
                    source=source_root,
                    destination=None,
                    action="failed",
                    error="source path does not exist",
                    reason="source_missing",
                ),
            )
            return

        try:
            files = find_candidate_files(source_root, config.includedfiles, config.excludedfiles)
        except Exception as exc:  # noqa: BLE001
            self._emit_result(
                callbacks,
                report,
                FileCopyResult(
                    source=source_root,
                    destination=None,
                    action="failed",
                    error=str(exc),
                    reason="scan_failed",
                ),
            )
            return

        if clone_role != "secondary":
            self._warm_metadata_cache(files, config, cancel_event)
            if cancel_event.is_set():
                report.cancelled = True
                self._log(callbacks, report, "Copy cancelled during metadata read.")
                return

        sidecar_index, files, sidecars_by_media = self._prepare_sidecar_work(files, config)
        sidecar_count = sum(len(matches) for matches in sidecars_by_media.values())
        source_total_bytes = sum(self._source_size(path) for path in files) + sum(
            self._source_size(work.match.path)
            for works in sidecars_by_media.values()
            for work in works
        )
        progress = SourceProgress(
            callbacks=callbacks,
            total_items=len(files) + sidecar_count,
            total_bytes=source_total_bytes,
        )
        source_action = self._source_action_label(clone_role)
        callbacks.emit_source_started(
            source_index, source_count, source_action, source_total_bytes
        )
        callbacks.emit_source_progress(0, source_total_bytes, False)
        self._log_discovered_files(callbacks, report, len(files), sidecar_count)
        self._log(
            callbacks, report, self._phase_start_message(clone_role, source_index, source_count)
        )

        seen_clone_paths: set[str] = set()
        clone_mismatches: list[CloneMismatchWork] = []
        volume_mismatches: list[VolumeMismatchWork] = []

        if clone_role == "secondary" and clone_manifest is not None and not clone_manifest:
            self._log(
                callbacks,
                report,
                "No first-source files are available for clone verification.",
            )

        for source in files:
            if cancel_event.is_set():
                report.cancelled = True
                self._log(callbacks, report, "Copy cancelled before next file.")
                break

            sidecar_work = sidecars_by_media.get(source, ())
            if clone_role == "secondary":
                assert clone_manifest is not None
                assert clone_inventory is not None
                self._process_secondary_media(
                    source=source,
                    sidecar_work=sidecar_work,
                    source_root=source_root,
                    config=config,
                    autoremove=autoremove,
                    callbacks=callbacks,
                    report=report,
                    cancel_event=cancel_event,
                    clone_manifest=clone_manifest,
                    clone_inventory=clone_inventory,
                    seen_clone_paths=seen_clone_paths,
                    clone_mismatches=clone_mismatches,
                    volume_mismatches=volume_mismatches,
                    progress=progress,
                )
            else:
                self._process_primary_or_normal_media(
                    source=source,
                    sidecar_work=sidecar_work,
                    source_root=source_root,
                    config=config,
                    autoremove=autoremove,
                    callbacks=callbacks,
                    report=report,
                    cancel_event=cancel_event,
                    clone_manifest=clone_manifest,
                    clone_inventory=clone_inventory,
                    clone_role=clone_role,
                    sidecar_index=sidecar_index,
                    seen_clone_paths=seen_clone_paths,
                    progress=progress,
                )

            if cancel_event.is_set():
                report.cancelled = True
                break

        if clone_role == "secondary" and clone_manifest is not None and not cancel_event.is_set():
            self._process_clone_mismatches(
                clone_mismatches,
                autoremove,
                config.durablewrites,
                callbacks,
                report,
                cancel_event,
            )
            if cancel_event.is_set():
                report.cancelled = True
                return
            self._emit_missing_clone_files(
                callbacks, report, source_root, clone_manifest, seen_clone_paths
            )
            self._process_volume_mismatches(
                volume_mismatches,
                config,
                autoremove,
                callbacks,
                report,
                cancel_event,
                sidecar_index,
                progress,
            )

    def _process_secondary_media(
        self,
        *,
        source: Path,
        sidecar_work: tuple[SidecarWork, ...],
        source_root: Path,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        clone_manifest: dict[str, CloneManifestEntry],
        clone_inventory: dict[str, CloneInventoryState],
        seen_clone_paths: set[str],
        clone_mismatches: list[CloneMismatchWork],
        volume_mismatches: list[VolumeMismatchWork],
        progress: SourceProgress,
    ) -> None:
        source_size = self._source_size(source)
        relative_path = self._relative_source_path(source_root, source)
        inventory_state = clone_inventory.get(relative_path)
        if inventory_state is None:
            self._queue_volume_mismatch(
                callbacks,
                report,
                volume_mismatches,
                source,
                source_size,
                companion_matches=tuple(work.match for work in sidecar_work),
            )
            return

        manifest_entry = clone_manifest.get(relative_path)
        if inventory_state == "rating_excluded":
            report.add_format_risk("rating_excluded", source)
        if inventory_state != "selected" or manifest_entry is None:
            progress.skip_files([source_size])
            self._process_secondary_sidecars(
                sidecar_work=sidecar_work,
                source_root=source_root,
                media_manifest_entry=None,
                config=config,
                autoremove=autoremove,
                callbacks=callbacks,
                report=report,
                cancel_event=cancel_event,
                clone_manifest=clone_manifest,
                clone_inventory=clone_inventory,
                seen_clone_paths=seen_clone_paths,
                clone_mismatches=clone_mismatches,
                volume_mismatches=volume_mismatches,
                progress=progress,
            )
            return

        seen_clone_paths.add(relative_path)
        file_index = progress.start_file(source, source_size)
        result = self._process_clone_second_file(
            source=source,
            source_size=source_size,
            entry=manifest_entry,
            autoremove=autoremove,
            callbacks=callbacks,
            report=report,
            cancel_event=cancel_event,
            file_index=file_index,
            source_done_bytes=progress.done_bytes,
            source_total_bytes=progress.total_bytes,
            clone_mismatches=clone_mismatches,
        )
        if result is not None:
            self._emit_result(callbacks, report, result)
        progress.finish_file(source_size)
        self._process_secondary_sidecars(
            sidecar_work=sidecar_work,
            source_root=source_root,
            media_manifest_entry=manifest_entry,
            config=config,
            autoremove=autoremove,
            callbacks=callbacks,
            report=report,
            cancel_event=cancel_event,
            clone_manifest=clone_manifest,
            clone_inventory=clone_inventory,
            seen_clone_paths=seen_clone_paths,
            clone_mismatches=clone_mismatches,
            volume_mismatches=volume_mismatches,
            progress=progress,
        )

    def _process_primary_or_normal_media(
        self,
        *,
        source: Path,
        sidecar_work: tuple[SidecarWork, ...],
        source_root: Path,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        clone_manifest: dict[str, CloneManifestEntry] | None,
        clone_inventory: dict[str, CloneInventoryState] | None,
        clone_role: CloneRole,
        sidecar_index: SidecarIndex,
        seen_clone_paths: set[str],
        progress: SourceProgress,
    ) -> None:
        source_size = self._source_size(source)
        file_index = progress.start_file(source, source_size)
        relative_path = self._relative_source_path(source_root, source)
        result, media_context, media_selected = self._process_media_file(
            source=source,
            relative_path=relative_path,
            config=config,
            autoremove=autoremove,
            callbacks=callbacks,
            report=report,
            cancel_event=cancel_event,
            clone_manifest=clone_manifest,
            clone_role=clone_role,
            sidecar_index=sidecar_index,
            companion_matches=tuple(work.match for work in sidecar_work),
            explicitly_selected_sidecar=is_sidecar_path(source),
            file_index=file_index,
            source_done_bytes=progress.done_bytes,
            source_total_bytes=progress.total_bytes,
        )
        if clone_role == "primary" and clone_inventory is not None:
            self._record_primary_inventory(
                clone_inventory,
                source_root,
                source,
                sidecar_work,
                state=self._clone_inventory_state(media_selected, result),
            )

        self._emit_result(callbacks, report, result)
        progress.finish_file(source_size)

        companion_should_copy = media_selected and self._can_process_sidecars(result)
        for work in sidecar_work:
            if work.explicitly_selected or companion_should_copy:
                self._process_sidecar_file(
                    work=work,
                    source_root=source_root,
                    media_context=media_context,
                    media_destination=(
                        result.destination
                        or (media_context.destination if media_context is not None else None)
                    ),
                    config=config,
                    autoremove=autoremove,
                    callbacks=callbacks,
                    report=report,
                    cancel_event=cancel_event,
                    clone_manifest=clone_manifest,
                    clone_role=clone_role,
                    seen_clone_paths=seen_clone_paths,
                    progress=progress,
                )
                if cancel_event.is_set():
                    report.cancelled = True
                    return
            else:
                progress.skip_files([self._source_size(work.match.path)])

    def _process_secondary_sidecars(
        self,
        *,
        sidecar_work: tuple[SidecarWork, ...],
        source_root: Path,
        media_manifest_entry: CloneManifestEntry | None,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        clone_manifest: dict[str, CloneManifestEntry],
        clone_inventory: dict[str, CloneInventoryState],
        seen_clone_paths: set[str],
        clone_mismatches: list[CloneMismatchWork],
        volume_mismatches: list[VolumeMismatchWork],
        progress: SourceProgress,
    ) -> None:
        for work in sidecar_work:
            match = work.match
            relative_path = self._relative_source_path(source_root, match.path)
            sidecar_size = self._source_size(match.path)
            if not work.explicitly_selected:
                progress.skip_files([sidecar_size])
                continue

            inventory_state = clone_inventory.get(relative_path)
            if inventory_state is None:
                destination = (
                    match.destination_for(media_manifest_entry.destination)
                    if media_manifest_entry is not None
                    else None
                )
                self._queue_volume_mismatch(
                    callbacks,
                    report,
                    volume_mismatches,
                    match.path,
                    sidecar_size,
                    destination=destination,
                )
                continue
            if inventory_state != "selected":
                progress.skip_files([sidecar_size])
                continue

            if relative_path not in clone_manifest:
                progress.skip_files([sidecar_size])
                continue
            self._process_sidecar_file(
                work=work,
                source_root=source_root,
                media_context=None,
                media_destination=None,
                config=config,
                autoremove=autoremove,
                callbacks=callbacks,
                report=report,
                cancel_event=cancel_event,
                clone_manifest=clone_manifest,
                clone_role="secondary",
                seen_clone_paths=seen_clone_paths,
                progress=progress,
                clone_mismatches=clone_mismatches,
            )
            if cancel_event.is_set():
                report.cancelled = True
                return

    def _record_primary_inventory(
        self,
        clone_inventory: dict[str, CloneInventoryState],
        source_root: Path,
        source: Path,
        sidecar_work: tuple[SidecarWork, ...],
        *,
        state: CloneInventoryState,
    ) -> None:
        clone_inventory[self._relative_source_path(source_root, source)] = state
        for work in sidecar_work:
            if not work.explicitly_selected:
                continue
            clone_inventory[
                self._relative_source_path(source_root, work.match.path)
            ] = "selected"

    @staticmethod
    def _clone_inventory_state(
        selected: bool, result: FileCopyResult
    ) -> CloneInventoryState:
        if not selected:
            return "rating_excluded"
        if result.action == "skipped":
            return "skipped"
        return "selected"

    def _queue_volume_mismatch(
        self,
        callbacks: CopyCallbacks,
        report: CopyReport,
        mismatches: list[VolumeMismatchWork],
        source: Path,
        source_size: int,
        *,
        destination: Path | None = None,
        companion_matches: tuple[SidecarMatch, ...] = (),
    ) -> None:
        mismatches.append(
            VolumeMismatchWork(
                source=source,
                source_size=source_size,
                destination=destination,
                companion_matches=companion_matches,
            )
        )
        self._log_volume_mismatch(callbacks, report, source, source_size)
        for match in companion_matches:
            self._log_volume_mismatch(
                callbacks, report, match.path, self._source_size(match.path)
            )

    def _process_volume_mismatches(
        self,
        mismatches: list[VolumeMismatchWork],
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        sidecar_index: SidecarIndex,
        progress: SourceProgress,
    ) -> None:
        for mismatch in mismatches:
            if cancel_event.is_set():
                report.cancelled = True
                return

            file_index = progress.start_file(mismatch.source, mismatch.source_size)
            try:
                context = self._volume_mismatch_context(mismatch, config, sidecar_index)
                result = self._resolve_volume_mismatch(
                    mismatch,
                    context,
                    config,
                    autoremove,
                    callbacks,
                    report,
                    cancel_event,
                    file_index,
                    progress,
                )
            except Exception as exc:  # noqa: BLE001
                context = None
                result = FileCopyResult(
                    source=mismatch.source,
                    destination=mismatch.destination,
                    action="failed",
                    reason="volume mismatch copy failed",
                    error=str(exc),
                    size_bytes=mismatch.source_size,
                    volume_mismatch=True,
                )

            if result.copy_mode == "volume_mismatch_skipped":
                self._add_volume_mismatch_format_risks(
                    report, mismatch, "volume_mismatch_skipped"
                )
            elif result.copy_mode == "volume_mismatch_kept_existing":
                self._add_volume_mismatch_format_risks(
                    report, mismatch, "volume_mismatch_kept_existing"
                )
            self._emit_result(callbacks, report, result)
            progress.finish_file(mismatch.source_size)

            if cancel_event.is_set():
                report.cancelled = True
                return

            if context is not None and self._can_process_sidecars(result):
                for match in mismatch.companion_matches:
                    self._copy_volume_mismatch_sidecar(
                        match,
                        result.destination,
                        context,
                        config,
                        autoremove,
                        callbacks,
                        report,
                        cancel_event,
                        progress,
                    )
                    if cancel_event.is_set():
                        report.cancelled = True
                        return
            else:
                progress.skip_files(
                    [self._source_size(match.path) for match in mismatch.companion_matches]
                )

    @staticmethod
    def _add_volume_mismatch_format_risks(
        report: CopyReport,
        mismatch: VolumeMismatchWork,
        kind: Literal[
            "volume_mismatch_skipped", "volume_mismatch_kept_existing"
        ],
    ) -> None:
        report.add_format_risk(kind, mismatch.source)
        for companion in mismatch.companion_matches:
            report.add_format_risk(kind, companion.path)

    def _volume_mismatch_context(
        self,
        mismatch: VolumeMismatchWork,
        config: CameraCopyConfig,
        sidecar_index: SidecarIndex,
    ) -> FileContext:
        if mismatch.destination is not None:
            return self._build_direct_context(mismatch.source, mismatch.destination, config)
        return self._build_file_context(
            mismatch.source, config, sidecar_index, read_rating=False
        )

    def _resolve_volume_mismatch(
        self,
        mismatch: VolumeMismatchWork,
        context: FileContext,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        file_index: int,
        progress: SourceProgress,
    ) -> FileCopyResult:
        destination_exists = context.destination.exists()
        if destination_exists:
            verification_progress = self._verification_progress_callback(
                callbacks, progress.done_bytes, progress.total_bytes, file_index
            )
            hash_result = compare_sha256_cancellable(
                context.source, context.destination, cancel_event, verification_progress
            )
            if cancel_event.is_set() or hash_result.error == "cancelled":
                return FileCopyResult(
                    source=context.source,
                    destination=context.destination,
                    action="skipped",
                    reason="copy cancelled",
                    size_bytes=context.source_size,
                    volume_mismatch=True,
                )
            if hash_result.error:
                return FileCopyResult(
                    source=context.source,
                    destination=context.destination,
                    action="failed",
                    hash_ok=False,
                    reason="volume mismatch SHA256 comparison failed",
                    error=hash_result.error,
                    size_bytes=context.source_size,
                    volume_mismatch=True,
                )
            if hash_result.ok:
                action = "verified_existing"
                if autoremove:
                    remove_error = self._remove_source(context.source)
                    if remove_error is not None:
                        return FileCopyResult(
                            source=context.source,
                            destination=context.destination,
                            action="failed",
                            hash_ok=True,
                            reason="source removal failed",
                            error=remove_error,
                            size_bytes=context.source_size,
                            volume_mismatch=True,
                        )
                    action = "removed"
                return FileCopyResult(
                    source=context.source,
                    destination=context.destination,
                    action=action,
                    hash_ok=True,
                    size_bytes=context.source_size,
                    copy_mode="volume_mismatch_verified_existing",
                    volume_mismatch=True,
                )

        wait_started = time.perf_counter()
        decision = callbacks.ask_volume_mismatch_decision(
            context.source,
            context.destination,
            context.source_size,
            len(mismatch.companion_matches),
            destination_exists,
        )
        report.prompt_wait_seconds += time.perf_counter() - wait_started

        if decision == "cancel":
            cancel_event.set()
            return FileCopyResult(
                source=context.source,
                destination=context.destination if destination_exists else None,
                action="skipped",
                reason="copy cancelled by volume mismatch dialog",
                size_bytes=context.source_size,
                volume_mismatch=True,
            )
        if decision in {"skip", "keep_existing"}:
            return FileCopyResult(
                source=context.source,
                destination=context.destination if decision == "keep_existing" else None,
                action="skipped",
                reason=(
                    "volume mismatch; user kept existing destination"
                    if decision == "keep_existing"
                    else "volume mismatch; user chose skip"
                ),
                size_bytes=context.source_size,
                copy_mode=(
                    "volume_mismatch_kept_existing"
                    if decision == "keep_existing"
                    else "volume_mismatch_skipped"
                ),
                volume_mismatch=True,
            )

        collision_policy: CollisionPolicy = "overwrite"
        if decision == "keep_both":
            collision_policy = "rename"
        copy_context = FileContext(
            source=context.source,
            destination=context.destination,
            rating=context.rating,
            rating_source=context.rating_source,
            timestamp=context.timestamp,
            timestamp_source=context.timestamp_source,
            source_size=context.source_size,
            collision_policy=collision_policy,
        )
        result = self._copy_context(
            copy_context,
            config,
            autoremove,
            callbacks,
            report,
            cancel_event,
            file_index,
            force_sha256=True,
            source_done_bytes=progress.done_bytes,
            source_total_bytes=progress.total_bytes,
            companion_matches=mismatch.companion_matches,
        )
        self._mark_volume_mismatch_result(result, decision)
        return result

    def _copy_volume_mismatch_sidecar(
        self,
        match: SidecarMatch,
        media_destination: Path | None,
        media_context: FileContext,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        progress: SourceProgress,
    ) -> None:
        size_bytes = self._source_size(match.path)
        file_index = progress.start_file(match.path, size_bytes)
        if media_destination is None:
            result = FileCopyResult(
                source=match.path,
                destination=None,
                action="failed",
                reason="volume mismatch copy failed",
                error="media destination is unavailable",
                size_bytes=size_bytes,
                volume_mismatch=True,
            )
        else:
            context = self._build_sidecar_context(
                match, media_destination, media_context, config
            )
            result = self._copy_context(
                context,
                config,
                autoremove,
                callbacks,
                report,
                cancel_event,
                file_index,
                force_sha256=True,
                source_done_bytes=progress.done_bytes,
                source_total_bytes=progress.total_bytes,
            )
            self._mark_volume_mismatch_result(result, "copy")
        self._emit_result(callbacks, report, result)
        progress.finish_file(size_bytes)

    @staticmethod
    def _mark_volume_mismatch_result(
        result: FileCopyResult, decision: VolumeMismatchDecision
    ) -> None:
        result.volume_mismatch = True
        if result.failed:
            return
        if result.action == "skipped":
            if decision == "keep_existing":
                result.copy_mode = "volume_mismatch_kept_existing"
            else:
                result.copy_mode = "volume_mismatch_skipped"
            return
        if result.copy_mode == "verified_existing":
            result.copy_mode = "volume_mismatch_verified_existing"
        elif result.copy_mode == "renamed_copy":
            result.copy_mode = "volume_mismatch_kept_both"
        elif result.copy_mode == "overwrote":
            result.copy_mode = "volume_mismatch_replaced"
        else:
            result.copy_mode = "volume_mismatch_copied"

    @staticmethod
    def _log_volume_mismatch(
        callbacks: CopyCallbacks,
        report: CopyReport,
        source: Path,
        source_size: int,
    ) -> None:
        CopyEngine._log(
            callbacks,
            report,
            f"VOLUME MISMATCH: {source} "
            f"({format_log_file_size(source_size)}, not found on the first-source volume; "
            "queued for review)",
        )

    @staticmethod
    def _log_discovered_files(
        callbacks: CopyCallbacks,
        report: CopyReport,
        media_count: int,
        sidecar_count: int,
    ) -> None:
        if sidecar_count:
            CopyEngine._log(
                callbacks,
                report,
                f"Found {media_count} media file(s) and {sidecar_count} matching sidecar(s)",
            )
            return
        CopyEngine._log(callbacks, report, f"Found {media_count} file(s)")

    @staticmethod
    def _prepare_sidecar_work(
        files: list[Path], config: CameraCopyConfig
    ) -> tuple[SidecarIndex, list[Path], dict[Path, tuple[SidecarWork, ...]]]:
        sidecar_index = SidecarIndex()
        if not config.copysidecars:
            return sidecar_index, files, {}
        explicitly_selected = set(files)
        sidecars_by_media = CopyEngine._build_sidecar_map(
            [path for path in files if not is_sidecar_path(path)],
            sidecar_index,
            explicitly_selected,
        )
        claimed_sidecars = {
            work.match.path
            for works in sidecars_by_media.values()
            for work in works
        }
        media_and_standalone_files = [
            path for path in files if path not in claimed_sidecars
        ]
        return sidecar_index, media_and_standalone_files, sidecars_by_media

    def _process_media_file(
        self,
        *,
        source: Path,
        relative_path: str,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        clone_manifest: dict[str, CloneManifestEntry] | None,
        clone_role: CloneRole,
        sidecar_index: SidecarIndex,
        companion_matches: tuple[SidecarMatch, ...],
        explicitly_selected_sidecar: bool,
        file_index: int,
        source_done_bytes: int,
        source_total_bytes: int,
    ) -> tuple[FileCopyResult, FileContext | None, bool]:
        try:
            context = self._build_file_context(
                source,
                config,
                sidecar_index,
                read_rating=not explicitly_selected_sidecar,
            )
            selected = explicitly_selected_sidecar or not (
                config.minrating > 0 and (context.rating or 0) < config.minrating
            )
            if selected:
                result = self._copy_context(
                    context,
                    config,
                    autoremove,
                    callbacks,
                    report,
                    cancel_event,
                    file_index,
                    force_sha256=clone_role == "primary",
                    source_done_bytes=source_done_bytes,
                    source_total_bytes=source_total_bytes,
                    companion_matches=companion_matches,
                )
            else:
                result = self._skipped_result(
                    context,
                    destination=None,
                    reason=f"rating {context.rating or 0} below minimum {config.minrating}",
                )
                report.add_format_risk("rating_excluded", source)
        except Exception as exc:  # noqa: BLE001
            return (
                FileCopyResult(source=source, destination=None, action="failed", error=str(exc)),
                None,
                False,
            )

        if clone_role == "primary" and clone_manifest is not None and not cancel_event.is_set():
            result = self._record_primary_clone_result(
                relative_path, result, clone_manifest, cancel_event
            )
        return result, context, selected

    def _process_sidecar_file(
        self,
        *,
        work: SidecarWork,
        source_root: Path,
        media_context: FileContext | None,
        media_destination: Path | None,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        clone_manifest: dict[str, CloneManifestEntry] | None,
        clone_role: CloneRole,
        seen_clone_paths: set[str],
        progress: SourceProgress,
        clone_mismatches: list[CloneMismatchWork] | None = None,
    ) -> None:
        match = work.match
        sidecar_size = self._source_size(match.path)
        file_index = progress.start_file(match.path, sidecar_size)
        relative_path = self._relative_source_path(source_root, match.path)

        if clone_role == "secondary" and clone_manifest is not None:
            seen_clone_paths.add(relative_path)
            entry = clone_manifest[relative_path]
            result = self._process_clone_second_file(
                source=match.path,
                source_size=sidecar_size,
                entry=entry,
                autoremove=autoremove,
                callbacks=callbacks,
                report=report,
                cancel_event=cancel_event,
                file_index=file_index,
                source_done_bytes=progress.done_bytes,
                source_total_bytes=progress.total_bytes,
                clone_mismatches=clone_mismatches,
            )
        else:
            context = self._build_sidecar_copy_context(
                work,
                media_destination,
                media_context,
                config,
            )
            result = self._copy_context(
                context,
                config,
                autoremove,
                callbacks,
                report,
                cancel_event,
                file_index,
                force_sha256=(
                    clone_role == "primary" and work.explicitly_selected
                ),
                source_done_bytes=progress.done_bytes,
                source_total_bytes=progress.total_bytes,
            )
            if (
                clone_role == "primary"
                and clone_manifest is not None
                and work.explicitly_selected
            ):
                result = self._record_primary_clone_result(
                    relative_path, result, clone_manifest, cancel_event
                )

        if result is not None:
            self._emit_result(callbacks, report, result)
        progress.finish_file(sidecar_size)

    def _copy_context(
        self,
        context: FileContext,
        config: CameraCopyConfig,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        file_index: int,
        *,
        force_sha256: bool = False,
        source_done_bytes: int = 0,
        source_total_bytes: int = 0,
        companion_matches: tuple[SidecarMatch, ...] = (),
    ) -> FileCopyResult:
        source = context.source
        try:
            destination = context.destination
            copy_mode: CopyMode = "copied"

            if destination.exists() and context.collision_policy == "ask":
                wait_started = time.perf_counter()
                decision = callbacks.ask_collision_decision(source, destination)
                report.prompt_wait_seconds += time.perf_counter() - wait_started
                if decision == "cancel":
                    cancel_event.set()
                    return self._skipped_result(
                        context, destination, "copy cancelled by existing-file prompt"
                    )
                if decision == "skip":
                    return self._skipped_result(
                        context, destination, "destination exists; user chose skip"
                    )
                if decision == "rename":
                    destination = self._unique_group_destination(
                        destination, companion_matches
                    )
                    copy_mode = "renamed_copy"
                elif decision == "overwrite":
                    copy_mode = "overwrote"

            elif destination.exists() and context.collision_policy == "skip":
                return self._skipped_result(
                    context, destination, "destination exists and overwrite is disabled"
                )
            elif destination.exists() and context.collision_policy == "rename":
                destination = self._unique_group_destination(destination, companion_matches)
                copy_mode = "renamed_copy"
            elif destination.exists():
                copy_mode = "overwrote"

            if self._same_file(source, destination):
                return self._failed_result(
                    context, destination, "source and destination are the same file"
                )

            copy_result = self._copy_to_destination(
                source=source,
                destination=destination,
                checkhash=config.checkhash or force_sha256,
                durable_writes=config.durablewrites,
                callbacks=callbacks,
                cancel_event=cancel_event,
                file_index=file_index,
                source_done_bytes=source_done_bytes,
                source_total_bytes=source_total_bytes,
            )
            if copy_result.cancelled:
                return FileCopyResult(
                    source=source,
                    destination=destination,
                    action="skipped",
                    rating=context.rating,
                    timestamp=context.timestamp,
                    reason="copy cancelled",
                    timestamp_source=context.timestamp_source,
                    rating_source=context.rating_source,
                    size_bytes=context.source_size,
                    bytes_copied=copy_result.bytes_copied,
                )
            if not copy_result.ok:
                failure_reason = self._copy_failure_reason(
                    copy_result, config.checkhash or force_sha256
                )
                return FileCopyResult(
                    source=source,
                    destination=destination,
                    action="failed",
                    hash_ok=copy_result.hash_ok,
                    rating=context.rating,
                    timestamp=context.timestamp,
                    reason=failure_reason,
                    timestamp_source=context.timestamp_source,
                    rating_source=context.rating_source,
                    error=copy_result.error,
                    size_bytes=context.source_size,
                    bytes_copied=copy_result.bytes_copied,
                )

            return self._finalize_successful_copy(
                context=context,
                destination=destination,
                autoremove=autoremove,
                bytes_copied=copy_result.bytes_copied,
                copy_mode=copy_mode,
                hash_ok=copy_result.hash_ok,
            )
        except Exception as exc:  # noqa: BLE001
            return FileCopyResult(source=source, destination=None, action="failed", error=str(exc))

    def _process_clone_second_file(
        self,
        *,
        source: Path,
        source_size: int,
        entry: CloneManifestEntry,
        autoremove: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
        file_index: int,
        source_done_bytes: int = 0,
        source_total_bytes: int = 0,
        clone_mismatches: list[CloneMismatchWork] | None = None,
    ) -> FileCopyResult | None:
        verification_progress = self._verification_progress_callback(
            callbacks,
            source_done_bytes,
            source_total_bytes,
            file_index,
        )
        hash_result = compare_sha256_cancellable(
            source, entry.destination, cancel_event, verification_progress
        )
        if cancel_event.is_set() or hash_result.error == "cancelled":
            return FileCopyResult(
                source=source,
                destination=entry.destination,
                action="skipped",
                reason="copy cancelled",
                size_bytes=source_size,
            )
        if hash_result.error:
            return FileCopyResult(
                source=source,
                destination=entry.destination,
                action="failed",
                hash_ok=False,
                reason="clone SHA256 comparison failed",
                error=hash_result.error,
                size_bytes=source_size,
            )
        if hash_result.ok:
            return self._clone_verified_result(source, entry.destination, source_size, autoremove)

        if clone_mismatches is None:
            raise RuntimeError("clone mismatch queue is unavailable")
        clone_mismatches.append(
            CloneMismatchWork(
                source=source,
                source_size=source_size,
                entry=entry,
                file_index=file_index,
            )
        )
        self._log(
            callbacks,
            report,
            f"CLONE MISMATCH: {source} -> {entry.destination} "
            f"({format_log_file_size(source_size)}, differs from the copied first-source file; "
            "queued for review)",
        )
        return None

    def _process_clone_mismatches(
        self,
        mismatches: list[CloneMismatchWork],
        autoremove: bool,
        durable_writes: bool,
        callbacks: CopyCallbacks,
        report: CopyReport,
        cancel_event: Event,
    ) -> None:
        for mismatch in mismatches:
            if cancel_event.is_set():
                report.cancelled = True
                return

            wait_started = time.perf_counter()
            response = callbacks.ask_clone_mismatch_decision(
                mismatch.source, mismatch.entry.destination, autoremove
            )
            report.prompt_wait_seconds += time.perf_counter() - wait_started
            result = self._resolve_clone_mismatch(
                mismatch,
                response.decision,
                autoremove and response.remove_source,
                durable_writes,
                callbacks,
                cancel_event,
            )
            if result.copy_mode == "clone_mismatch_skipped":
                report.add_format_risk("clone_kept_first_source", mismatch.source)
            elif (
                result.copy_mode == "clone_mismatch_replaced"
                and mismatch.entry.source.exists()
            ):
                report.add_format_risk(
                    "clone_used_second_source", mismatch.entry.source
                )
            self._emit_result(callbacks, report, result)
            if cancel_event.is_set():
                report.cancelled = True
                return

    def _resolve_clone_mismatch(
        self,
        mismatch: CloneMismatchWork,
        decision: CloneMismatchDecision,
        remove_source: bool,
        durable_writes: bool,
        callbacks: CopyCallbacks,
        cancel_event: Event,
    ) -> FileCopyResult:
        source = mismatch.source
        entry = mismatch.entry
        source_size = mismatch.source_size
        first_source_size = self._source_size(entry.destination)

        if decision == "cancel":
            cancel_event.set()
            return FileCopyResult(
                source=source,
                destination=entry.destination,
                action="skipped",
                reason="copy cancelled by clone mismatch dialog",
                size_bytes=source_size,
            )
        if decision == "skip":
            return FileCopyResult(
                source=source,
                destination=entry.destination,
                action="skipped",
                reason="clone mismatch; user kept first-source version",
                size_bytes=source_size,
                copy_mode="clone_mismatch_skipped",
                first_source_destination=entry.destination,
                first_source_size_bytes=first_source_size,
            )

        destination = entry.destination
        copy_mode: CopyMode = "clone_mismatch_replaced"
        if decision == "keep_both":
            destination = self._clone_mismatch_destination(entry.destination)
            copy_mode = "clone_mismatch_kept"

        copy_result = self._copy_to_destination(
            source=source,
            destination=destination,
            checkhash=True,
            durable_writes=durable_writes,
            callbacks=callbacks,
            cancel_event=cancel_event,
            file_index=mismatch.file_index,
        )
        if copy_result.cancelled:
            return FileCopyResult(
                source=source,
                destination=destination,
                action="skipped",
                reason="copy cancelled",
                size_bytes=source_size,
                bytes_copied=copy_result.bytes_copied,
            )
        if not copy_result.ok:
            return FileCopyResult(
                source=source,
                destination=destination,
                action="failed",
                hash_ok=copy_result.hash_ok,
                reason=self._copy_failure_reason(copy_result, checkhash=True),
                error=copy_result.error,
                size_bytes=source_size,
                bytes_copied=copy_result.bytes_copied,
            )

        if remove_source:
            remove_error = self._remove_source(source)
            if remove_error is not None:
                return FileCopyResult(
                    source=source,
                    destination=destination,
                    action="failed",
                    hash_ok=True,
                    reason="source removal failed",
                    error=remove_error,
                    size_bytes=source_size,
                    bytes_copied=copy_result.bytes_copied,
                    copy_mode=copy_mode,
                )
            action = "removed"
        else:
            action = "copied"

        return FileCopyResult(
            source=source,
            destination=destination,
            action=action,
            hash_ok=True,
            size_bytes=source_size,
            bytes_copied=copy_result.bytes_copied,
            copy_mode=copy_mode,
            first_source_destination=entry.destination,
            first_source_size_bytes=first_source_size,
        )

    @staticmethod
    def _record_primary_clone_result(
        relative_path: str,
        result: FileCopyResult,
        clone_manifest: dict[str, CloneManifestEntry],
        cancel_event: Event,
    ) -> FileCopyResult:
        if (
            result.failed
            or result.destination is None
            or result.action == "skipped"
            or cancel_event.is_set()
        ):
            return result

        clone_manifest[relative_path] = CloneManifestEntry(
            relative_path=relative_path,
            source=result.source,
            destination=result.destination,
            size_bytes=result.size_bytes,
        )
        return result

    def _emit_missing_clone_files(
        self,
        callbacks: CopyCallbacks,
        report: CopyReport,
        source_root: Path,
        clone_manifest: dict[str, CloneManifestEntry],
        seen_clone_paths: set[str],
    ) -> None:
        for relative_path in sorted(set(clone_manifest) - seen_clone_paths):
            entry = clone_manifest[relative_path]
            self._emit_result(
                callbacks,
                report,
                FileCopyResult(
                    source=source_root / relative_path,
                    destination=None,
                    action="failed",
                    reason=(
                        "selected on the first-source volume, not found on the "
                        "second-source volume"
                    ),
                    size_bytes=entry.size_bytes,
                ),
            )

    def _clone_verified_result(
        self,
        source: Path,
        destination: Path,
        source_size: int,
        autoremove: bool,
    ) -> FileCopyResult:
        if autoremove:
            remove_error = self._remove_source(source)
            if remove_error is not None:
                return FileCopyResult(
                    source=source,
                    destination=destination,
                    action="failed",
                    hash_ok=True,
                    reason="source removal failed",
                    error=remove_error,
                    size_bytes=source_size,
                )
            return FileCopyResult(
                source=source,
                destination=destination,
                action="removed",
                hash_ok=True,
                size_bytes=source_size,
                copy_mode="clone_verified",
            )
        return FileCopyResult(
            source=source,
            destination=destination,
            action="verified_existing",
            hash_ok=True,
            size_bytes=source_size,
            copy_mode="clone_verified",
        )

    @staticmethod
    def _source_action_label(clone_role: CloneRole) -> str:
        if clone_role == "secondary":
            return "Verifying clone"
        return "Copying"

    @staticmethod
    def _phase_start_message(clone_role: CloneRole, source_index: int, source_count: int) -> str:
        if clone_role == "secondary":
            return "Starting clone verification..."
        if source_count > 1 and source_index == 1:
            return "Starting first-source file copy..."
        if source_count > 1 and source_index == 2:
            return "Starting second-source file copy..."
        return "Starting file copy..."

    def _warm_metadata_cache(
        self, files: list[Path], config: CameraCopyConfig, cancel_event: Event
    ) -> None:
        if not config.useembeddedmetadata:
            return
        warmed_services: set[int] = set()
        if config.minrating > 0:
            self.rating_reader.warm_cache(files, cancel_event=cancel_event)
            warmed_services.add(id(self.rating_reader.exiftool))
        if (
            self._date_folder_enabled(config)
            and id(self.timestamp_resolver.exiftool) not in warmed_services
        ):
            self.timestamp_resolver.warm_cache(files, cancel_event=cancel_event)

    def _build_file_context(
        self,
        source: Path,
        config: CameraCopyConfig,
        sidecars: SidecarIndex,
        *,
        read_rating: bool = True,
    ) -> FileContext:
        source_size = source.stat().st_size
        if read_rating:
            rating, rating_source = self._read_rating(source, config, sidecars)
        else:
            rating, rating_source = None, None
        timestamp, timestamp_source = self._resolve_timestamp(source, config, sidecars)
        destination = build_destination_file(config, source, timestamp)
        return FileContext(
            source=source,
            destination=destination,
            rating=rating,
            rating_source=rating_source,
            timestamp=timestamp,
            timestamp_source=timestamp_source,
            source_size=source_size,
            collision_policy=config.collisionpolicy,
        )

    @staticmethod
    def _build_direct_context(
        source: Path,
        destination: Path,
        config: CameraCopyConfig,
    ) -> FileContext:
        timestamp = datetime.fromtimestamp(source.stat().st_mtime)
        return FileContext(
            source=source,
            destination=destination,
            rating=None,
            rating_source=None,
            timestamp=timestamp,
            timestamp_source="file_mtime",
            source_size=source.stat().st_size,
            collision_policy=config.collisionpolicy,
        )

    def _resolve_timestamp(
        self,
        source: Path,
        config: CameraCopyConfig,
        sidecars: SidecarIndex,
    ) -> tuple[datetime, str]:
        return self.timestamp_resolver.resolve(
            source,
            fix_sony_timestamps=config.fixsonytimestamps,
            use_embedded_metadata=config.useembeddedmetadata,
            sidecars=sidecars,
        )

    @staticmethod
    def _build_sidecar_map(
        media_files: list[Path],
        sidecars: SidecarIndex,
        explicitly_selected: set[Path],
    ) -> dict[Path, tuple[SidecarWork, ...]]:
        claimed: set[Path] = set()
        result: dict[Path, tuple[SidecarWork, ...]] = {}
        for media_path in media_files:
            matches = tuple(
                match for match in sidecars.matching(media_path) if match.path not in claimed
            )
            if not matches:
                continue
            claimed.update(match.path for match in matches)
            result[media_path] = tuple(
                SidecarWork(
                    match=match,
                    explicitly_selected=match.path in explicitly_selected,
                )
                for match in matches
            )
        return result

    @staticmethod
    def _build_sidecar_context(
        match: SidecarMatch,
        media_destination: Path,
        media_context: FileContext,
        config: CameraCopyConfig,
    ) -> FileContext:
        return FileContext(
            source=match.path,
            destination=match.destination_for(media_destination),
            rating=None,
            rating_source=None,
            timestamp=media_context.timestamp,
            timestamp_source=media_context.timestamp_source,
            source_size=match.path.stat().st_size,
            collision_policy=config.collisionpolicy,
        )

    @staticmethod
    def _build_sidecar_copy_context(
        work: SidecarWork,
        media_destination: Path | None,
        media_context: FileContext | None,
        config: CameraCopyConfig,
    ) -> FileContext:
        if media_context is not None and media_destination is not None:
            return CopyEngine._build_sidecar_context(
                work.match,
                media_destination,
                media_context,
                config,
            )
        if not work.explicitly_selected:
            raise ValueError("companion sidecar destination is unavailable")

        source = work.match.path
        timestamp = datetime.fromtimestamp(source.stat().st_mtime)
        return FileContext(
            source=source,
            destination=build_destination_file(config, source, timestamp),
            rating=None,
            rating_source=None,
            timestamp=timestamp,
            timestamp_source="file_mtime",
            source_size=source.stat().st_size,
            collision_policy=config.collisionpolicy,
        )

    @staticmethod
    def _can_process_sidecars(result: FileCopyResult) -> bool:
        return not result.failed and result.destination is not None and result.action != "skipped"

    def _finalize_successful_copy(
        self,
        context: FileContext,
        destination: Path,
        autoremove: bool,
        bytes_copied: int,
        copy_mode: CopyMode,
        hash_ok: bool | None,
    ) -> FileCopyResult:
        if autoremove:
            remove_error = self._remove_source(context.source)
            if remove_error is not None:
                return FileCopyResult(
                    source=context.source,
                    destination=destination,
                    action="failed",
                    hash_ok=hash_ok,
                    rating=context.rating,
                    timestamp=context.timestamp,
                    timestamp_source=context.timestamp_source,
                    rating_source=context.rating_source,
                    error=remove_error,
                    reason="source removal failed",
                    size_bytes=context.source_size,
                    bytes_copied=bytes_copied,
                )
            return FileCopyResult(
                source=context.source,
                destination=destination,
                action="removed",
                hash_ok=hash_ok,
                rating=context.rating,
                timestamp=context.timestamp,
                timestamp_source=context.timestamp_source,
                rating_source=context.rating_source,
                reason="source removed",
                size_bytes=context.source_size,
                bytes_copied=bytes_copied,
                copy_mode=copy_mode,
            )

        return FileCopyResult(
            source=context.source,
            destination=destination,
            action="copied",
            hash_ok=hash_ok,
            rating=context.rating,
            timestamp=context.timestamp,
            timestamp_source=context.timestamp_source,
            rating_source=context.rating_source,
            reason=None,
            size_bytes=context.source_size,
            bytes_copied=bytes_copied,
            copy_mode=copy_mode,
        )

    def _copy_to_destination(
        self,
        source: Path,
        destination: Path,
        checkhash: bool,
        durable_writes: bool,
        callbacks: CopyCallbacks,
        cancel_event: Event,
        file_index: int,
        source_done_bytes: int = 0,
        source_total_bytes: int = 0,
    ) -> VerifiedCopy:
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            source_stat = source.stat()
            source_size = source_stat.st_size
            free_bytes = shutil.disk_usage(destination.parent).free
        except OSError as exc:
            return VerifiedCopy(ok=False, hash_ok=None, error=f"cannot check free space: {exc}")
        if free_bytes < source_size:
            return VerifiedCopy(
                ok=False,
                hash_ok=None,
                error=(
                    f"not enough free space in {destination.parent} "
                    f"({free_bytes:,} free, {source_size:,} needed)"
                ),
            )

        temp_path: Path | None = None
        bytes_done = 0
        source_digest = hashlib.sha256() if checkhash else None
        try:
            temp_path = self._create_temporary_destination(destination)
            with source.open("rb") as src, temp_path.open("wb") as dst:
                while True:
                    if cancel_event.is_set():
                        break
                    chunk = src.read(COPY_CHUNK_SIZE)
                    if not chunk:
                        break
                    dst.write(chunk)
                    if source_digest is not None:
                        source_digest.update(chunk)
                    bytes_done += len(chunk)
                    callbacks.emit_byte_progress(bytes_done, source_size, file_index)
                    if source_total_bytes > 0:
                        callbacks.emit_source_progress(
                            source_done_bytes + bytes_done, source_total_bytes, True
                        )
                if durable_writes and not cancel_event.is_set():
                    dst.flush()
                    os.fsync(dst.fileno())
            if cancel_event.is_set():
                self._safe_unlink(temp_path)
                return VerifiedCopy(
                    ok=False,
                    hash_ok=None,
                    bytes_copied=bytes_done,
                    cancelled=True,
                    error="cancelled",
                )

            if _source_stat_changed(source_stat, source.stat()):
                self._safe_unlink(temp_path)
                return VerifiedCopy(
                    ok=False,
                    hash_ok=False if checkhash else None,
                    bytes_copied=bytes_done,
                    error="source changed during copy",
                )

            shutil.copystat(source, temp_path)

            if checkhash:
                try:
                    destination_hash = sha256_file(temp_path, cancel_event=cancel_event)
                except InterruptedError:
                    self._safe_unlink(temp_path)
                    return VerifiedCopy(
                        ok=False,
                        hash_ok=None,
                        bytes_copied=bytes_done,
                        cancelled=True,
                        error="cancelled",
                    )
                verified = source_digest is not None and (
                    source_digest.hexdigest() == destination_hash
                )
            else:
                verified = temp_path.stat().st_size == source_size

            if _source_stat_changed(source_stat, source.stat()):
                self._safe_unlink(temp_path)
                return VerifiedCopy(
                    ok=False,
                    hash_ok=False if checkhash else None,
                    bytes_copied=bytes_done,
                    error="source changed during copy",
                )

            if cancel_event.is_set():
                self._safe_unlink(temp_path)
                return VerifiedCopy(
                    ok=False,
                    hash_ok=None,
                    bytes_copied=bytes_done,
                    cancelled=True,
                    error="cancelled",
                )
            if not verified:
                self._safe_unlink(temp_path)
                return VerifiedCopy(
                    ok=False,
                    hash_ok=False if checkhash else None,
                    bytes_copied=bytes_done,
                    error="verification failed",
                )
            os.replace(temp_path, destination)
            if durable_writes:
                self._fsync_directory_best_effort(destination.parent)
            return VerifiedCopy(
                ok=True, hash_ok=True if checkhash else None, bytes_copied=bytes_done
            )
        except PermissionError as exc:
            if temp_path is not None:
                self._safe_unlink(temp_path)
            return VerifiedCopy(
                ok=False,
                hash_ok=None,
                bytes_copied=bytes_done,
                error=_friendly_permission_error(exc),
            )
        except Exception as exc:  # noqa: BLE001
            if temp_path is not None:
                self._safe_unlink(temp_path)
            return VerifiedCopy(
                ok=False,
                hash_ok=None,
                bytes_copied=bytes_done,
                error=_friendly_exception_error(exc),
            )

    @staticmethod
    def _copy_failure_reason(copy_result: VerifiedCopy, checkhash: bool) -> str:
        if copy_result.hash_ok is False:
            return "SHA256 FAILED" if checkhash else "verification failed"
        return copy_result.error or "copy failed"

    @staticmethod
    def _date_folder_enabled(config: CameraCopyConfig) -> bool:
        return bool(config.datetimestring or config.folderprefix or config.folderpostfix)

    def _read_rating(
        self,
        source: Path,
        config: CameraCopyConfig,
        sidecars: SidecarIndex,
    ) -> tuple[int | None, str | None]:
        if config.minrating <= 0:
            return None, None
        lookup = self.rating_reader.read_rating_with_source(
            source, config.useembeddedmetadata, sidecars
        )
        return lookup.rating, lookup.source

    @staticmethod
    def _relative_source_path(source_root: Path, source: Path) -> str:
        try:
            return source.relative_to(source_root).as_posix()
        except ValueError:
            return source.name

    @staticmethod
    def _clone_mismatch_destination(destination: Path) -> Path:
        return CopyEngine._unique_destination(destination)

    @staticmethod
    def _unique_group_destination(
        destination: Path, companion_matches: tuple[SidecarMatch, ...]
    ) -> Path:
        if not destination.exists() and all(
            not match.destination_for(destination).exists() for match in companion_matches
        ):
            return destination
        stem = destination.stem
        suffix = destination.suffix
        for index in range(1, 10_000):
            candidate = destination.with_name(f"{stem}_{index:03d}{suffix}")
            if candidate.exists():
                continue
            if any(match.destination_for(candidate).exists() for match in companion_matches):
                continue
            return candidate
        raise RuntimeError(f"Could not create a unique destination for {destination}")

    @staticmethod
    def _unique_destination(destination: Path) -> Path:
        if not destination.exists():
            return destination
        stem = destination.stem
        suffix = destination.suffix
        for index in range(1, 10_000):
            candidate = destination.with_name(f"{stem}_{index:03d}{suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Could not create a unique destination for {destination}")

    @staticmethod
    def _create_temporary_destination(destination: Path) -> Path:
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".cameracopy.tmp",
            dir=destination.parent,
        )
        os.close(fd)
        return Path(temp_name)

    @staticmethod
    def _fsync_directory_best_effort(path: Path) -> None:
        if not hasattr(os, "O_RDONLY"):
            return
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        try:
            fd = os.open(path, flags)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            return
        finally:
            os.close(fd)

    @staticmethod
    def _safe_unlink(path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            logger.warning("Could not remove CameraCopy temporary file %s: %s", path, exc)

    @staticmethod
    def _remove_source(source: Path) -> str | None:
        try:
            source.unlink()
        except OSError as exc:
            return str(exc)
        return None

    @staticmethod
    def _source_progress_callback(
        callbacks: CopyCallbacks,
        source_done_bytes: int,
        source_total_bytes: int,
    ) -> HashProgressCallback | None:
        if source_total_bytes <= 0:
            return None

        def progress(bytes_done: int, _file_total: int) -> None:
            callbacks.emit_source_progress(source_done_bytes + bytes_done, source_total_bytes, True)

        return progress

    @staticmethod
    def _verification_progress_callback(
        callbacks: CopyCallbacks,
        source_done_bytes: int,
        source_total_bytes: int,
        file_index: int,
    ) -> HashProgressCallback:
        source_progress = CopyEngine._source_progress_callback(
            callbacks, source_done_bytes, source_total_bytes
        )

        def progress(bytes_done: int, file_total: int) -> None:
            callbacks.emit_byte_progress(bytes_done, file_total, file_index)
            if source_progress is not None:
                source_progress(bytes_done, file_total)

        return progress

    @staticmethod
    def _source_size(source: Path) -> int:
        try:
            return source.stat().st_size
        except OSError:
            return 0

    @staticmethod
    def _same_file(source: Path, destination: Path) -> bool:
        try:
            return source.samefile(destination)
        except FileNotFoundError:
            return False
        except OSError:
            return False

    @staticmethod
    def _skipped_result(
        context: FileContext, destination: Path | None, reason: str
    ) -> FileCopyResult:
        return FileCopyResult(
            source=context.source,
            destination=destination,
            action="skipped",
            rating=context.rating,
            timestamp=context.timestamp,
            timestamp_source=context.timestamp_source,
            rating_source=context.rating_source,
            reason=reason,
            size_bytes=context.source_size,
        )

    @staticmethod
    def _failed_result(
        context: FileContext, destination: Path | None, error: str
    ) -> FileCopyResult:
        return FileCopyResult(
            source=context.source,
            destination=destination,
            action="failed",
            rating=context.rating,
            timestamp=context.timestamp,
            timestamp_source=context.timestamp_source,
            rating_source=context.rating_source,
            reason="copy_blocked",
            error=error,
            size_bytes=context.source_size,
        )

    def _emit_result(
        self, callbacks: CopyCallbacks, report: CopyReport, result: FileCopyResult
    ) -> None:
        report.add_result(result)
        callbacks.emit_result(result)
        message = result_message(result)
        message_type = classify_file_result(result)
        if not callbacks.emit_typed_log(message, message_type.name):
            callbacks.emit_log(message)
        report.add_log(message)

    @staticmethod
    def _log(callbacks: CopyCallbacks, report: CopyReport, message: str) -> None:
        message_type = classify_log_message(message)
        if not callbacks.emit_typed_log(message, message_type.name):
            callbacks.emit_log(message)
        report.add_log(message)

