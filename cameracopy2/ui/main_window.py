from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QTimer, Qt, Slot
from PySide6.QtGui import QCloseEvent
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QApplication,
    QWidget,
)

from cameracopy2.config import CONFIG_FILE_NAME, load_config, save_config
from cameracopy2.core.source_paths import source_display_text
from cameracopy2.models import CameraCopyConfig, CopyJob, VolumeInfo
from cameracopy2.operation_log import CopyOperationLog
from cameracopy2.services.format_service import FormatService
from cameracopy2.services.volume_scan_requests import VolumeScanKind
from cameracopy2.services.volume_service import (
    VolumeService,
    volumes_refer_to_same_mounted_volume,
)
from cameracopy2.ui.copy_progress_dialog import CopyProgressDialog
from cameracopy2.ui.main_widgets import ElidedLabel, VolumeComboBox
from cameracopy2.ui.settings_dialog import SettingsDialog
from cameracopy2.ui.theme import apply_theme
from cameracopy2.ui.tooltips import set_form_row_tooltip, set_help_tooltip
from cameracopy2.ui.volume_scanner import VolumeScanController
from cameracopy2.ui.volume_selection import (
    AUTO_PENDING,
    USER_CLEARED,
    USER_SELECTED,
    SelectionState,
    resolve_refreshed_volume_ids,
)

HOTPLUG_POLL_INTERVAL_MS = 5000


@dataclass(frozen=True, slots=True)
class PendingCopyStart:
    primary: VolumeInfo
    secondary: VolumeInfo | None
    clone_mode: bool
    autoremove: bool
    format_after_copy: str | None
    operation_log: CopyOperationLog
    dialog: CopyProgressDialog


class MainWindow(QMainWindow):
    def __init__(
        self, config_path: Path | None = None, config: CameraCopyConfig | None = None
    ) -> None:
        super().__init__()
        self.setWindowTitle("CameraCopy")
        self.resize(650, 260)
        self.config_path = config_path or Path(CONFIG_FILE_NAME)
        self.config = config or load_config(self.config_path)
        self.volumes: list[VolumeInfo] = []
        self._volume_snapshot: tuple[tuple[str, str, str, str, str, str, int | None], ...] = tuple()
        self._updating_volume_combos = False
        self._primary_selection_state: SelectionState = AUTO_PENDING
        self._secondary_selection_state: SelectionState = AUTO_PENDING
        self._closing = False
        self._pending_copy_start: PendingCopyStart | None = None
        self._cancelled_pre_copy_scan_active = False
        self._settings_dialog: SettingsDialog | None = None

        self.primary_combo = VolumeComboBox()
        self.secondary_combo = VolumeComboBox()
        for combo in (self.primary_combo, self.secondary_combo):
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.setMinimumWidth(360)
            combo.setSizeAdjustPolicy(
                QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
            )

        self.clone_checkbox = QCheckBox("Second volume is a clone of first")
        self.clone_checkbox.setChecked(self.config.clonemode)
        self.autoremove_checkbox = QCheckBox("Remove source files after successful copy")
        self.autoremove_checkbox.setChecked(self.config.autoremove)

        self.format_combo = QComboBox()
        self._populate_format_combo()
        self._set_format_combo(self.config.autoformat)

        self.first_source_summary_label = ElidedLabel()
        self.second_source_summary_label = ElidedLabel()
        self.destination_summary_label = ElidedLabel()
        self.files_summary_label = ElidedLabel()

        self.refresh_button = QPushButton("Manual\nrescan")
        self.refresh_button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Expanding)
        self.settings_button = QPushButton("Settings")
        self.start_button = QPushButton("Start copy")
        self.start_button.setDefault(True)

        first_volume_label = QLabel("First volume:")
        second_volume_label = QLabel("Second volume:")
        source_grid = QGridLayout()
        source_grid.setColumnStretch(1, 1)
        source_grid.setHorizontalSpacing(8)
        source_grid.setVerticalSpacing(6)
        source_grid.addWidget(first_volume_label, 0, 0)
        source_grid.addWidget(self.primary_combo, 0, 1)
        source_grid.addWidget(self.refresh_button, 0, 2, 2, 1)
        source_grid.addWidget(second_volume_label, 1, 0)
        source_grid.addWidget(self.secondary_combo, 1, 1)

        set_help_tooltip(
            first_volume_label,
            "Select the primary source volume.",
            "Its files determine destination names, rating selection, and the reference "
            "files used in clone mode.",
        )
        self.primary_combo.setToolTip(first_volume_label.toolTip())
        set_help_tooltip(
            second_volume_label,
            "Select an optional second source volume.",
            "Normally it is copied as another source. In clone mode, it is checked "
            "against files preserved from the first volume.",
        )
        self.secondary_combo.setToolTip(second_volume_label.toolTip())
        set_help_tooltip(
            self.refresh_button,
            "Rescan mounted volumes now.",
            "CameraCopy also refreshes the volume list automatically while idle.",
        )
        set_help_tooltip(self.settings_button, "Open CameraCopy settings.")
        set_help_tooltip(
            self.start_button,
            "Rescan the selected volumes, validate the copy job, and start copying.",
        )

        form = QFormLayout()
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.addRow("Clone mode:", self.clone_checkbox)
        form.addRow("After successful copy:", self.autoremove_checkbox)
        form.addRow("Format after copy:", self.format_combo)
        form.addRow(self._summary_row())
        set_form_row_tooltip(
            form,
            self.clone_checkbox,
            "Treat the second volume as a duplicate of the first.",
            "CameraCopy copies from the first volume, then SHA256-verifies matching "
            "files on the second. Files found only on the second volume are reviewed "
            "separately.",
        )
        set_form_row_tooltip(
            form,
            self.autoremove_checkbox,
            "Remove each source file only after its destination copy has been written "
            "and successfully verified.",
        )
        set_form_row_tooltip(
            form,
            self.format_combo,
            "Optionally format the selected source volume or volumes after the copy "
            "finishes successfully.",
            "Formatting erases all remaining data on those volumes.",
        )

        button_row = QHBoxLayout()
        button_row.addWidget(self.settings_button)
        button_row.addStretch(1)
        button_row.addWidget(self.start_button)

        layout = QVBoxLayout()
        layout.addLayout(source_grid)
        layout.addLayout(form)
        layout.addLayout(button_row)

        central = QWidget()
        central.setLayout(layout)
        self.setCentralWidget(central)

        self.refresh_button.clicked.connect(self.refresh_volumes)
        self.settings_button.clicked.connect(self.open_settings)
        self.start_button.clicked.connect(self.start_copy)
        self.clone_checkbox.toggled.connect(self._clone_mode_changed)
        self.primary_combo.currentIndexChanged.connect(
            lambda _index: self._volume_selection_changed("primary")
        )
        self.secondary_combo.currentIndexChanged.connect(
            lambda _index: self._volume_selection_changed("secondary")
        )

        self._volume_scanner = VolumeScanController(self)
        self._volume_scanner.succeeded.connect(self._volume_scan_succeeded)
        self._volume_scanner.failed.connect(self._volume_scan_failed)
        self._volume_scanner.manual_busy_changed.connect(self._update_scan_controls)

        self._start_hotplug_polling()
        self._request_volume_scan("startup")
        self._update_summaries()

    def _summary_row(self) -> QWidget:
        widget = QWidget()
        layout = QGridLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setHorizontalSpacing(16)
        layout.setVerticalSpacing(4)

        layout.addWidget(self._summary_cell("First source", self.first_source_summary_label), 0, 0)
        layout.addWidget(self._summary_cell("Destination", self.destination_summary_label), 0, 1)
        layout.addWidget(
            self._summary_cell("Second source", self.second_source_summary_label), 1, 0
        )
        layout.addWidget(self._summary_cell("Files", self.files_summary_label), 1, 1)
        layout.setColumnStretch(0, 1)
        layout.setColumnStretch(1, 1)
        return widget

    @staticmethod
    def _summary_cell(title: str, value_label: ElidedLabel) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)

        title_label = QLabel(f"{title}:")
        title_label.setStyleSheet("font-weight: 600;")
        title_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        layout.addWidget(title_label)
        layout.addWidget(value_label, 1)
        return widget

    def _populate_format_combo(self) -> None:
        self.format_combo.clear()
        self.format_combo.addItem("Do not format", None)
        availability = FormatService().available_filesystems()
        for filesystem, reason in availability.items():
            label = filesystem if reason is None else f"{filesystem} (unavailable: {reason})"
            self.format_combo.addItem(label, filesystem)
            index = self.format_combo.count() - 1
            self.format_combo.model().item(index).setEnabled(reason is None)

    def _set_format_combo(self, value: str | None) -> None:
        for index in range(self.format_combo.count()):
            if (
                self.format_combo.itemData(index) == value
                and self.format_combo.model().item(index).isEnabled()
            ):
                self.format_combo.setCurrentIndex(index)
                return
        self.format_combo.setCurrentIndex(0)

    def refresh_volumes(self) -> None:
        self._request_volume_scan("manual")

    def _request_volume_scan(
        self, kind: VolumeScanKind, included_devices: list[str] | None = None
    ) -> None:
        if not self._closing:
            keywords = (
                self.config.includeddevices
                if included_devices is None
                else included_devices
            )
            self._volume_scanner.request(kind, keywords)

    @Slot(bool)
    def _update_scan_controls(self, _manual_busy: bool | None = None) -> None:
        self.refresh_button.setEnabled(
            not self._volume_scanner.manual_busy and self._pending_copy_start is None
        )

    @Slot(str, object)
    def _volume_scan_succeeded(self, kind: str, result: object) -> None:
        if self._closing:
            return
        volumes = (
            [volume for volume in result if isinstance(volume, VolumeInfo)]
            if isinstance(result, list)
            else []
        )

        if kind == "pre_copy":
            self._apply_volume_list(volumes)
            if self._pending_copy_start is None:
                self._finish_cancelled_pre_copy_scan()
            else:
                self._complete_pending_copy_start(volumes)
            return
        if kind == "settings_dialog":
            if self._settings_dialog is not None:
                self._settings_dialog.update_volumes(volumes)
            return

        popup_open = (
            self.primary_combo.view().isVisible()
            or self.secondary_combo.view().isVisible()
        )
        if kind != "manual" and popup_open:
            return
        if self._make_volume_snapshot(volumes) != self._volume_snapshot or not self.volumes:
            self._apply_volume_list(volumes)

    @Slot(str, str)
    def _volume_scan_failed(self, kind: str, error: str) -> None:
        if self._closing:
            return
        if kind == "manual":
            QMessageBox.warning(
                self, "Volume rescan failed", f"Could not rescan mounted volumes:\n{error}"
            )
        elif kind == "pre_copy":
            pending = self._pending_copy_start
            if pending is None:
                self._finish_cancelled_pre_copy_scan()
                return
            pending.operation_log.error("Pre-copy volume scan failed: %s", error)
            pending.operation_log.close()
            self._pending_copy_start = None
            pending.dialog.reject()
            QMessageBox.critical(
                self,
                "Volume rescan failed",
                f"Could not verify the selected volumes before copying:\n{error}",
            )

    def _start_hotplug_polling(self) -> None:
        self._hotplug_timer = QTimer(self)
        self._hotplug_timer.setInterval(HOTPLUG_POLL_INTERVAL_MS)
        self._hotplug_timer.timeout.connect(self._check_for_volume_changes)
        self._hotplug_timer.start()

    def _pause_hotplug_polling(self) -> None:
        if self._hotplug_timer.isActive():
            self._hotplug_timer.stop()

    def _resume_hotplug_polling(self) -> None:
        if not self._hotplug_timer.isActive():
            self._hotplug_timer.start()

    @Slot()
    def _check_for_volume_changes(self) -> None:
        if self.primary_combo.view().isVisible() or self.secondary_combo.view().isVisible():
            return
        self._request_volume_scan("poll")

    def _apply_volume_list(self, volumes: list[VolumeInfo]) -> None:
        previous_primary = self._current_volume(self.primary_combo)
        previous_secondary = self._current_volume(self.secondary_combo)
        previous_primary_id = previous_primary.id if previous_primary is not None else ""
        previous_secondary_id = previous_secondary.id if previous_secondary is not None else ""

        self.volumes = volumes
        self._volume_snapshot = self._make_volume_snapshot(volumes)
        primary_id, secondary_id, self._primary_selection_state, self._secondary_selection_state = (
            resolve_refreshed_volume_ids(
                self.volumes,
                previous_primary_id,
                previous_secondary_id,
                self._primary_selection_state,
                self._secondary_selection_state,
                self.config.defaultprimaryvolumeid,
                self.config.defaultsecondaryvolumeid,
                self.config.defaultprimaryvolumematch,
                self.config.defaultsecondaryvolumematch,
                previous_primary_volume=previous_primary,
                previous_secondary_volume=previous_secondary,
            )
        )
        self._rebuild_volume_combos(primary_id, secondary_id)
        self._update_summaries()

    @staticmethod
    def _make_volume_snapshot(
        volumes: list[VolumeInfo],
    ) -> tuple[tuple[str, str, str, str, str, str, int | None], ...]:
        return tuple(
            sorted(
                (
                    volume.id,
                    str(volume.mount_path),
                    volume.device_path or "",
                    volume.uuid or "",
                    volume.device_serial or "",
                    volume.partition_uuid or "",
                    volume.size_bytes,
                )
                for volume in volumes
            )
        )

    def _rebuild_volume_combos(self, primary_id: str | None, secondary_id: str | None) -> None:
        primary_id = primary_id or ""
        secondary_id = secondary_id or ""
        if primary_id and secondary_id and primary_id == secondary_id:
            secondary_id = ""

        self._updating_volume_combos = True
        try:
            self.primary_combo.clear()
            self.secondary_combo.clear()

            self.primary_combo.addItem("None", None)
            self.primary_combo.setItemData(
                0, "No primary source selected", Qt.ItemDataRole.ToolTipRole
            )
            for volume in self.volumes:
                if volume.id != secondary_id:
                    self._add_volume_item(self.primary_combo, volume)

            self.secondary_combo.addItem("None", None)
            self.secondary_combo.setItemData(
                0, "No secondary source selected", Qt.ItemDataRole.ToolTipRole
            )
            for volume in self.volumes:
                if volume.id != primary_id:
                    self._add_volume_item(self.secondary_combo, volume)

            self._select_volume_id(self.primary_combo, primary_id)
            self._select_volume_id(self.secondary_combo, secondary_id)
        finally:
            self._updating_volume_combos = False

    @staticmethod
    def _add_volume_item(combo: QComboBox, volume: VolumeInfo) -> None:
        combo.addItem(volume.display_name, volume)
        combo.setItemData(combo.count() - 1, volume.display_name, Qt.ItemDataRole.ToolTipRole)

    @staticmethod
    def _select_volume_id(combo: QComboBox, volume_id: str) -> None:
        if not volume_id:
            combo.setCurrentIndex(0)
            return
        for index in range(combo.count()):
            volume = combo.itemData(index)
            if isinstance(volume, VolumeInfo) and volume.id == volume_id:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(0)

    @staticmethod
    def _current_volume(combo: QComboBox) -> VolumeInfo | None:
        volume = combo.currentData()
        return volume if isinstance(volume, VolumeInfo) else None

    @staticmethod
    def _current_volume_id(combo: QComboBox) -> str:
        volume = MainWindow._current_volume(combo)
        return volume.id if volume is not None else ""

    def _volume_selection_changed(self, role: str) -> None:
        if self._updating_volume_combos:
            return

        if role == "primary":
            self._primary_selection_state = self._manual_selection_state(self.primary_combo)
        elif role == "secondary":
            self._secondary_selection_state = self._manual_selection_state(self.secondary_combo)

        primary_id = self._current_volume_id(self.primary_combo)
        secondary_id = self._current_volume_id(self.secondary_combo)
        self._rebuild_volume_combos(primary_id, secondary_id)
        self._update_summaries()

    @staticmethod
    def _manual_selection_state(combo: QComboBox) -> SelectionState:
        if isinstance(combo.currentData(), VolumeInfo):
            return USER_SELECTED
        has_real_volume_choices = any(
            isinstance(combo.itemData(index), VolumeInfo) for index in range(combo.count())
        )
        return USER_CLEARED if has_real_volume_choices else AUTO_PENDING

    def _update_summaries(self) -> None:
        self.first_source_summary_label.set_full_text(
            self._source_summary_text(self.primary_combo.currentData(), "")
        )
        self.second_source_summary_label.set_full_text(
            self._source_summary_text(self.secondary_combo.currentData(), "")
        )
        self.destination_summary_label.set_full_text(self.config.destination or "Not configured")
        self.files_summary_label.set_full_text(self._files_summary_text())

    def _source_summary_text(self, volume: object, none_text: str) -> str:
        if not isinstance(volume, VolumeInfo):
            return none_text
        return source_display_text(volume, self.config.source)

    def _files_summary_text(self) -> str:
        included = " ".join(self.config.includedfiles) if self.config.includedfiles else "*"
        excluded = " ".join(f"!{pattern}" for pattern in self.config.excludedfiles)
        return " ".join(part for part in (included, excluded) if part).strip() or "*"

    def open_settings(self) -> None:
        self._pause_hotplug_polling()
        dialog = SettingsDialog(self.config, self.volumes, self)
        self._settings_dialog = dialog
        QTimer.singleShot(0, lambda: self._request_volume_scan("settings_dialog", []))
        try:
            if dialog.exec():
                self.config = dialog.config()
                save_config(self.config, self.config_path)
                app = QApplication.instance()
                if isinstance(app, QApplication):
                    apply_theme(app, self.config.theme)
                self.autoremove_checkbox.setChecked(self.config.autoremove)
                self._populate_format_combo()
                self._set_format_combo(self.config.autoformat)
                self._primary_selection_state = AUTO_PENDING
                self._secondary_selection_state = AUTO_PENDING
                self._request_volume_scan("settings")
                self._update_summaries()
        finally:
            self._settings_dialog = None
            self._resume_hotplug_polling()

    def _clone_mode_changed(self, checked: bool) -> None:
        if self.config.clonemode == checked:
            return
        self.config.clonemode = checked
        save_config(self.config, self.config_path)

    def start_copy(self) -> None:
        operation_log = CopyOperationLog(self.config_path)
        operation_log.info("User requested a copy operation")
        primary = self._current_volume(self.primary_combo)
        secondary = self._current_volume(self.secondary_combo)
        if primary is None:
            operation_log.warning("Copy validation failed: no primary volume selected")
            operation_log.close()
            QMessageBox.critical(self, "No primary volume", "Choose a primary volume first.")
            return
        if not self._ensure_destination_configured():
            operation_log.warning("Copy validation stopped: destination is unavailable")
            operation_log.close()
            return
        if secondary is not None and volumes_refer_to_same_mounted_volume(primary, secondary):
            operation_log.warning(
                "Copy validation failed: primary and secondary point to the same mounted volume"
            )
            operation_log.close()
            QMessageBox.critical(
                self, "Duplicate volume", "Primary and secondary volume must be different."
            )
            return

        format_value = self.format_combo.currentData()
        provisional_job = CopyJob(
            primary=primary,
            secondary=secondary,
            config=self.config,
            clone_mode=self.clone_checkbox.isChecked(),
            autoremove=self.autoremove_checkbox.isChecked(),
            format_after_copy=format_value if isinstance(format_value, str) else None,
        )
        dialog = CopyProgressDialog(provisional_job, self, operation_log=operation_log)
        dialog.volumes_changed.connect(self._handle_volumes_changed_after_format)
        dialog.finished.connect(self._copy_dialog_finished)
        dialog.validation_cancelled.connect(self._cancel_pending_copy_start)
        self._pending_copy_start = PendingCopyStart(
            primary=primary,
            secondary=secondary,
            clone_mode=provisional_job.clone_mode,
            autoremove=provisional_job.autoremove,
            format_after_copy=provisional_job.format_after_copy,
            operation_log=operation_log,
            dialog=dialog,
        )
        operation_log.info(
            "Pre-copy scan requested; primary=%s secondary=%s destination=%s clone=%s "
            "autoremove=%s format=%s sha256=%s durable_writes=%s",
            primary.display_name,
            secondary.display_name if secondary is not None else "none",
            self.config.destination,
            self.clone_checkbox.isChecked(),
            self.autoremove_checkbox.isChecked(),
            format_value if isinstance(format_value, str) else "none",
            self.config.checkhash,
            self.config.durablewrites,
        )
        self._set_copy_setup_enabled(False)
        self._pause_hotplug_polling()
        dialog.show_validation_state()
        dialog.show()
        self._request_volume_scan("pre_copy")

    @Slot()
    def _cancel_pending_copy_start(self) -> None:
        pending = self._pending_copy_start
        if pending is None:
            return
        pending.operation_log.warning(
            "Copy cancelled while source-volume validation was pending"
        )
        pending.operation_log.close()
        self._pending_copy_start = None
        self._cancelled_pre_copy_scan_active = (
            self._volume_scanner.active_kind == "pre_copy"
        )
        self._volume_scanner.clear_pending()

    def _finish_cancelled_pre_copy_scan(self) -> None:
        if not self._cancelled_pre_copy_scan_active:
            return
        self._cancelled_pre_copy_scan_active = False
        self._set_copy_setup_enabled(True)
        self._resume_hotplug_polling()

    def _complete_pending_copy_start(self, volumes: list[VolumeInfo]) -> None:
        pending = self._pending_copy_start
        if pending is None:
            return

        current_primary = VolumeService.find_matching_copy_source(pending.primary, volumes)
        current_secondary = (
            VolumeService.find_matching_copy_source(pending.secondary, volumes)
            if pending.secondary is not None
            else None
        )
        missing: list[str] = []
        if current_primary is None:
            missing.append(pending.primary.display_name)
        if pending.secondary is not None and current_secondary is None:
            missing.append(pending.secondary.display_name)
        if missing:
            pending.operation_log.warning(
                "Pre-copy identity validation failed for: %s", ", ".join(missing)
            )
            pending.operation_log.close()
            self._pending_copy_start = None
            pending.dialog.reject()
            names = "\n".join(missing)
            QMessageBox.critical(
                self,
                "Volume unavailable",
                "The selected volume is no longer mounted or available:\n"
                f"{names}\n\nUse Manual rescan and try again.",
            )
            return

        if current_secondary is not None and volumes_refer_to_same_mounted_volume(
            current_primary, current_secondary
        ):
            pending.operation_log.warning(
                "Pre-copy identity validation found the same mounted volume in both roles"
            )
            pending.operation_log.close()
            self._pending_copy_start = None
            pending.dialog.reject()
            QMessageBox.critical(
                self,
                "Duplicate volume",
                "Primary and secondary volume resolve to the same mounted volume. "
                "Choose two different volumes.",
            )
            return

        selected_volumes = [
            current_primary,
            *([current_secondary] if current_secondary is not None else []),
        ]
        if pending.format_after_copy and not self._confirm_destination_on_formatted_volume(
            selected_volumes
        ):
            pending.operation_log.warning(
                "Copy validation failed: destination is on a volume selected for formatting"
            )
            pending.operation_log.close()
            self._pending_copy_start = None
            pending.dialog.reject()
            return

        job = CopyJob(
            primary=current_primary,
            secondary=current_secondary,
            config=self.config,
            clone_mode=pending.clone_mode,
            autoremove=pending.autoremove,
            format_after_copy=pending.format_after_copy,
        )
        self._pending_copy_start = None
        self._volume_scanner.clear_pending()
        pending.operation_log.info("Pre-copy validation completed successfully")
        pending.dialog.start(job)

    @Slot(int)
    def _copy_dialog_finished(self, _result: int) -> None:
        self.autoremove_checkbox.setChecked(self.config.autoremove)
        if self._cancelled_pre_copy_scan_active:
            return
        self._set_copy_setup_enabled(True)
        self._resume_hotplug_polling()

    def _set_copy_setup_enabled(self, enabled: bool) -> None:
        for widget in (
            self.primary_combo,
            self.secondary_combo,
            self.clone_checkbox,
            self.autoremove_checkbox,
            self.format_combo,
            self.settings_button,
            self.start_button,
        ):
            widget.setEnabled(enabled)
        self._update_scan_controls()

    @Slot()
    def _handle_volumes_changed_after_format(self) -> None:
        """Refresh the volume list after formatting can change UUIDs/mount points.

        The immediate refresh handles the common path where FormatService has already
        mounted the volume again. The delayed refresh catches slower udev/UDisks
        updates without requiring the user to press Manual rescan.
        """
        self._request_volume_scan("post_format")
        QTimer.singleShot(1000, lambda: self._request_volume_scan("post_format"))

    def _confirm_destination_on_formatted_volume(self, volumes: list[VolumeInfo]) -> bool:
        destination = Path(self.config.destination).expanduser()
        risky = [
            volume for volume in volumes if self._path_is_inside(destination, volume.mount_path)
        ]
        if not risky:
            return True
        names = "\n".join(volume.display_name for volume in risky)
        QMessageBox.critical(
            self,
            "Destination volume will be formatted",
            "The destination folder is located on a selected source volume that will "
            "be formatted after copying.\n\n"
            f"Destination:\n{destination}\n\n"
            f"Formatted volume(s):\n{names}\n\n"
            "Choose a destination on another volume or disable formatting.",
            QMessageBox.StandardButton.Ok,
        )
        return False

    @staticmethod
    def _path_is_inside(path: Path, parent: Path) -> bool:
        try:
            resolved_path = path.resolve()
            resolved_parent = parent.resolve()
        except OSError:
            resolved_path = path.absolute()
            resolved_parent = parent.absolute()
        try:
            resolved_path.relative_to(resolved_parent)
            return True
        except ValueError:
            return False

    def _ensure_destination_configured(self) -> bool:
        if self.config.destination and self._destination_path_looks_usable(self.config.destination):
            try:
                Path(self.config.destination).expanduser().mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self,
                    "Destination unavailable",
                    f"Could not create or access destination folder:\n{exc}",
                )
                return False
            return True

        message = "Choose a destination folder before copying."
        if self.config.destination:
            message = (
                "The configured destination does not look usable on this system:\n"
                f"{self.config.destination}\n\nChoose a new folder."
            )
        QMessageBox.information(self, "Choose destination", message)
        selected = QFileDialog.getExistingDirectory(
            self, "Choose destination folder", str(Path.home())
        )
        if not selected:
            return False
        self.config.destination = selected
        save_config(self.config, self.config_path)
        self._update_summaries()
        return True

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        self._closing = True
        self._hotplug_timer.stop()
        if self._pending_copy_start is not None:
            self._pending_copy_start.operation_log.warning(
                "Application closed while pre-copy validation was pending"
            )
            self._pending_copy_start.operation_log.close()
            self._pending_copy_start = None
        if not self._volume_scanner.close(timeout_ms=100):
            event.ignore()
            QTimer.singleShot(100, self.close)
            return
        super().closeEvent(event)

    @staticmethod
    def _destination_path_looks_usable(path_text: str) -> bool:
        if sys.platform != "win32" and len(path_text) >= 2 and path_text[1] == ":":
            return False
        try:
            path = Path(path_text).expanduser()
        except OSError:
            return False
        return bool(str(path))
