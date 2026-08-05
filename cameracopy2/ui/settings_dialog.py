from __future__ import annotations

from html import escape as html_escape
from pathlib import Path
import sys
from typing import cast

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QTextEdit,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from cameracopy2 import __version__
from cameracopy2.config import (
    normalize_source_subfolder,
    validate_folder_component,
    validate_relative_source,
)
from cameracopy2.core.log_messages import LogMessageType
from cameracopy2.core.metadata_diagnostics import diagnose_files, format_diagnostics_report
from cameracopy2.core.naming import preview_destination
from cameracopy2.core.source_paths import resolve_source_root
from cameracopy2.models import (
    CameraCopyConfig,
    CollisionPolicy,
    ThemeMode,
    VolumeInfo,
    VolumeMatch,
    VolumeMatchMethod,
)
from cameracopy2.services.application_launcher import (
    ApplicationConfigurationError,
    build_application_launch,
)
from cameracopy2.services.format_service import FormatService
from cameracopy2.services.metadata_service import ExifToolService
from cameracopy2.ui.log_style import automatic_log_colors
from cameracopy2.ui.settings_widgets import (
    ColorPickerControl,
    FILE_PATTERN_PRESETS,
    FilePatternPreset,
    PatternListEditor,
)
from cameracopy2.ui.theme import preview_palette
from cameracopy2.ui.tooltips import set_help_tooltip
from cameracopy2.ui.volume_selection import (
    MATCH_METHOD_LABELS,
    MATCH_METHODS,
    build_volume_match,
    find_volume_by_match,
    volume_match_label,
    volume_match_value,
    volume_match_warning,
)

DATE_PRESETS = [
    ("2026-05-09", "yyyy-MM-dd"),
    ("2026-05-09_14-30", "yyyy-MM-dd_HH-mm"),
    ("2026_05_09", "yyyy_MM_dd"),
    ("20260509", "yyyyMMdd"),
    ("No date folder", ""),
    ("Custom", None),
]


class _CompactForm(QWidget):
    """Two-column form whose final row absorbs all surplus tab height."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(0, 0)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )

        self._grid = QGridLayout(self)
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(10)
        self._grid.setVerticalSpacing(8)
        self._grid.setColumnStretch(0, 0)
        self._grid.setColumnStretch(1, 1)

        self._next_row = 0
        self._labels: dict[int, QLabel] = {}
        self._finished = False

    def addRow(
        self,
        label: str | QWidget,
        field: QWidget | QLayout | None = None,
    ) -> None:
        if self._finished:
            raise RuntimeError("Cannot add rows after finish()")

        if field is None:
            section = QLabel(label) if isinstance(label, str) else label
            section.setSizePolicy(
                QSizePolicy.Policy.Expanding,
                QSizePolicy.Policy.Fixed,
            )
            self._grid.addWidget(section, self._next_row, 0, 1, 2)
            self._next_row += 1
            return

        label_widget = QLabel(label) if isinstance(label, str) else label
        label_widget.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        label_widget.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self._grid.addWidget(label_widget, self._next_row, 0)

        if isinstance(field, QLayout):
            self._grid.addLayout(field, self._next_row, 1)
        else:
            self._grid.addWidget(field, self._next_row, 1)

        self._labels[id(field)] = label_widget
        self._next_row += 1

    def addFullRow(self, item: QWidget | QLayout) -> None:
        if self._finished:
            raise RuntimeError("Cannot add rows after finish()")
        if isinstance(item, QLayout):
            self._grid.addLayout(item, self._next_row, 0, 1, 2)
        else:
            self._grid.addWidget(item, self._next_row, 0, 1, 2)
        self._next_row += 1

    def addHelpRow(self, label: QLabel) -> None:
        if self._finished:
            raise RuntimeError("Cannot add rows after finish()")
        label.setWordWrap(True)
        label.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        policy.setHeightForWidth(True)
        label.setSizePolicy(policy)
        label.setMinimumWidth(0)
        label.setContentsMargins(0, 0, 0, 0)
        label.setProperty("settingsHelpText", True)
        self._grid.addWidget(label, self._next_row, 1)
        self._next_row += 1

    def finish(self) -> None:
        """Put all extra vertical space into one empty row at the bottom."""
        if self._finished:
            return
        spacer = QSpacerItem(
            0,
            0,
            QSizePolicy.Policy.Minimum,
            QSizePolicy.Policy.Expanding,
        )
        self._grid.addItem(spacer, self._next_row, 0, 1, 2)
        self._grid.setRowStretch(self._next_row, 1)
        self._finished = True

    def labelFor(self, field: QWidget | QLayout) -> QLabel | None:
        return self._labels.get(id(field))


def _create_form_tab() -> tuple[QWidget, _CompactForm]:
    """Create a tab with one form that fills the bordered tab area."""
    tab = QWidget()
    tab.setMinimumSize(0, 0)

    tab_layout = QVBoxLayout(tab)
    tab_layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)

    form = _CompactForm()
    tab_layout.addWidget(form, 1)
    return tab, form


def set_form_row_tooltip(
    layout: _CompactForm,
    field: QWidget | QLayout,
    summary: str,
    details: str | None = None,
    *,
    controls: tuple[QWidget, ...] = (),
) -> None:
    """Apply one tooltip to the row label, field widget, and explicit controls."""
    targets: list[QWidget] = []
    row_label = layout.labelFor(field)
    if row_label is not None:
        targets.append(row_label)
    if isinstance(field, QWidget):
        targets.append(field)
    targets.extend(controls)

    seen: set[int] = set()
    for target in targets:
        if id(target) in seen:
            continue
        seen.add(id(target))
        if details is None:
            set_help_tooltip(target, summary)
        else:
            set_help_tooltip(target, summary, details)


class SettingsDialog(QDialog):
    def __init__(
        self,
        config: CameraCopyConfig,
        volumes: list[VolumeInfo] | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("CameraCopy Settings")
        self.resize(860, 680)
        self._config = config
        self._all_volumes = list(volumes or [])
        self._volumes = self._filter_volumes(self._config.includeddevices)

        self.tabs = QTabWidget()
        self._build_source_tab()
        self._build_naming_tab()
        self._build_metadata_tab()
        self._build_copy_tab()
        self._build_device_tab()
        self._build_appearance_tab()
        self._build_advanced_tab()
        self._refresh_inline_warnings()

        self.preview_label = QLabel()
        self.preview_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.preview_label.setWordWrap(True)
        preview_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        preview_policy.setHeightForWidth(True)
        self.preview_label.setSizePolicy(preview_policy)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        self.version_label = QLabel(__version__)
        self.version_label.setEnabled(False)

        footer = QHBoxLayout()
        footer.addWidget(self.version_label)
        footer.addStretch(1)
        footer.addWidget(buttons)

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetNoConstraint)
        layout.addWidget(self.tabs, 1)
        layout.addWidget(self.preview_label)
        layout.addLayout(footer)
        self._update_preview()

    def _build_source_tab(self) -> None:
        tab, form = _create_form_tab()
        self.source_edit = QLineEdit(normalize_source_subfolder(self._config.source))
        self.source_edit.setPlaceholderText(
            "Leave empty to copy from volume root. Example: DCIM"
        )
        self.destination_edit = QLineEdit(self._config.destination)

        source_browse_button = QPushButton("Browse…")
        source_browse_button.clicked.connect(self._browse_source_subfolder)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(source_browse_button)

        destination_browse_button = QPushButton("Browse…")
        destination_browse_button.clicked.connect(self._browse_destination)
        destination_row = QHBoxLayout()
        destination_row.addWidget(self.destination_edit, 1)
        destination_row.addWidget(destination_browse_button)

        form.addRow("Source subfolder:", source_row)
        source_help = QLabel(
            "Source is relative to the selected volume. Use Browse to choose a folder "
            "from a mounted volume; CameraCopy stores only the relative subfolder."
        )
        form.addHelpRow(source_help)

        form.addRow("Destination folder:", destination_row)
        destination_help = QLabel(
            "Destination is where CameraCopy creates dated folders and writes copied files."
        )
        form.addHelpRow(destination_help)
        set_form_row_tooltip(
            form,
            source_row,
            "Folder inside each selected source volume to scan.",
            "Leave empty to scan from the volume root. Example: DCIM.",
            controls=(self.source_edit, source_browse_button),
        )
        set_form_row_tooltip(
            form,
            destination_row,
            "Root folder where CameraCopy creates dated folders and writes copied files.",
            controls=(self.destination_edit, destination_browse_button),
        )
        set_help_tooltip(
            source_browse_button,
            "Choose a folder on a mounted volume.",
            "CameraCopy stores only its path relative to the volume.",
        )
        set_help_tooltip(
            destination_browse_button,
            "Choose the destination folder for copied files.",
        )
        form.finish()
        self.tabs.addTab(tab, "Source/Destination")
        self.source_edit.textChanged.connect(self._update_preview)
        self.destination_edit.textChanged.connect(self._update_preview)

    def _build_naming_tab(self) -> None:
        tab, layout = _create_form_tab()
        self.prefix_edit = QLineEdit(self._config.folderprefix)
        self.postfix_edit = QLineEdit(self._config.folderpostfix)
        self.date_preset_combo = QComboBox()
        self.custom_date_edit = QLineEdit(self._config.datetimestring)
        for label, value in DATE_PRESETS:
            self.date_preset_combo.addItem(label, value)
        preset_index = next(
            (
                i
                for i, (_, value) in enumerate(DATE_PRESETS)
                if value == self._config.datetimestring
            ),
            len(DATE_PRESETS) - 1,
        )
        self.date_preset_combo.setCurrentIndex(preset_index)
        self.custom_date_edit.setEnabled(self.date_preset_combo.currentData() is None)

        layout.addRow("Folder prefix:", self.prefix_edit)
        layout.addRow("Date format:", self.date_preset_combo)
        layout.addRow("Custom date format:", self.custom_date_edit)
        layout.addRow("Folder postfix:", self.postfix_edit)
        set_form_row_tooltip(
            layout,
            self.prefix_edit,
            "Text added before the date in each destination folder name.",
        )
        set_form_row_tooltip(
            layout,
            self.date_preset_combo,
            "Choose how the capture date is formatted in destination folder names.",
        )
        set_form_row_tooltip(
            layout,
            self.custom_date_edit,
            "Enter a custom date pattern, such as yyyy-MM-dd.",
            "Used only when Custom is selected.",
        )
        set_form_row_tooltip(
            layout,
            self.postfix_edit,
            "Text added after the date in each destination folder name.",
        )

        help_label = QLabel(
            "Common date presets are shown as examples. Custom formats use the CameraCopy "
            "date format, for example yyyy-MM-dd."
        )
        layout.addHelpRow(help_label)
        layout.finish()
        self.tabs.addTab(tab, "Folder naming")

        self.prefix_edit.textChanged.connect(self._update_preview)
        self.postfix_edit.textChanged.connect(self._update_preview)
        self.custom_date_edit.textChanged.connect(self._update_preview)
        self.date_preset_combo.currentIndexChanged.connect(self._date_preset_changed)

    def _build_metadata_tab(self) -> None:
        tab, layout = _create_form_tab()
        self.rating_combo = QComboBox()
        for label, value in [
            ("Off", 0),
            ("1 star and up", 1),
            ("2 stars and up", 2),
            ("3 stars and up", 3),
            ("4 stars and up", 4),
            ("5 stars only", 5),
        ]:
            self.rating_combo.addItem(label, value)
        self.rating_combo.setCurrentIndex(max(0, min(5, self._config.minrating)))

        self.rating_help_label = QLabel(
            "CameraCopy reads ratings from supported Adobe XMP and .rrdata sidecars. "
            "Embedded ratings can also be read through ExifTool. Other sidecar formats "
            "are not guaranteed to be supported. Files without a readable rating are "
            "treated as 0."
        )
        self.fix_sony_checkbox = QCheckBox("Use Sony XML timestamps for Sony MP4 files")
        self.fix_sony_checkbox.setChecked(self._config.fixsonytimestamps)
        self.embedded_metadata_checkbox = QCheckBox(
            "Use embedded metadata through ExifTool during copy"
        )
        self.embedded_metadata_checkbox.setChecked(self._config.useembeddedmetadata)

        exiftool = ExifToolService()
        capability = exiftool.capability()
        self._exiftool_available = capability.available
        status = (
            f"found at {capability.path}"
            if capability.available and capability.path
            else "not found"
        )
        self.exiftool_status_label = QLabel(f"ExifTool status: {status}")
        self.rating_warning_label = QLabel(
            "ExifTool was not found. Embedded ratings cannot be read. CameraCopy will "
            "only use supported Adobe XMP and .rrdata sidecar ratings. Files without a "
            "supported rating sidecar will be treated as unrated."
        )

        self.metadata_diagnostics_button = QPushButton("Run metadata diagnostics")
        self.metadata_diagnostics_button.clicked.connect(self._run_metadata_diagnostics)

        layout.addRow("Minimum rating:", self.rating_combo)
        layout.addHelpRow(self.rating_help_label)
        layout.addHelpRow(self.rating_warning_label)
        layout.addRow("Sony MP4 timestamps:", self.fix_sony_checkbox)
        layout.addRow("Embedded metadata:", self.embedded_metadata_checkbox)
        layout.addRow("External metadata tool:", self.exiftool_status_label)
        layout.addRow("Diagnostics:", self.metadata_diagnostics_button)
        set_form_row_tooltip(
            layout,
            self.rating_combo,
            "Copy only files whose rating meets or exceeds this value.",
            "CameraCopy reads supported Adobe XMP and .rrdata sidecars, and embedded "
            "metadata when ExifTool reading is enabled. Other sidecar formats are not "
            "guaranteed to be supported. Files without a rating are treated as 0.",
            controls=(self.rating_help_label,),
        )
        set_form_row_tooltip(
            layout,
            self.fix_sony_checkbox,
            "Use capture timestamps from matching Sony XML files when determining "
            "destination folders for Sony MP4 videos.",
        )
        set_form_row_tooltip(
            layout,
            self.embedded_metadata_checkbox,
            "Allow ExifTool to read embedded ratings and capture timestamps when "
            "suitable sidecar metadata is unavailable.",
            "This can slow large copy jobs.",
        )
        set_form_row_tooltip(
            layout,
            self.exiftool_status_label,
            "Shows whether ExifTool is available for embedded metadata reading and diagnostics.",
        )
        set_form_row_tooltip(
            layout,
            self.metadata_diagnostics_button,
            "Select one or more files and inspect their metadata using ExifTool.",
            "CameraCopy displays all metadata ExifTool can read, together with the "
            "timestamp candidates and folder date CameraCopy would choose. Files and "
            "settings are not changed.",
        )
        layout.finish()
        self.tabs.addTab(tab, "Metadata")
        self.rating_combo.currentIndexChanged.connect(self._refresh_inline_warnings)
        self.embedded_metadata_checkbox.toggled.connect(self._refresh_inline_warnings)

    def _run_metadata_diagnostics(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Choose files for metadata diagnostics",
            self._metadata_diagnostics_start_dir(),
        )
        if not paths:
            return
        config = self._config_from_controls()
        diagnostics = diagnose_files([Path(path) for path in paths], config)
        self._show_metadata_diagnostics(format_diagnostics_report(diagnostics))

    def _metadata_diagnostics_start_dir(self) -> str:
        preview_volume = self._preview_volume()
        if preview_volume is not None:
            source_root = resolve_source_root(
                preview_volume, normalize_source_subfolder(self.source_edit.text())
            )
            if source_root.exists():
                return str(source_root)
        destination = Path(self.destination_edit.text()).expanduser()
        if destination.exists():
            return str(destination)
        return str(Path.home())

    def _show_metadata_diagnostics(self, report_text: str) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Metadata diagnostics")
        dialog.resize(900, 620)

        text_view = QPlainTextEdit()
        text_view.setReadOnly(True)
        text_view.setPlainText(report_text)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)

        layout = QVBoxLayout(dialog)
        layout.addWidget(text_view, 1)
        layout.addWidget(buttons)
        dialog.exec()

    def _build_copy_tab(self) -> None:
        tab, layout = _create_form_tab()

        preset_row = QHBoxLayout()
        preset_label = QLabel("Pattern preset:")
        self.pattern_preset_combo = QComboBox()
        for preset in FILE_PATTERN_PRESETS:
            self.pattern_preset_combo.addItem(preset.name, preset)
        apply_preset_button = QPushButton("Apply preset")
        apply_preset_button.clicked.connect(self._apply_pattern_preset)
        preset_row.addWidget(preset_label)
        preset_row.addWidget(self.pattern_preset_combo, 1)
        preset_row.addWidget(apply_preset_button)
        set_help_tooltip(
            preset_label,
            "Choose a predefined set of include and exclude patterns.",
            "The lists are not changed until Apply preset is pressed.",
        )
        self.pattern_preset_combo.setToolTip(preset_label.toolTip())
        set_help_tooltip(
            apply_preset_button,
            "Replace the current include and exclude patterns with the selected preset.",
        )
        layout.addFullRow(preset_row)

        self.include_editor = PatternListEditor(
            self._config.includedfiles,
            "Example: *.JPG",
        )
        self.exclude_editor = PatternListEditor(
            self._config.excludedfiles,
            "Example: MEDIAPRO.XML",
        )
        layout.addRow("Include:", self.include_editor)
        layout.addRow("Exclude:", self.exclude_editor)
        set_form_row_tooltip(
            layout,
            self.include_editor,
            "Select files that match at least one of these patterns.",
            "Use patterns such as *.ARW or *.JPG.",
            controls=(self.include_editor.list_widget, self.include_editor.input),
        )
        set_form_row_tooltip(
            layout,
            self.exclude_editor,
            "Ignore files that match any of these patterns, even when they also match "
            "an include pattern.",
            controls=(self.exclude_editor.list_widget, self.exclude_editor.input),
        )

        separator = QLabel("Copy behavior")
        separator.setStyleSheet("font-weight: 600;")
        layout.addFullRow(separator)

        self.hash_checkbox = QCheckBox("Verify copied files with SHA256")
        self.hash_checkbox.setChecked(self._config.checkhash)
        self.hash_warning_label = QLabel(
            "Verification is disabled. CameraCopy will not confirm that copied file "
            "contents match the source. Source removal and formatting will have reduced "
            "protection."
        )
        self.copy_sidecars_checkbox = QCheckBox("Copy matching sidecars (.xmp, .rrdata)")
        self.copy_sidecars_checkbox.setChecked(self._config.copysidecars)
        self.collision_combo = QComboBox()
        self.collision_combo.addItem("Skip existing files", "skip")
        self.collision_combo.addItem("Always ask", "ask")
        self.collision_combo.addItem("Replace existing files", "overwrite")
        self.collision_combo.addItem("Keep both", "rename")
        self._set_combo_by_data(self.collision_combo, self._config.collisionpolicy)
        self.autoremove_checkbox = QCheckBox("Remove source files after successful copy")
        self.autoremove_checkbox.setChecked(self._config.autoremove)

        layout.addRow("Verification:", self.hash_checkbox)
        layout.addHelpRow(self.hash_warning_label)
        layout.addRow("Sidecars:", self.copy_sidecars_checkbox)
        layout.addRow("Existing files:", self.collision_combo)
        layout.addRow("Source cleanup:", self.autoremove_checkbox)
        set_form_row_tooltip(
            layout,
            self.hash_checkbox,
            "Compare SHA256 hashes of source and destination after copying.",
            "When disabled, normal copies use a basic size check. Clone verification "
            "always uses SHA256.",
        )
        set_form_row_tooltip(
            layout,
            self.copy_sidecars_checkbox,
            "Copy matching XMP and RapidRaw sidecars for selected media files.",
            "Ratings can still be read from sidecars when this option is disabled.",
        )
        set_form_row_tooltip(
            layout,
            self.autoremove_checkbox,
            "Set the default source-removal choice shown on the main screen.",
        )
        self._collision_form = layout
        self.collision_combo.currentIndexChanged.connect(self._update_collision_tooltip)
        self.hash_checkbox.toggled.connect(self._refresh_inline_warnings)
        self._update_collision_tooltip()

        layout.finish()
        self.tabs.addTab(tab, "Files")

    def _update_collision_tooltip(self) -> None:
        descriptions = {
            "skip": (
                "Leave existing destination files unchanged and do not copy the "
                "corresponding source files."
            ),
            "ask": "Ask what to do whenever a destination file already exists.",
            "overwrite": (
                "Replace existing destination files with the source versions without "
                "asking."
            ),
            "rename": (
                "Leave existing destination files unchanged and copy source files under "
                "numbered filenames."
            ),
        }
        description = descriptions.get(str(self.collision_combo.currentData()), "")
        set_form_row_tooltip(self._collision_form, self.collision_combo, description)

    def _build_device_tab(self) -> None:
        tab, layout = _create_form_tab()
        self.device_filter_editor = PatternListEditor(self._config.includeddevices, "Example: Sony")
        self.primary_default_combo = QComboBox()
        self.secondary_default_combo = QComboBox()
        self.primary_match_combo = QComboBox()
        self.secondary_match_combo = QComboBox()
        for combo in (
            self.primary_default_combo,
            self.secondary_default_combo,
            self.primary_match_combo,
            self.secondary_match_combo,
        ):
            combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            combo.setMinimumContentsLength(42)
        self._populate_device_default_combos()

        layout.addRow("Volume filter keywords:", self.device_filter_editor)
        layout.addRow("Default primary volume:", self.primary_default_combo)
        layout.addRow("Match primary volume by:", self.primary_match_combo)
        layout.addRow("Default secondary volume:", self.secondary_default_combo)
        layout.addRow("Match secondary volume by:", self.secondary_match_combo)
        set_form_row_tooltip(
            layout,
            self.device_filter_editor,
            "Show a volume when any keyword appears in its name, label, model, mount "
            "path, filesystem, or device identifiers.",
            "Leave empty to show all mounted volumes.",
            controls=(self.device_filter_editor.list_widget, self.device_filter_editor.input),
        )
        set_form_row_tooltip(
            layout,
            self.primary_default_combo,
            "Automatically select this volume as the first source when CameraCopy finds a match.",
        )
        set_form_row_tooltip(
            layout,
            self.primary_match_combo,
            "Choose which volume property CameraCopy uses to recognize the default "
            "primary volume after remounting or formatting.",
        )
        set_form_row_tooltip(
            layout,
            self.secondary_default_combo,
            "Automatically select this volume as the second source when CameraCopy finds a match.",
        )
        set_form_row_tooltip(
            layout,
            self.secondary_match_combo,
            "Choose which volume property CameraCopy uses to recognize the default "
            "secondary volume after remounting or formatting.",
        )

        help_label = QLabel(
            "Leave filters empty to show all mounted volumes. Default volumes are stored "
            "with an explicit matching rule such as Device serial, Filesystem UUID, Size, "
            "or Mount point. Device serial is usually the best choice for frequently "
            "formatted volumes when the camera exposes unique serials."
        )
        layout.addHelpRow(help_label)
        layout.finish()
        self.tabs.addTab(tab, "Devices")
        self.device_filter_editor.changed.connect(self._device_filters_changed)
        self.primary_default_combo.currentIndexChanged.connect(self._primary_default_volume_changed)
        self.secondary_default_combo.currentIndexChanged.connect(
            self._secondary_default_volume_changed
        )
        self.primary_match_combo.currentIndexChanged.connect(self._update_preview)
        self.secondary_match_combo.currentIndexChanged.connect(self._update_preview)

    def _build_appearance_tab(self) -> None:
        tab, layout = _create_form_tab()

        self.theme_combo = QComboBox()
        self.theme_combo.addItem("Follow system theme", "system")
        self.theme_combo.addItem("Light", "light")
        self.theme_combo.addItem("Dark", "dark")
        self._set_combo_by_data(self.theme_combo, self._config.theme)
        layout.addRow("Theme:", self.theme_combo)
        set_form_row_tooltip(
            layout,
            self.theme_combo,
            "Choose the application appearance or follow the operating-system theme.",
        )

        heading = QLabel("Copy log colors")
        heading.setStyleSheet("font-weight: 600;")
        layout.addFullRow(heading)

        self.log_color_controls = {
            LogMessageType.INFORMATION: ColorPickerControl(
                self._config.copyloginformationcolor
            ),
            LogMessageType.COPIED: ColorPickerControl(self._config.copylogcopiedcolor),
            LogMessageType.CONFIRMED: ColorPickerControl(
                self._config.copylogconfirmedcolor
            ),
            LogMessageType.WARNING: ColorPickerControl(self._config.copylogwarningcolor),
            LogMessageType.ERROR: ColorPickerControl(self._config.copylogerrorcolor),
        }
        self.log_background_control = ColorPickerControl(
            self._config.copylogbackgroundcolor
        )
        labels = {
            LogMessageType.INFORMATION: "Information:",
            LogMessageType.COPIED: "Successful copy:",
            LogMessageType.CONFIRMED: "Copied after confirmation:",
            LogMessageType.WARNING: "Warning:",
            LogMessageType.ERROR: "Error:",
        }
        descriptions = {
            LogMessageType.INFORMATION: (
                "Color used for routine progress and configuration messages."
            ),
            LogMessageType.COPIED: (
                "Color used for successful copies and verification results that "
                "required no user decision."
            ),
            LogMessageType.CONFIRMED: (
                "Color used for successful actions performed after a conflict or "
                "mismatch decision."
            ),
            LogMessageType.WARNING: (
                "Color used for mismatches, intentional omissions, and other nonfatal "
                "safety conditions."
            ),
            LogMessageType.ERROR: (
                "Color used for failed copies, failed verification, formatting failures, "
                "and internal errors."
            ),
        }
        for message_type, control in self.log_color_controls.items():
            layout.addRow(labels[message_type], control)
            control.set_help_text(descriptions[message_type])
            set_form_row_tooltip(layout, control, descriptions[message_type])
            control.changed.connect(self._update_log_preview)
        layout.addRow("Background:", self.log_background_control)
        self.log_background_control.set_help_text("Background color of the copy log.")
        set_form_row_tooltip(
            layout,
            self.log_background_control,
            "Background color of the copy log.",
        )
        self.log_background_control.changed.connect(self._update_log_preview)

        reset_all_button = QPushButton("Reset all colors to automatic")
        reset_all_button.clicked.connect(self._reset_log_colors)
        set_help_tooltip(
            reset_all_button,
            "Return every copy-log color to its automatic theme-derived value.",
        )
        layout.addFullRow(reset_all_button)

        preview_label = QLabel("Preview")
        preview_label.setStyleSheet("font-weight: 600;")
        layout.addFullRow(preview_label)

        self.log_preview = QTextEdit()
        self.log_preview.setReadOnly(True)
        self.log_preview.setMaximumHeight(170)
        layout.addFullRow(self.log_preview)

        layout.finish()
        self.theme_combo.currentIndexChanged.connect(self._update_log_preview)
        self.tabs.addTab(tab, "Appearance")
        self._update_log_preview()

    def _reset_log_colors(self) -> None:
        for control in self.log_color_controls.values():
            control.set_value(None)
        self.log_background_control.set_value(None)
        self._update_log_preview()

    def _update_log_preview(self) -> None:
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return
        theme = cast(ThemeMode, str(self.theme_combo.currentData()))
        defaults = automatic_log_colors(theme, preview_palette(app, theme))
        default_by_type = {
            LogMessageType.INFORMATION: defaults.information,
            LogMessageType.COPIED: defaults.copied,
            LogMessageType.CONFIRMED: defaults.confirmed,
            LogMessageType.WARNING: defaults.warning,
            LogMessageType.ERROR: defaults.error,
        }
        for message_type, control in self.log_color_controls.items():
            control.set_automatic_color(default_by_type[message_type])
        self.log_background_control.set_automatic_color(defaults.background)

        samples = [
            (LogMessageType.INFORMATION, "Scanning first-source volume…"),
            (LogMessageType.COPIED, "COPIED: DSC04755.ARW"),
            (
                LogMessageType.CONFIRMED,
                "VOLUME MISMATCH — COPIED: DSC04643.ARW",
            ),
            (LogMessageType.WARNING, "VOLUME MISMATCH: DSC04643.ARW"),
            (LogMessageType.ERROR, "CLONE FAILED: DSC04755.ARW"),
        ]
        html_lines = []
        for message_type, text in samples:
            value = self.log_color_controls[message_type].value()
            color = value or default_by_type[message_type].name()
            html_lines.append(
                f'<span style="color:{color};">{html_escape(text)}</span>'
            )
        background = self.log_background_control.value() or defaults.background.name()
        self.log_preview.setStyleSheet(f"QTextEdit {{ background-color: {background}; }}")
        self.log_preview.setHtml("<br>".join(html_lines))
        self._refresh_inline_warnings()

    def _refresh_inline_warnings(self) -> None:
        app = QApplication.instance()
        if not isinstance(app, QApplication):
            return
        theme = (
            cast(ThemeMode, str(self.theme_combo.currentData()))
            if hasattr(self, "theme_combo")
            else self._config.theme
        )
        warning_color = automatic_log_colors(theme, preview_palette(app, theme)).warning.name()
        warning_style = f"color: {warning_color};"

        if hasattr(self, "hash_warning_label"):
            self.hash_warning_label.setStyleSheet(warning_style)
            self.hash_warning_label.setVisible(not self.hash_checkbox.isChecked())
        if hasattr(self, "durable_writes_warning_label"):
            self.durable_writes_warning_label.setStyleSheet(warning_style)
            self.durable_writes_warning_label.setVisible(
                not self.durable_writes_checkbox.isChecked()
            )
        if hasattr(self, "rating_warning_label"):
            self.rating_warning_label.setStyleSheet(warning_style)
            self.rating_warning_label.setVisible(
                int(self.rating_combo.currentData() or 0) > 0
                and self.embedded_metadata_checkbox.isChecked()
                and not self._exiftool_available
            )

    def _build_advanced_tab(self) -> None:
        tab, layout = _create_form_tab()
        self._format_service = FormatService()
        self.format_combo = QComboBox()
        self.format_combo.addItem("Do not format", None)
        availability = self._format_service.available_filesystems()
        for filesystem, reason in availability.items():
            label = filesystem if reason is None else f"{filesystem} (unavailable: {reason})"
            self.format_combo.addItem(label, filesystem)
            index = self.format_combo.count() - 1
            self.format_combo.model().item(index).setEnabled(reason is None)
        self._set_combo_by_data(self.format_combo, self._config.autoformat)
        self.format_requirements_button = QPushButton("Check format requirements")
        self.format_requirements_button.clicked.connect(self._show_format_requirements_dialog)
        self.format_prompt_checkbox = QCheckBox("Ask before formatting")
        self.format_prompt_checkbox.setChecked(self._config.formatprompt)
        self.durable_writes_checkbox = QCheckBox("Flush copied files to disk after writing")
        self.durable_writes_checkbox.setChecked(self._config.durablewrites)
        self.durable_writes_warning_label = QLabel(
            "Durable writes are disabled. Copying may finish before all data has been "
            "committed to the destination device. Source removal and formatting will "
            "have reduced protection."
        )
        layout.addRow("Default format choice:", self.format_combo)
        layout.addRow("Format requirements:", self.format_requirements_button)
        layout.addRow("Format confirmation:", self.format_prompt_checkbox)
        format_warning = QLabel(
            "Formatting erases the volume. CameraCopy blocks formatting if the copy job "
            "fails or is cancelled. Disabling the format prompt is dangerous and should "
            "only be used in a controlled workflow."
        )
        layout.addHelpRow(format_warning)
        layout.addRow("Durable writes:", self.durable_writes_checkbox)
        layout.addHelpRow(self.durable_writes_warning_label)
        set_form_row_tooltip(
            layout,
            self.format_combo,
            "Set the filesystem offered by default for formatting after a successful copy.",
            "Formatting erases the selected source volumes.",
        )
        set_form_row_tooltip(
            layout,
            self.format_requirements_button,
            "Show whether the tools and services required for each supported filesystem "
            "are available.",
        )
        set_form_row_tooltip(
            layout,
            self.format_prompt_checkbox,
            "Require a final confirmation before each source volume is formatted.",
            "Disabling this removes an important safety check.",
        )
        set_form_row_tooltip(
            layout,
            self.durable_writes_checkbox,
            "Flush each copied file to storage before verification and request a "
            "destination-folder flush after renaming.",
            "This is slower but more conservative.",
        )
        self.durable_writes_checkbox.toggled.connect(self._refresh_inline_warnings)

        application_heading = QLabel("Open destination in application")
        application_heading.setStyleSheet("font-weight: 600;")
        layout.addRow(application_heading)

        self.application_name_edit = QLineEdit(self._config.applicationname)
        self.application_path_edit = QLineEdit(self._config.applicationpath)
        application_browse_button = QPushButton("Browse…")
        application_browse_button.clicked.connect(self._browse_application_path)
        application_path_row = QHBoxLayout()
        application_path_row.addWidget(self.application_path_edit, 1)
        application_path_row.addWidget(application_browse_button)
        self.application_arguments_edit = QLineEdit(self._config.applicationarguments)
        self.application_environment_edit = QLineEdit(
            self._config.applicationenvironment
        )
        self.application_preview_label = QLabel()
        self.application_preview_label.setWordWrap(True)
        application_preview_policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Maximum,
        )
        application_preview_policy.setHeightForWidth(True)
        self.application_preview_label.setSizePolicy(application_preview_policy)
        self.application_preview_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )

        layout.addRow("Application name:", self.application_name_edit)
        layout.addRow("Path to application:", application_path_row)
        layout.addRow("Environment variables:", self.application_environment_edit)
        layout.addRow("Command-line arguments:", self.application_arguments_edit)
        layout.addRow("Example launch:", self.application_preview_label)

        set_form_row_tooltip(
            layout,
            self.application_name_edit,
            "Name shown on the completion-dialog button.",
            "Both an application name and application path are required before the "
            "button is shown.",
        )
        set_form_row_tooltip(
            layout,
            application_path_row,
            "Select the executable CameraCopy should launch.",
            "Both an application path and application name are required before the "
            "completion-dialog button is shown.",
            controls=(self.application_path_edit, application_browse_button),
        )
        set_help_tooltip(
            application_browse_button,
            "Choose the application executable CameraCopy should launch.",
        )
        set_form_row_tooltip(
            layout,
            self.application_environment_edit,
            "Optional environment variables supplied only to the launched application.",
            "Enter space-separated NAME=value assignments. Quote values containing "
            'spaces, for example CACHE_PATH="/path with spaces".',
        )
        set_form_row_tooltip(
            layout,
            self.application_arguments_edit,
            "Optional arguments passed to the application. Use %d wherever the "
            "destination directory should be inserted.",
            "Leave this field empty, or omit %d, to launch the application without "
            "passing the destination directory.",
        )
        set_form_row_tooltip(
            layout,
            self.application_preview_label,
            "Shows the executable, environment variables, and arguments CameraCopy "
            "will use with the currently configured destination.",
            "CameraCopy launches the application directly without a command shell.",
        )

        application_help = QLabel(
            "Use %d in command-line arguments to pass the destination directory. "
            "Leave the arguments empty, or omit %d, to launch the application without "
            "giving it a directory. The completion button is shown only when both an "
            "application name and path are configured."
        )
        layout.addHelpRow(application_help)

        for control in (
            self.application_name_edit,
            self.application_path_edit,
            self.application_arguments_edit,
            self.application_environment_edit,
        ):
            control.textChanged.connect(self._update_application_preview)
        self.destination_edit.textChanged.connect(self._update_application_preview)

        layout.finish()
        self.tabs.addTab(tab, "Advanced")
        self._update_application_preview()

    def _show_format_requirements_dialog(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Format requirements")
        dialog.resize(560, 360)

        output = QPlainTextEdit(FormatService().compatibility_report(), dialog)
        output.setReadOnly(True)
        output.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        output.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close, dialog)
        buttons.rejected.connect(dialog.reject)

        layout = QVBoxLayout(dialog)
        description = QLabel("Detected formatting tools and services:", dialog)
        layout.addWidget(description)
        layout.addWidget(output, 1)
        layout.addWidget(buttons)
        dialog.exec()

    def _populate_device_default_combos(
        self,
        primary_value: str | None = None,
        secondary_value: str | None = None,
        primary_match_method: VolumeMatchMethod | None = None,
        secondary_match_method: VolumeMatchMethod | None = None,
    ) -> None:
        initial_primary = primary_value is None
        initial_secondary = secondary_value is None
        primary_value = (
            self._config.defaultprimaryvolumeid if primary_value is None else primary_value
        )
        secondary_value = (
            self._config.defaultsecondaryvolumeid if secondary_value is None else secondary_value
        )
        if initial_primary:
            matched_primary = find_volume_by_match(
                self._volumes, self._config.defaultprimaryvolumematch
            )
            primary_value = matched_primary.id if matched_primary is not None else primary_value
        if initial_secondary:
            matched_secondary = find_volume_by_match(
                self._volumes, self._config.defaultsecondaryvolumematch
            )
            secondary_value = (
                matched_secondary.id if matched_secondary is not None else secondary_value
            )
        primary_match_method = (
            self._config.defaultprimaryvolumematch.method
            if primary_match_method is None
            else primary_match_method
        )
        secondary_match_method = (
            self._config.defaultsecondaryvolumematch.method
            if secondary_match_method is None
            else secondary_match_method
        )

        self.primary_default_combo.blockSignals(True)
        self.secondary_default_combo.blockSignals(True)
        self.primary_match_combo.blockSignals(True)
        self.secondary_match_combo.blockSignals(True)
        self.primary_default_combo.clear()
        self.secondary_default_combo.clear()

        self.primary_default_combo.addItem("None — no default primary volume", "")
        self.secondary_default_combo.addItem("None — no default secondary volume", "")

        for volume in self._volumes:
            self.primary_default_combo.addItem(volume.display_name, volume.id)
            self.primary_default_combo.setItemData(
                self.primary_default_combo.count() - 1,
                volume.display_name,
                Qt.ItemDataRole.ToolTipRole,
            )
            self.secondary_default_combo.addItem(volume.display_name, volume.id)
            self.secondary_default_combo.setItemData(
                self.secondary_default_combo.count() - 1,
                volume.display_name,
                Qt.ItemDataRole.ToolTipRole,
            )

        self._set_combo_by_data(self.primary_default_combo, primary_value)
        self._set_combo_by_data(self.secondary_default_combo, secondary_value)
        self._populate_match_combo(
            self.primary_match_combo,
            self._selected_default_volume(self.primary_default_combo),
            primary_match_method,
        )
        self._populate_match_combo(
            self.secondary_match_combo,
            self._selected_default_volume(self.secondary_default_combo),
            secondary_match_method,
        )
        self.primary_default_combo.blockSignals(False)
        self.secondary_default_combo.blockSignals(False)
        self.primary_match_combo.blockSignals(False)
        self.secondary_match_combo.blockSignals(False)

    def _populate_match_combo(
        self,
        combo: QComboBox,
        volume: VolumeInfo | None,
        selected_method: VolumeMatchMethod = "device_serial",
    ) -> None:
        combo.clear()
        for method in MATCH_METHODS:
            combo.addItem(volume_match_label(volume, method), method)
            index = combo.count() - 1
            tooltip = self._match_method_tooltip(volume, method)
            combo.setItemData(index, tooltip, Qt.ItemDataRole.ToolTipRole)
        self._set_combo_by_data(combo, selected_method)

    @staticmethod
    def _match_method_tooltip(volume: VolumeInfo | None, method: VolumeMatchMethod) -> str:
        value = volume_match_value(volume, method) if volume is not None else ""
        if value == "" or value is None:
            return f"{MATCH_METHOD_LABELS[method]} is unavailable for the selected volume."
        return f"Will match future volumes where {MATCH_METHOD_LABELS[method]} equals {value}."

    def _primary_default_volume_changed(self) -> None:
        method = cast(VolumeMatchMethod, self.primary_match_combo.currentData() or "device_serial")
        self._populate_match_combo(
            self.primary_match_combo,
            self._selected_default_volume(self.primary_default_combo),
            method,
        )
        self._update_preview()

    def _secondary_default_volume_changed(self) -> None:
        method = cast(
            VolumeMatchMethod, self.secondary_match_combo.currentData() or "device_serial"
        )
        self._populate_match_combo(
            self.secondary_match_combo,
            self._selected_default_volume(self.secondary_default_combo),
            method,
        )
        self._update_preview()

    def _selected_default_volume(self, combo: QComboBox) -> VolumeInfo | None:
        selected_id = str(combo.currentData() or "")
        for volume in self._volumes:
            if volume.id == selected_id:
                return volume
        return None

    @staticmethod
    def _set_combo_by_data(combo: QComboBox, value: object) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value and combo.model().item(index).isEnabled():
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(0)

    def _apply_pattern_preset(self) -> None:
        preset = self.pattern_preset_combo.currentData()
        if not isinstance(preset, FilePatternPreset):
            return
        self.include_editor.set_values(preset.include)
        self.exclude_editor.set_values(preset.exclude)

    def _date_preset_changed(self) -> None:
        custom = self.date_preset_combo.currentData() is None
        self.custom_date_edit.setEnabled(custom)
        if not custom:
            self.custom_date_edit.setText(str(self.date_preset_combo.currentData() or ""))
        self._update_preview()

    def _browse_destination(self) -> None:
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose destination folder",
            self.destination_edit.text() or str(Path.home()),
        )
        if selected:
            self.destination_edit.setText(selected)

    def _browse_application_path(self) -> None:
        current = Path(self.application_path_edit.text()).expanduser()
        start_dir = current.parent if current.parent.exists() else Path.home()
        selected, _selected_filter = QFileDialog.getOpenFileName(
            self,
            "Choose application executable",
            str(start_dir),
            "Applications (*.exe);;All files (*)"
            if sys.platform == "win32"
            else "All files (*)",
        )
        if selected:
            self.application_path_edit.setText(selected)

    def _update_application_preview(self) -> None:
        if not hasattr(self, "application_preview_label"):
            return
        config = self._config_from_controls()
        try:
            launch = build_application_launch(
                config,
                config.destination or str(Path.home() / "Pictures"),
                require_executable=False,
            )
        except ApplicationConfigurationError as exc:
            self.application_preview_label.setText(f"Preview unavailable: {exc}")
            return
        if launch is None:
            self.application_preview_label.setText("Not configured")
            return
        self.application_preview_label.setText(launch.preview())

    def _browse_source_subfolder(self) -> None:
        start_dir = self._source_browse_start_dir()
        selected = QFileDialog.getExistingDirectory(
            self, "Choose source folder on volume", start_dir
        )
        if not selected:
            return
        self.source_edit.setText(self._source_subfolder_from_selected_path(Path(selected)))

    def _source_browse_start_dir(self) -> str:
        current_source = normalize_source_subfolder(self.source_edit.text())
        preview_volume = self._preview_volume()
        if preview_volume is not None:
            candidate = resolve_source_root(preview_volume, current_source)
            if candidate.exists():
                return str(candidate)
        return str(Path.home())

    def _source_subfolder_from_selected_path(self, selected: Path) -> str:
        selected = selected.expanduser().resolve()
        volumes = sorted(
            self._all_volumes, key=lambda volume: len(str(volume.mount_path)), reverse=True
        )
        for volume in volumes:
            try:
                relative = selected.relative_to(volume.mount_path.expanduser().resolve())
            except ValueError:
                continue
            return "" if str(relative) == "." else relative.as_posix()
        drive = selected.drive
        if drive:
            try:
                return selected.relative_to(Path(drive + selected.root)).as_posix()
            except ValueError:
                return selected.name
        if selected.is_absolute():
            return (
                selected.relative_to(selected.anchor).as_posix()
                if selected.anchor
                else selected.name
            )
        return selected.name

    def _selected_device_ids(self) -> tuple[str, str]:
        primary_id = str(self.primary_default_combo.currentData() or "")
        secondary_id = str(self.secondary_default_combo.currentData() or "")
        return primary_id, secondary_id

    def _selected_device_matches(self) -> tuple[VolumeMatch, VolumeMatch]:
        primary_method = cast(
            VolumeMatchMethod, self.primary_match_combo.currentData() or "device_serial"
        )
        secondary_method = cast(
            VolumeMatchMethod, self.secondary_match_combo.currentData() or "device_serial"
        )
        primary_match = build_volume_match(
            self._selected_default_volume(self.primary_default_combo), primary_method
        )
        secondary_match = build_volume_match(
            self._selected_default_volume(self.secondary_default_combo), secondary_method
        )
        return primary_match, secondary_match

    def _config_from_controls(self) -> CameraCopyConfig:
        if self.date_preset_combo.currentData() is None:
            date_format = self.custom_date_edit.text().strip()
        else:
            date_format = str(self.date_preset_combo.currentData() or "")
        primary_id, secondary_id = self._selected_device_ids()
        primary_match, secondary_match = self._selected_device_matches()
        collision_policy = cast(CollisionPolicy, str(self.collision_combo.currentData()))
        return CameraCopyConfig(
            source=normalize_source_subfolder(self.source_edit.text()),
            destination=self.destination_edit.text().strip(),
            includedfiles=self.include_editor.values() or ["*"],
            excludedfiles=self.exclude_editor.values(),
            includeddevices=self.device_filter_editor.values(),
            folderprefix=self.prefix_edit.text(),
            datetimestring=date_format,
            folderpostfix=self.postfix_edit.text(),
            defaultprimaryvolumeid=primary_id,
            defaultsecondaryvolumeid=secondary_id,
            defaultprimaryvolumematch=primary_match,
            defaultsecondaryvolumematch=secondary_match,
            minrating=int(self.rating_combo.currentData()),
            useembeddedmetadata=self.embedded_metadata_checkbox.isChecked(),
            copysidecars=self.copy_sidecars_checkbox.isChecked(),
            clonemode=self._config.clonemode,
            autoformat=self.format_combo.currentData(),
            autoremove=self.autoremove_checkbox.isChecked(),
            formatprompt=self.format_prompt_checkbox.isChecked(),
            checkhash=self.hash_checkbox.isChecked(),
            durablewrites=self.durable_writes_checkbox.isChecked(),
            fixsonytimestamps=self.fix_sony_checkbox.isChecked(),
            collisionpolicy=collision_policy,
            applicationname=self.application_name_edit.text().strip(),
            applicationpath=self.application_path_edit.text().strip(),
            applicationarguments=self.application_arguments_edit.text().strip(),
            applicationenvironment=self.application_environment_edit.text().strip(),
            theme=cast(ThemeMode, str(self.theme_combo.currentData())),
            copyloginformationcolor=self.log_color_controls[
                LogMessageType.INFORMATION
            ].value(),
            copylogcopiedcolor=self.log_color_controls[LogMessageType.COPIED].value(),
            copylogconfirmedcolor=self.log_color_controls[LogMessageType.CONFIRMED].value(),
            copylogwarningcolor=self.log_color_controls[LogMessageType.WARNING].value(),
            copylogerrorcolor=self.log_color_controls[LogMessageType.ERROR].value(),
            copylogbackgroundcolor=self.log_background_control.value(),
        )

    def _update_preview(self) -> None:
        try:
            config = self._config_from_controls()
            source_example = self._preview_source(config)
            destination_example = preview_destination(config)
            self.preview_label.setText(
                f"Source example: {source_example}\nDestination example: {destination_example}"
            )
        except Exception as exc:  # noqa: BLE001
            self.preview_label.setText(f"Preview unavailable: {exc}")

    def _preview_source(self, config: CameraCopyConfig) -> str:
        preview_volume = self._preview_volume()
        if preview_volume is not None:
            source_root = resolve_source_root(preview_volume, config.source)
        else:
            source_subfolder = normalize_source_subfolder(config.source)
            source_root = (
                Path("/mounted/volume") / source_subfolder
                if source_subfolder
                else Path("/mounted/volume")
            )
        return str(source_root / "DSC0001.ARW")

    def _preview_volume(self) -> VolumeInfo | None:
        if not self._volumes:
            return None
        if hasattr(self, "primary_default_combo"):
            selected_id = str(self.primary_default_combo.currentData() or "")
            for volume in self._volumes:
                if volume.id == selected_id:
                    return volume
        return self._volumes[0]

    def update_volumes(self, volumes: list[VolumeInfo]) -> None:
        """Replace the device list after an asynchronous main-window scan."""
        self._all_volumes = list(volumes)
        self._refresh_device_choices()

    def _device_filters_changed(self) -> None:
        self._refresh_device_choices()

    def _refresh_device_choices(self) -> None:
        primary_value, secondary_value = self._selected_device_ids()
        primary_method = cast(
            VolumeMatchMethod, self.primary_match_combo.currentData() or "device_serial"
        )
        secondary_method = cast(
            VolumeMatchMethod, self.secondary_match_combo.currentData() or "device_serial"
        )
        self._volumes = self._filter_volumes(self.device_filter_editor.values())
        self._populate_device_default_combos(
            primary_value, secondary_value, primary_method, secondary_method
        )
        self._update_preview()

    def _filter_volumes(self, keywords: list[str]) -> list[VolumeInfo]:
        return [volume for volume in self._all_volumes if volume.matches_keywords(keywords)]

    def _validate_and_accept(self) -> None:
        config = self._config_from_controls()
        errors: list[str] = []
        source_error = validate_relative_source(config.source)
        if source_error:
            errors.append(source_error)
        if not config.destination:
            errors.append("Destination folder is required.")
        if not config.includedfiles:
            errors.append("At least one include pattern is required.")
        for label, value in [
            ("Folder prefix", config.folderprefix),
            ("Folder postfix", config.folderpostfix),
        ]:
            folder_error = validate_folder_component(value)
            if folder_error:
                errors.append(f"{label}: {folder_error}")
        try:
            preview_destination(config)
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))
        try:
            build_application_launch(config, config.destination, require_executable=True)
        except ApplicationConfigurationError as exc:
            errors.append(str(exc))
        if config.autoformat and not FormatService().can_format(config.autoformat):
            errors.append(
                f"Selected format filesystem is not available on this system: {config.autoformat}"
            )
        if errors:
            QMessageBox.critical(self, "Invalid settings", "\n".join(errors))
            return
        if config.autoremove and not self._confirm_dangerous_setting(
            "Source cleanup enabled",
            "Source cleanup removes files from the volume after successful copy. Continue?",
        ):
            return
        if (
            config.autoformat
            and not config.formatprompt
            and not self._confirm_dangerous_setting(
                "Format prompt disabled",
                "The format prompt is disabled while default formatting is enabled. "
                "This is dangerous. Continue?",
            )
        ):
            return
        match_warnings = self._volume_match_warnings()
        if match_warnings and not self._confirm_dangerous_setting(
            "Default volume matching warning",
            "\n".join(match_warnings) + "\n\nSave these default volume matching settings anyway?",
        ):
            return
        self._config = config
        self.accept()

    def _volume_match_warnings(self) -> list[str]:
        warnings: list[str] = []
        for role, combo, match_combo in (
            ("Primary", self.primary_default_combo, self.primary_match_combo),
            ("Secondary", self.secondary_default_combo, self.secondary_match_combo),
        ):
            if not combo.currentData():
                continue
            volume = self._selected_default_volume(combo)
            method = cast(VolumeMatchMethod, match_combo.currentData() or "device_serial")
            warning = volume_match_warning(self._volumes, volume, method)
            if warning:
                warnings.append(f"{role}: {warning}")
        return warnings

    def _confirm_dangerous_setting(self, title: str, message: str) -> bool:
        answer = QMessageBox.warning(
            self,
            title,
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        return answer == QMessageBox.StandardButton.Yes

    def config(self) -> CameraCopyConfig:
        return self._config
