from __future__ import annotations

from html import escape as html_escape
from pathlib import Path
from subprocess import Popen
from threading import Event

from PySide6.QtCore import QThread, Qt, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from cameracopy2.core.format_warnings import (
    format_risk_warning_paragraphs,
    irreversible_format_warning,
)
from cameracopy2.core.log_messages import LogMessageType, classify_log_message
from cameracopy2.core.transfer_rate import TransferRateEstimator
from cameracopy2.models import CopyJob, CopyReport, FileCopyResult
from cameracopy2.operation_log import CopyOperationLog
from cameracopy2.services.application_launcher import (
    ApplicationConfigurationError,
    application_button_text,
    application_button_tooltip,
    application_integration_configured,
    build_application_launch,
    launch_application,
)
from cameracopy2.services.format_service import FormatService
from cameracopy2.ui.copy_workers import CopyWorker, FormatWorker
from cameracopy2.ui.log_style import resolved_log_colors
from cameracopy2.ui.tooltips import set_help_tooltip


class CopyProgressDialog(QDialog):
    volumes_changed = Signal()
    validation_cancelled = Signal()

    def __init__(
        self,
        job: CopyJob,
        parent: QWidget | None = None,
        *,
        operation_log: CopyOperationLog | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Copy files")
        self.resize(1000, 640)
        self.job = job
        self.operation_log = operation_log
        self.cancel_event = Event()
        self.thread: QThread | None = None
        self.worker: CopyWorker | None = None
        self.format_thread: QThread | None = None
        self.format_worker: FormatWorker | None = None
        self.report: CopyReport | None = None
        self.validation_pending = False
        self.copy_running = False
        self.format_running = False
        self.current_file_index = 0
        self.current_file_total = 0
        self.source_index = 0
        self.source_total = 0
        self.source_action = ""
        self._transfer_rate = TransferRateEstimator()
        self._launched_processes: list[Popen[bytes]] = []

        self.source_status_label = QLabel("Source: —")
        self.status_label = QLabel("Ready")
        self.current_file_label = QLabel("Current file: —")
        self.current_file_label.setWordWrap(True)
        self.file_progress_bar = QProgressBar()
        self.file_progress_bar.setRange(0, 100)
        self.byte_progress_bar = QProgressBar()
        self.byte_progress_bar.setRange(0, 100)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_colors = resolved_log_colors(self.job.config, self.palette())
        self.log_view.setStyleSheet(
            f"QTextEdit {{ background-color: {self.log_colors.background.name()}; }}"
        )
        self.cancel_button = QPushButton("Cancel")
        self.open_destination_button = QPushButton("Open destination")
        self.open_destination_button.setEnabled(False)
        button_text = application_button_text(self.job.config).replace("&", "&&")
        self.open_application_button = QPushButton(button_text)
        self.open_application_button.setVisible(
            application_integration_configured(self.job.config)
        )
        self.open_application_button.setEnabled(False)
        set_help_tooltip(
            self.open_application_button,
            application_button_tooltip(self.job.config),
        )
        self.close_button = QPushButton("Close")
        self.close_button.setEnabled(False)

        row = QHBoxLayout()
        row.addWidget(self.cancel_button)
        row.addStretch(1)
        row.addWidget(self.open_destination_button)
        row.addWidget(self.open_application_button)
        row.addWidget(self.close_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.source_status_label)
        layout.addWidget(self.status_label)
        layout.addWidget(self.current_file_label)
        layout.addWidget(QLabel("Files"))
        layout.addWidget(self.file_progress_bar)
        layout.addWidget(QLabel("Current file bytes"))
        layout.addWidget(self.byte_progress_bar)
        layout.addWidget(self.log_view, 1)
        layout.addLayout(row)

        self.cancel_button.clicked.connect(self.request_cancel)
        self.open_destination_button.clicked.connect(self.open_destination)
        self.open_application_button.clicked.connect(self.open_configured_application)
        self.close_button.clicked.connect(self.accept)

    def show_validation_state(self) -> None:
        self.validation_pending = True
        self.source_status_label.setText("Validating source volumes…")
        self.status_label.hide()
        self.current_file_label.setText("Current file: —")
        self.file_progress_bar.setRange(0, 0)
        self.byte_progress_bar.setValue(0)
        self.append_log("Validating source volumes…")

    def start(self, job: CopyJob | None = None) -> None:
        if job is not None:
            self.job = job
        self.validation_pending = False
        self.source_status_label.setText("Source: —")
        self.status_label.show()
        self.status_label.setText("Starting copy…")
        self.file_progress_bar.setRange(0, 100)
        self.file_progress_bar.setValue(0)
        if self.operation_log is not None:
            self.operation_log.info("Copy worker starting")
        self.copy_running = True
        self.thread = QThread(self)
        self.worker = CopyWorker(self.job, self.cancel_event)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.log.connect(self.append_log)
        self.worker.progress.connect(self.update_file_progress)
        self.worker.source_started.connect(self.handle_source_started)
        self.worker.source_progress.connect(self.update_source_progress)
        self.worker.file_started.connect(self.handle_file_started)
        self.worker.byte_progress.connect(self.update_byte_progress)
        self.worker.ask_collision.connect(self.handle_collision_question)
        self.worker.ask_clone_mismatch.connect(self.handle_clone_mismatch_question)
        self.worker.ask_volume_mismatch.connect(self.handle_volume_mismatch_question)
        self.worker.result.connect(self.handle_result)
        self.worker.finished.connect(self.handle_finished)
        self.worker.finished.connect(self.thread.quit)
        self.thread.finished.connect(self._thread_finished)
        self.thread.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self.thread.deleteLater)
        self.thread.start()

    @Slot(str)
    @Slot(str, str)
    def append_log(self, message: str, message_type_name: str = "") -> None:
        if self.operation_log is not None and message:
            log_message = message.replace("\n", " | ")
            if message_type_name == LogMessageType.ERROR.name:
                self.operation_log.error("Copy report: %s", log_message)
            elif message_type_name == LogMessageType.WARNING.name:
                self.operation_log.warning("Copy report: %s", log_message)
            else:
                self.operation_log.info("Copy report: %s", log_message)
        if not message:
            self.log_view.append("")
            return
        try:
            message_type = LogMessageType[message_type_name] if message_type_name else None
        except KeyError:
            message_type = None
        if message_type is None:
            message_type = classify_log_message(message)
        color = self.log_colors.for_message(message_type).name()
        escaped_lines = []
        for line in message.splitlines():
            indent = len(line) - len(line.lstrip(" "))
            escaped_lines.append(("&nbsp;" * indent) + html_escape(line[indent:]))
        escaped = "<br>".join(escaped_lines)
        self.log_view.append(f'<span style="color:{color};">{escaped}</span>')

    @Slot(int, int, str, object)
    def handle_source_started(self, index: int, total: int, action: str, total_bytes: int) -> None:
        total_bytes = self._safe_int(total_bytes)
        self.source_index = index
        self.source_total = total
        self.source_action = action
        self._transfer_rate.reset()
        if action == "Scanning":
            self.source_status_label.setText(f"{self._source_label()}: Scanning...")
        else:
            self._set_source_status(0, total_bytes, False)

    @Slot(object, object, bool)
    def update_source_progress(self, bytes_done: int, total_bytes: int, metered: bool) -> None:
        bytes_done = self._safe_int(bytes_done)
        total_bytes = self._safe_int(total_bytes)
        self._set_source_status(bytes_done, total_bytes, metered)

    @Slot(int, int)
    def update_file_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self.file_progress_bar.setValue(100)
            self.status_label.setText("No files")
            return
        percent = int((current / total) * 100)
        self.file_progress_bar.setValue(percent)

    @Slot(str, int, int, object)
    def handle_file_started(self, path_text: str, index: int, total: int, size_bytes: int) -> None:
        size_bytes = self._safe_int(size_bytes)
        self.current_file_index = index
        self.current_file_total = total
        name = Path(path_text).name
        self.current_file_label.setText(f"Current file: {name} ({self._format_bytes(size_bytes)})")
        self.byte_progress_bar.setValue(0)
        self.status_label.setText(
            f"{self._file_action_label()} {index} / {total} — "
            f"0 bytes / {self._format_bytes(size_bytes)}"
        )

    @Slot(object, object, int)
    def update_byte_progress(self, bytes_done: int, total_bytes: int, file_index: int) -> None:
        bytes_done = self._safe_int(bytes_done)
        total_bytes = self._safe_int(total_bytes)
        if total_bytes <= 0:
            self.byte_progress_bar.setValue(100)
            return
        percent = min(100, int((bytes_done / total_bytes) * 100))
        self.byte_progress_bar.setValue(percent)
        if file_index == self.current_file_index:
            self.status_label.setText(
                f"{self._file_action_label()} {file_index} / {self.current_file_total} — "
                f"{self._format_bytes(bytes_done)} / {self._format_bytes(total_bytes)}"
            )

    def _file_action_label(self) -> str:
        if self.source_action == "Verifying clone":
            return "Verifying clone file"
        return "Copying file"

    def _set_source_status(self, bytes_done: int, total_bytes: int, metered: bool) -> None:
        label = self._source_label()
        action = self.source_action or "Processing"
        if total_bytes <= 0:
            self.source_status_label.setText(f"{label}: {action}...")
            return

        done = max(0, min(bytes_done, total_bytes))
        speed = self._transfer_rate.update(
            done,
            metered=metered,
            force_display=done >= total_bytes,
        )
        if speed > 0:
            speed_text = self._format_speed(speed)
            remaining = max(0, total_bytes - done)
            eta_text = f"ETA {self._format_eta(remaining / speed)}"
        else:
            speed_text = "calculating speed"
            eta_text = "ETA —"

        self.source_status_label.setText(
            f"{label}: {action} — {self._format_bytes(done)} / {self._format_bytes(total_bytes)} — "
            f"{speed_text} — {eta_text}"
        )

    def _source_label(self) -> str:
        if self.source_total > 1:
            return f"Source {self.source_index}/{self.source_total}"
        return "Source"

    @staticmethod
    def _safe_int(value: object) -> int:
        try:
            return int(value)
        except (TypeError, ValueError, OverflowError):
            return 0

    @classmethod
    def _path_size_text(cls, path: Path) -> str:
        try:
            return cls._format_bytes(path.stat().st_size)
        except OSError:
            return "unavailable"

    @staticmethod
    def _format_bytes(size_bytes: int) -> str:
        size = float(max(0, size_bytes))
        units = ["bytes", "KB", "MB", "GB", "TB"]
        unit_index = 0
        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1
        if unit_index == 0:
            count = int(size)
            return f"{count} byte" if count == 1 else f"{count} bytes"
        if size >= 100:
            return f"{size:.0f} {units[unit_index]}"
        if size >= 10:
            return f"{size:.1f} {units[unit_index]}"
        return f"{size:.2f} {units[unit_index]}"

    @classmethod
    def _format_speed(cls, bytes_per_second: float) -> str:
        return f"{cls._format_bytes(int(bytes_per_second))}/s"

    @staticmethod
    def _format_eta(seconds: float) -> str:
        if seconds < 1:
            return "<1s"
        if seconds < 60:
            return f"{int(round(seconds))}s"
        minutes, remaining_seconds = divmod(int(round(seconds)), 60)
        if minutes < 60:
            return f"{minutes}m {remaining_seconds:02d}s"
        hours, remaining_minutes = divmod(minutes, 60)
        return f"{hours}h {remaining_minutes:02d}m"

    @Slot(str, str)
    def handle_collision_question(self, source_text: str, destination_text: str) -> None:
        if self.worker is None:
            return
        source_name = Path(source_text).name
        destination = Path(destination_text)
        box = QMessageBox(self)
        box.setWindowTitle("Existing file")
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Destination file already exists:\n{destination}")
        informative_text = f"Choose what to do with {source_name}."
        if self.job.format_after_copy:
            informative_text += (
                "\n\nIf you skip this file and later format the source volume, "
                "the source copy will be erased."
            )
        box.setInformativeText(informative_text)
        apply_for_all_checkbox = QCheckBox("Apply this choice for all remaining file conflicts")
        box.setCheckBox(apply_for_all_checkbox)
        skip_button = box.addButton("Skip", QMessageBox.ButtonRole.AcceptRole)
        overwrite_button = box.addButton("Replace existing", QMessageBox.ButtonRole.DestructiveRole)
        rename_button = box.addButton("Keep both", QMessageBox.ButtonRole.ActionRole)
        cancel_button = box.addButton("Cancel copy", QMessageBox.ButtonRole.RejectRole)
        overwrite_button.setToolTip("Replace the existing destination file with the source file.")
        rename_button.setToolTip("Copy the source file under a new filename.")
        skip_button.setToolTip(
            "Leave the existing destination unchanged and skip this source file."
        )
        cancel_button.setToolTip("Stop the copy operation.")
        box.setDefaultButton(skip_button)
        box.exec()
        clicked = box.clickedButton()
        if clicked == overwrite_button:
            decision = "overwrite"
        elif clicked == rename_button:
            decision = "rename"
        elif clicked == cancel_button:
            decision = "cancel"
        else:
            decision = "skip"
        self.worker.answer_collision(decision, apply_for_all_checkbox.isChecked())

    @Slot(str, str, bool)
    def handle_clone_mismatch_question(
        self, source_text: str, destination_text: str, allow_remove: bool
    ) -> None:
        if self.worker is None:
            return
        source = Path(source_text)
        destination = Path(destination_text)

        box = QMessageBox(self)
        box.setWindowTitle("Different files found during clone verification")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(
            "These files have the same relative name, but their contents are different."
        )
        box.setInformativeText(
            "The destination currently contains the version copied from the first-source "
            "volume. Which version should CameraCopy preserve?\n\n"
            f"Copied from the first-source volume:\n{destination}\n"
            f"Size: {self._path_size_text(destination)}\n\n"
            f"File on the second-source volume:\n{source}\n"
            f"Size: {self._path_size_text(source)}"
        )

        apply_for_all_checkbox = QCheckBox(
            "Apply this choice to all remaining clone mismatches", box
        )
        remove_checkbox = QCheckBox(
            "Remove second-source file after successful copy", box
        )
        remove_checkbox.setVisible(allow_remove)
        options = QWidget(box)
        options_layout = QVBoxLayout(options)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.addWidget(apply_for_all_checkbox)
        options_layout.addWidget(remove_checkbox)
        message_layout = box.layout()
        if hasattr(message_layout, "rowCount") and hasattr(message_layout, "columnCount"):
            message_layout.addWidget(
                options, message_layout.rowCount(), 0, 1, message_layout.columnCount()
            )
        else:
            message_layout.addWidget(options)

        keep_both_button = box.addButton("Keep both", QMessageBox.ButtonRole.ActionRole)
        keep_first_button = box.addButton(
            "Keep first-source", QMessageBox.ButtonRole.ActionRole
        )
        use_second_button = box.addButton(
            "Use second-source", QMessageBox.ButtonRole.ActionRole
        )
        cancel_button = box.addButton("Cancel copy", QMessageBox.ButtonRole.RejectRole)

        keep_both_button.setToolTip(
            "Keep the existing first-source copy and copy the second-source version "
            "under a new filename."
        )
        keep_first_tooltip = (
            "Leave the destination unchanged and do not copy the differing "
            "second-source version."
        )
        if self.job.format_after_copy:
            keep_first_tooltip += (
                " Formatting the second-source volume will erase its differing version."
            )
        keep_first_button.setToolTip(keep_first_tooltip)
        use_second_button.setToolTip(
            "Replace the destination file with the version from the second-source volume."
        )
        cancel_button.setToolTip(
            "Stop the copy operation. Neither volume will be formatted."
        )
        box.setDefaultButton(keep_both_button)
        box.exec()

        clicked = box.clickedButton()
        if clicked == keep_first_button:
            decision = "skip"
        elif clicked == use_second_button:
            decision = "replace"
        elif clicked == cancel_button:
            decision = "cancel"
        else:
            decision = "keep_both"

        remove_source = bool(
            allow_remove
            and remove_checkbox.isChecked()
            and decision in {"keep_both", "replace"}
        )
        self.worker.answer_clone_mismatch(
            decision, remove_source, apply_for_all_checkbox.isChecked()
        )

    @Slot(str, str, object, object, int, bool)
    def handle_volume_mismatch_question(
        self,
        source_text: str,
        destination_text: str,
        source_size_value: object,
        destination_size_value: object,
        companion_count: int,
        destination_exists: bool,
    ) -> None:
        if self.worker is None:
            return

        source = Path(source_text)
        destination = Path(destination_text)
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)

        companion_text = ""
        if companion_count:
            noun = "sidecar" if companion_count == 1 else "sidecars"
            companion_text = (
                f"\n\n{companion_count} matching {noun} were also found only on the "
                "second-source volume and will follow this file when it is copied."
            )

        if destination_exists:
            box.setWindowTitle("Different file already exists at the destination")
            box.setText(
                "This file exists only on the second-source volume, but a different "
                "file already occupies its destination path."
            )
            box.setInformativeText(
                f"File on the second-source volume:\n{source}\n"
                f"Size: {self._format_bytes(self._safe_int(source_size_value))}\n\n"
                f"Existing destination file:\n{destination}\n"
                f"Size: {self._format_bytes(self._safe_int(destination_size_value))}"
                f"{companion_text}"
            )
            keep_both_button = box.addButton(
                "Keep both", QMessageBox.ButtonRole.ActionRole
            )
            keep_existing_button = box.addButton(
                "Keep existing", QMessageBox.ButtonRole.ActionRole
            )
            replace_button = box.addButton(
                "Replace existing", QMessageBox.ButtonRole.ActionRole
            )
            cancel_button = box.addButton(
                "Cancel copy", QMessageBox.ButtonRole.RejectRole
            )
            keep_both_button.setToolTip(
                "Copy the second-source file under a new filename and leave the "
                "existing destination unchanged."
            )
            keep_existing_tooltip = (
                "Leave the destination unchanged and do not copy the second-source file."
            )
            if self.job.format_after_copy:
                keep_existing_tooltip += (
                    " Formatting the second-source volume will erase that source file."
                )
            keep_existing_button.setToolTip(keep_existing_tooltip)
            replace_button.setToolTip(
                "Replace the existing destination with the second-source file."
            )
            cancel_button.setToolTip(
                "Stop the copy operation. Neither volume will be formatted."
            )
            box.setDefaultButton(keep_both_button)
        else:
            box.setWindowTitle("File found only on the second-source volume")
            box.setText("This file was not found on the first-source volume.")
            box.setInformativeText(
                f"File on the second-source volume:\n{source}\n"
                f"Size: {self._format_bytes(self._safe_int(source_size_value))}\n\n"
                f"Destination:\n{destination}{companion_text}"
            )
            copy_button = box.addButton("Copy", QMessageBox.ButtonRole.ActionRole)
            skip_button = box.addButton("Skip", QMessageBox.ButtonRole.ActionRole)
            cancel_button = box.addButton(
                "Cancel copy", QMessageBox.ButtonRole.RejectRole
            )
            copy_button.setToolTip(
                "Copy the second-source file to the displayed destination."
            )
            skip_tooltip = "Leave the file on the second-source volume and do not copy it."
            if self.job.format_after_copy:
                skip_tooltip += (
                    " Formatting the second-source volume will erase this file."
                )
            skip_button.setToolTip(skip_tooltip)
            cancel_button.setToolTip(
                "Stop the copy operation. Neither volume will be formatted."
            )
            box.setDefaultButton(copy_button)

        apply_for_all_checkbox = QCheckBox(
            "Apply this choice to remaining volume mismatches of this type"
        )
        box.setCheckBox(apply_for_all_checkbox)
        box.exec()
        clicked = box.clickedButton()

        if destination_exists:
            if clicked == keep_existing_button:
                decision = "keep_existing"
            elif clicked == replace_button:
                decision = "replace"
            elif clicked == cancel_button:
                decision = "cancel"
            else:
                decision = "keep_both"
        else:
            if clicked == copy_button:
                decision = "copy"
            elif clicked == skip_button:
                decision = "skip"
            else:
                decision = "cancel"

        self.worker.answer_volume_mismatch(
            decision,
            destination_exists,
            apply_for_all_checkbox.isChecked(),
        )

    @Slot(object)
    def handle_result(self, result: FileCopyResult) -> None:
        if self.operation_log is not None:
            self.operation_log.debug(
                "File result: source=%s destination=%s action=%s mode=%s reason=%s "
                "error=%s bytes=%s hash_ok=%s",
                result.source,
                result.destination,
                result.action,
                result.copy_mode,
                result.reason,
                result.error,
                result.bytes_copied,
                result.hash_ok,
            )
        if result.failed:
            self.status_label.setText("A copy problem was detected")
        elif result.cancelled:
            self.status_label.setText("Cancelling…")

    @Slot(object)
    def handle_finished(self, report: CopyReport) -> None:
        if self.operation_log is not None:
            self.operation_log.info(
                "Copy worker finished; cancelled=%s failures=%s results=%s",
                report.cancelled,
                len(report.failures),
                len(report.results),
            )
        self.copy_running = False
        self.report = report
        self.cancel_button.setEnabled(False)
        self.open_destination_button.setEnabled(False)
        self.open_application_button.setEnabled(False)
        self.close_button.setEnabled(False)
        if report.cancelled:
            self.source_status_label.setText("Cancelled")
            self.status_label.setText("Cancelled")
        elif report.has_failures:
            self.source_status_label.setText("Finished")
            self.status_label.setText(f"Finished with {len(report.failures)} failure(s)")
        else:
            self.source_status_label.setText("Finished")
            self.status_label.setText("Finished successfully")
        self.file_progress_bar.setValue(100)
        self._append_summary(report)
        if not self._maybe_start_format(report):
            self._finish_all_operations()

    def _append_summary(self, report: CopyReport) -> None:
        self.append_log("")
        self.append_log("Summary")
        self.append_log("-------")
        for line in report.summary_lines():
            self.append_log(line)

    def _maybe_start_format(self, report: CopyReport) -> bool:
        filesystem = self.job.format_after_copy
        if not filesystem:
            return False
        if report.has_failures or report.cancelled:
            self.append_log("Formatting skipped because the copy did not finish cleanly.")
            return False

        service = FormatService()
        unavailable = service.available_filesystems().get(filesystem)
        if unavailable:
            self.append_log(f"Formatting skipped: {unavailable}")
            return False

        targets = []
        for target in self.job.selected_volumes():
            if not self._confirm_format(target, filesystem):
                self.append_log(f"Formatting cancelled for {target.display_name}.")
                continue
            targets.append(target)

        if not targets:
            return False

        self.format_running = True
        self.source_status_label.setText("Formatting")
        self.status_label.setText("Formatting selected volume(s)…")
        self.format_thread = QThread(self)
        self.format_worker = FormatWorker(targets, filesystem, report)
        self.format_worker.moveToThread(self.format_thread)
        self.format_thread.started.connect(self.format_worker.run)
        self.format_worker.log.connect(
            self.append_log,
            Qt.ConnectionType.QueuedConnection,
        )
        self.format_worker.status.connect(self.status_label.setText)
        self.format_worker.finished.connect(self._handle_format_finished)
        self.format_worker.finished.connect(self.format_thread.quit)
        self.format_thread.finished.connect(self._format_thread_finished)
        self.format_thread.finished.connect(self.format_worker.deleteLater)
        self.format_thread.finished.connect(self.format_thread.deleteLater)
        self.format_thread.start()
        return True

    @Slot(bool, bool, object)
    def _handle_format_finished(
        self,
        format_attempted: bool,
        format_failed: bool,
        rejected_targets: object,
    ) -> None:
        if self.operation_log is not None:
            self.operation_log.info(
                "Formatting finished; attempted=%s failed=%s rejected=%s",
                format_attempted,
                format_failed,
                list(rejected_targets) if rejected_targets else [],
            )
        rejections = [str(message) for message in rejected_targets] if rejected_targets else []
        if format_attempted:
            if format_failed:
                self.append_log("Formatting finished with failure(s).")
            elif rejections:
                self.append_log(
                    "Formatting finished, but one or more selected volumes were skipped."
                )
            else:
                self.append_log("Formatting finished successfully.")
            self.append_log("Refreshing mounted volumes after formatting.")
            self.volumes_changed.emit()
        self.source_status_label.setText("Finished")
        if self.report is not None and self.report.has_failures:
            self.status_label.setText(f"Finished with {len(self.report.failures)} failure(s)")
        elif self.report is not None and self.report.cancelled:
            self.status_label.setText("Cancelled")
        elif format_failed:
            self.status_label.setText("Finished, but formatting failed")
        elif rejections:
            self.status_label.setText("Finished, but one or more volumes were not formatted")
        else:
            self.status_label.setText("Finished successfully")
        if rejections:
            self._show_format_rejections(rejections)
        self._finish_all_operations()

    @Slot()
    def _format_thread_finished(self) -> None:
        self.format_running = False
        self.format_thread = None
        self.format_worker = None

    def _finish_all_operations(self) -> None:
        self.open_destination_button.setEnabled(bool(self.job.config.destination))
        self.open_application_button.setEnabled(
            application_integration_configured(self.job.config)
        )
        self.close_button.setEnabled(True)
        if self.operation_log is not None:
            self.operation_log.info("All copy and format operations finished")
            self.operation_log.close()

    def _confirm_format(self, target, filesystem: str) -> bool:  # noqa: ANN001 - Qt-facing helper
        size_text = (
            FormatService._format_bytes(target.size_bytes)
            if target.size_bytes is not None
            else "unknown"
        )
        model_line = f"Model: {target.model}\n" if target.model else ""
        text = (
            f"Format {target.display_name} as {filesystem}?\n\n"
            f"{model_line}"
            f"Mount: {target.mount_path}\n"
            f"Device: {target.device_path or 'unknown'}\n"
            f"Label: {target.label or 'unlabeled'}\n"
            f"Size: {size_text}\n\n"
            "This erases the selected volume."
        )
        if self.report is not None:
            risk_counts = self.report.format_risk_counts_for(target)
            risk_paragraphs = format_risk_warning_paragraphs(risk_counts)
            if risk_paragraphs:
                text += "\n\n" + "\n\n".join(risk_paragraphs)
        if not self.job.config.formatprompt:
            return True
        answer = QMessageBox.warning(
            self,
            "Confirm format",
            text + "\n\n" + irreversible_format_warning(),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def _show_format_rejections(self, rejections: list[str]) -> None:
        details = "\n".join(f"• {message}" for message in rejections)
        QMessageBox.warning(
            self,
            "One or more volumes were not formatted",
            "CameraCopy could not safely verify one or more selected volumes. "
            "No formatting command was run for those volumes.\n\n"
            f"{details}\n\n"
            "Reconnect the original volume and start a new operation if you still "
            "want to format it.",
            QMessageBox.StandardButton.Ok,
        )

    def open_destination(self) -> None:
        destination = Path(self.job.config.destination).expanduser()
        destination.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(destination)))

    def open_configured_application(self) -> None:
        destination = Path(self.job.config.destination).expanduser()
        try:
            launch = build_application_launch(
                self.job.config,
                destination,
                require_executable=True,
            )
            if launch is None:
                return
            if launch.uses_destination:
                destination.mkdir(parents=True, exist_ok=True)
            self._launched_processes = [
                process
                for process in self._launched_processes
                if process.poll() is None
            ]
            self._launched_processes.append(launch_application(launch))
        except (ApplicationConfigurationError, OSError) as exc:
            name = self.job.config.applicationname.strip() or "application"
            message = f"FAILED TO OPEN {name}: {exc}"
            self.append_log(message, LogMessageType.ERROR.name)
            QMessageBox.critical(self, f"Could not open {name}", str(exc))
            return

        if launch.uses_destination:
            message = f"OPENED {launch.name}: {destination}"
        else:
            message = f"OPENED {launch.name}"
        self.append_log(message, LogMessageType.INFORMATION.name)

    def request_cancel(self) -> None:
        if self.validation_pending:
            self.validation_pending = False
            self.cancel_button.setEnabled(False)
            self.append_log("Copy cancelled during source-volume validation.")
            self.validation_cancelled.emit()
            self.reject()
            return
        self.cancel_event.set()
        self.cancel_button.setEnabled(False)
        self.append_log("Cancel requested. Current file copy will stop at the next chunk boundary.")

    @Slot()
    def _thread_finished(self) -> None:
        self.copy_running = False
        self.thread = None
        self.worker = None

    def closeEvent(self, event) -> None:  # noqa: ANN001, N802 - Qt override
        if self.validation_pending:
            self.request_cancel()
            event.ignore()
            return
        if self.copy_running:
            self.request_cancel()
            event.ignore()
            return
        if self.format_running:
            event.ignore()
            return
        if self.operation_log is not None:
            self.operation_log.close()
        super().closeEvent(event)
