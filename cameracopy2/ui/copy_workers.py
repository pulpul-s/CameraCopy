from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from threading import Event
from time import monotonic

from PySide6.QtCore import QObject, Signal, Slot

from cameracopy2.core.copy_engine import CopyEngine
from cameracopy2.core.log_messages import LogMessageType
from cameracopy2.models import (
    CloneMismatchDecision,
    CloneMismatchResponse,
    CollisionDecision,
    CopyCallbacks,
    CopyJob,
    CopyReport,
    FileCopyResult,
    VolumeInfo,
    VolumeMismatchDecision,
)
from cameracopy2.platform.windows_com import windows_com_initialized
from cameracopy2.services.format_service import FormatService

logger = logging.getLogger(__name__)

PROGRESS_SIGNAL_INTERVAL_SECONDS = 0.1


class CopyWorker(QObject):
    """Run one copy job outside the GUI thread and bridge prompt decisions."""

    log = Signal(str, str)
    progress = Signal(int, int)
    source_started = Signal(int, int, str, object)
    source_progress = Signal(object, object, bool)
    file_started = Signal(str, int, int, object)
    byte_progress = Signal(object, object, int)
    result = Signal(object)
    ask_collision = Signal(str, str)
    ask_clone_mismatch = Signal(str, str, bool)
    ask_volume_mismatch = Signal(str, str, object, object, int, bool)
    finished = Signal(object)

    def __init__(self, job: CopyJob, cancel_event: Event) -> None:
        super().__init__()
        self.job = job
        self.cancel_event = cancel_event
        self._collision_event = Event()
        self._collision_decision: CollisionDecision = "skip"
        self._collision_decision_for_all: CollisionDecision | None = None
        self._clone_mismatch_event = Event()
        self._clone_mismatch_response = CloneMismatchResponse(
            decision="skip",
            remove_source=False,
        )
        self._clone_mismatch_response_for_all: CloneMismatchResponse | None = None
        self._volume_mismatch_event = Event()
        self._volume_mismatch_decision: VolumeMismatchDecision = "skip"
        self._volume_mismatch_decisions_for_all: dict[bool, VolumeMismatchDecision] = {}
        self._last_source_progress_emit: float | None = None
        self._last_byte_progress_emit: float | None = None
        self._last_byte_progress_file_index = 0

    @Slot()
    def run(self) -> None:
        try:
            callbacks = CopyCallbacks(
                typed_log=self.log.emit,
                progress=self.progress.emit,
                source_started=self.source_started.emit,
                source_progress=self._emit_source_progress,
                result=lambda result: self.result.emit(result),
                file_started=self._emit_file_started,
                byte_progress=self._emit_byte_progress,
                collision_decision=self.request_collision_decision,
                clone_mismatch_decision=self.request_clone_mismatch_decision,
                volume_mismatch_decision=self.request_volume_mismatch_decision,
            )
            report = CopyEngine().run(self.job, callbacks, self.cancel_event)
        except Exception as exc:  # noqa: BLE001 - convert worker failures into a report
            logger.exception("Unexpected copy-worker failure")
            report = self._internal_error_report(exc)
        self.finished.emit(report)

    def _emit_file_started(self, path: Path, index: int, total: int, size: int) -> None:
        self.file_started.emit(str(path), index, total, size)

    def _emit_source_progress(
        self, bytes_done: int, total_bytes: int, metered: bool
    ) -> None:
        now = monotonic()
        force = (
            not metered
            or bytes_done <= 0
            or total_bytes <= 0
            or bytes_done >= total_bytes
        )
        if not self._progress_signal_due(self._last_source_progress_emit, now, force):
            return
        if metered or self._last_source_progress_emit is None:
            self._last_source_progress_emit = now
        self.source_progress.emit(bytes_done, total_bytes, metered)

    def _emit_byte_progress(
        self, bytes_done: int, total_bytes: int, file_index: int
    ) -> None:
        now = monotonic()
        new_file = file_index != self._last_byte_progress_file_index
        force = (
            new_file
            or bytes_done <= 0
            or total_bytes <= 0
            or bytes_done >= total_bytes
        )
        if not self._progress_signal_due(self._last_byte_progress_emit, now, force):
            return
        self._last_byte_progress_emit = now
        self._last_byte_progress_file_index = file_index
        self.byte_progress.emit(bytes_done, total_bytes, file_index)

    @staticmethod
    def _progress_signal_due(
        last_emit: float | None, now: float, force: bool
    ) -> bool:
        return (
            force
            or last_emit is None
            or now - last_emit >= PROGRESS_SIGNAL_INTERVAL_SECONDS
        )

    def _internal_error_report(self, exc: Exception) -> CopyReport:
        now = datetime.now()
        report = CopyReport(started_at=now, finished_at=now)
        result = FileCopyResult(
            source=self.job.primary.mount_path,
            destination=None,
            action="failed",
            reason="internal worker error",
            error=str(exc),
        )
        report.add_result(result)
        message = f"FAILED: unexpected copy worker error: {exc}"
        report.add_log(message)
        self.log.emit(message, LogMessageType.ERROR.name)
        self.result.emit(result)
        return report

    def request_collision_decision(
        self,
        source: Path,
        destination: Path,
    ) -> CollisionDecision:
        if self._collision_decision_for_all is not None:
            return self._collision_decision_for_all
        self._collision_decision = "skip"
        self._collision_event.clear()
        self.ask_collision.emit(str(source), str(destination))
        while not self._collision_event.wait(0.1):
            if self.cancel_event.is_set():
                return "cancel"
        return self._collision_decision

    def request_clone_mismatch_decision(
        self,
        source: Path,
        destination: Path,
        allow_remove: bool,
    ) -> CloneMismatchResponse:
        if self._clone_mismatch_response_for_all is not None:
            return self._clone_mismatch_response_for_all
        self._clone_mismatch_response = CloneMismatchResponse(
            decision="skip",
            remove_source=False,
        )
        self._clone_mismatch_event.clear()
        self.ask_clone_mismatch.emit(str(source), str(destination), allow_remove)
        while not self._clone_mismatch_event.wait(0.1):
            if self.cancel_event.is_set():
                return CloneMismatchResponse(decision="cancel", remove_source=False)
        return self._clone_mismatch_response

    def answer_clone_mismatch(
        self,
        decision: CloneMismatchDecision,
        remove_source: bool = False,
        apply_for_all: bool = False,
    ) -> None:
        self._clone_mismatch_response = CloneMismatchResponse(
            decision=decision,
            remove_source=remove_source,
        )
        if apply_for_all and decision != "cancel":
            self._clone_mismatch_response_for_all = self._clone_mismatch_response
        self._clone_mismatch_event.set()

    def request_volume_mismatch_decision(
        self,
        source: Path,
        destination: Path,
        size_bytes: int,
        companion_count: int,
        destination_exists: bool,
    ) -> VolumeMismatchDecision:
        saved = self._volume_mismatch_decisions_for_all.get(destination_exists)
        if saved is not None:
            return saved
        self._volume_mismatch_decision = "keep_existing" if destination_exists else "skip"
        self._volume_mismatch_event.clear()
        self.ask_volume_mismatch.emit(
            str(source),
            str(destination),
            size_bytes,
            self._safe_path_size(destination),
            companion_count,
            destination_exists,
        )
        while not self._volume_mismatch_event.wait(0.1):
            if self.cancel_event.is_set():
                return "cancel"
        return self._volume_mismatch_decision

    @staticmethod
    def _safe_path_size(path: Path) -> int:
        try:
            return path.stat().st_size
        except OSError:
            return 0

    def answer_volume_mismatch(
        self,
        decision: VolumeMismatchDecision,
        destination_exists: bool,
        apply_for_all: bool = False,
    ) -> None:
        self._volume_mismatch_decision = decision
        if apply_for_all and decision != "cancel":
            self._volume_mismatch_decisions_for_all[destination_exists] = decision
        self._volume_mismatch_event.set()

    def answer_collision(
        self,
        decision: CollisionDecision,
        apply_for_all: bool = False,
    ) -> None:
        self._collision_decision = decision
        if apply_for_all and decision != "cancel":
            self._collision_decision_for_all = decision
        self._collision_event.set()


class FormatWorker(QObject):
    """Format approved volumes sequentially in one background worker."""

    log = Signal(str)
    status = Signal(str)
    finished = Signal(bool, bool, object)

    def __init__(self, targets: list[VolumeInfo], filesystem: str, report: CopyReport) -> None:
        super().__init__()
        self.targets = targets
        self.filesystem = filesystem
        self.report = report

    @Slot()
    def run(self) -> None:
        attempted = False
        had_failure = False
        rejections: list[str] = []
        try:
            with windows_com_initialized():
                service = FormatService()
                for target in self.targets:
                    attempted = True
                    self.status.emit(f"Verifying {target.display_name} before formatting…")
                    result = service.format_volume(
                        target,
                        self.filesystem,
                        self.report,
                        before_format=lambda current: self._announce_format(service, current),
                    )
                    if result.target_rejected:
                        rejections.append(result.message)
                        self.status.emit(f"Formatting skipped for {target.display_name}")
                        self.log.emit(f"FORMAT SKIPPED: {result.message}")
                    elif result.ok:
                        self.log.emit(result.message)
                    else:
                        had_failure = True
                        self.log.emit(f"FORMAT FAILED: {result.message}")
        except Exception as exc:  # noqa: BLE001 - report worker failures in the dialog
            had_failure = True
            self.log.emit(f"Formatting worker failed unexpectedly: {exc}")
        finally:
            self.finished.emit(attempted, had_failure, rejections)

    def _announce_format(self, service: FormatService, target: VolumeInfo) -> None:
        description = service.format_target_description(target, self.filesystem)
        self.status.emit(f"Formatting {target.display_name}…")
        self.log.emit(f"Formatting {description}…")
