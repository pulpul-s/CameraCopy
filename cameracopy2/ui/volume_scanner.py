from __future__ import annotations

import logging

from PySide6.QtCore import QObject, QThread, Signal, Slot

from cameracopy2.platform.windows_com import windows_com_initialized
from cameracopy2.services.volume_scan_requests import (
    VolumeScanKind,
    VolumeScanRequest,
    prefer_scan_request,
)
from cameracopy2.services.volume_service import VolumeService, create_volume_service

logger = logging.getLogger(__name__)


class _VolumeScanWorker(QObject):
    succeeded = Signal(int, object)
    failed = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self._volume_service: VolumeService | None = None

    @Slot(int, object)
    def scan(self, request_id: int, included_devices: object) -> None:
        if QThread.currentThread().isInterruptionRequested():
            return
        try:
            keywords = (
                list(included_devices)
                if isinstance(included_devices, (list, tuple))
                else []
            )
            with windows_com_initialized():
                if self._volume_service is None:
                    self._volume_service = create_volume_service()
                volumes = self._volume_service.filtered_volumes(keywords)
        except Exception as exc:  # noqa: BLE001 - report failures to the GUI thread
            logger.exception("Volume scan failed")
            self.failed.emit(request_id, str(exc))
            return
        if QThread.currentThread().isInterruptionRequested():
            return
        self.succeeded.emit(request_id, volumes)


class VolumeScanController(QObject):
    """Serialize volume scans and coalesce repeated GUI requests."""

    succeeded = Signal(str, object)
    failed = Signal(str, str)
    manual_busy_changed = Signal(bool)
    _scan_requested = Signal(int, object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._closing = False
        self._next_request_id = 0
        self._active: tuple[int, VolumeScanRequest] | None = None
        self._pending: VolumeScanRequest | None = None

        self._thread = QThread(self)
        self._worker = _VolumeScanWorker()
        self._worker.moveToThread(self._thread)
        self._scan_requested.connect(self._worker.scan)
        self._worker.succeeded.connect(self._handle_success)
        self._worker.failed.connect(self._handle_failure)
        self._thread.finished.connect(self._worker.deleteLater)
        self._thread.start()

    @property
    def active_kind(self) -> VolumeScanKind | None:
        return self._active[1].kind if self._active else None

    @property
    def manual_busy(self) -> bool:
        pending_kind = self._pending.kind if self._pending else None
        return "manual" in (self.active_kind, pending_kind)

    def request(self, kind: VolumeScanKind, included_devices: list[str]) -> None:
        if self._closing:
            return
        request = VolumeScanRequest(kind, tuple(included_devices))
        if self._active is None:
            self._start(request)
        else:
            self._pending = prefer_scan_request(self._pending, request)
            self._emit_manual_busy()

    def clear_pending(self) -> None:
        self._pending = None
        self._emit_manual_busy()

    def close(self, timeout_ms: int = 1000) -> bool:
        self._closing = True
        self._pending = None
        self._thread.requestInterruption()
        self._thread.quit()
        return self._thread.wait(max(0, timeout_ms))

    def _start(self, request: VolumeScanRequest) -> None:
        self._next_request_id += 1
        request_id = self._next_request_id
        self._active = (request_id, request)
        self._emit_manual_busy()
        self._scan_requested.emit(request_id, list(request.included_devices))

    def _finish(self, request_id: int) -> VolumeScanRequest | None:
        active = self._active
        if active is None or active[0] != request_id:
            return None
        self._active = None
        return active[1]

    def _start_pending(self) -> None:
        if self._closing or self._active is not None:
            return
        request = self._pending
        self._pending = None
        if request is not None:
            self._start(request)
        else:
            self._emit_manual_busy()

    @Slot(int, object)
    def _handle_success(self, request_id: int, volumes: object) -> None:
        request = self._finish(request_id)
        if request is None or self._closing:
            return
        self.succeeded.emit(request.kind, volumes)
        self._start_pending()

    @Slot(int, str)
    def _handle_failure(self, request_id: int, error: str) -> None:
        request = self._finish(request_id)
        if request is None or self._closing:
            return
        self.failed.emit(request.kind, error)
        self._start_pending()

    def _emit_manual_busy(self) -> None:
        self.manual_busy_changed.emit(self.manual_busy)
