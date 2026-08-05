from __future__ import annotations

from dataclasses import dataclass

from cameracopy2.models import DEFAULT_INCLUDE_PATTERNS
from cameracopy2.ui.tooltips import help_tooltip_text

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QColorDialog,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


COMMON_CAMERA_MEDIA_PATTERNS = tuple(DEFAULT_INCLUDE_PATTERNS)


@dataclass(frozen=True, slots=True)
class FilePatternPreset:
    name: str
    include: tuple[str, ...]
    exclude: tuple[str, ...]


FILE_PATTERN_PRESETS = [
    FilePatternPreset("Common camera media", COMMON_CAMERA_MEDIA_PATTERNS, ()),
    FilePatternPreset(
        "Sony",
        ("*.ARW", "*.SR2", "*.SRF", "*.JPG", "*.JPEG", "*.MP4", "*.MOV"),
        ("C*T*.JPG",),
    ),
    FilePatternPreset(
        "Canon",
        ("*.CR3", "*.CR2", "*.CRW", "*.JPG", "*.JPEG", "*.MP4", "*.MOV"),
        (),
    ),
    FilePatternPreset(
        "Nikon",
        ("*.NEF", "*.NRW", "*.JPG", "*.JPEG", "*.MP4", "*.MOV"),
        (),
    ),
    FilePatternPreset(
        "Fujifilm",
        ("*.RAF", "*.JPG", "*.JPEG", "*.MP4", "*.MOV"),
        (),
    ),
    FilePatternPreset(
        "Leica",
        ("*.DNG", "*.RWL", "*.JPG", "*.JPEG", "*.MP4", "*.MOV"),
        (),
    ),
    FilePatternPreset("Everything", ("*",), ()),
]


class PatternListEditor(QWidget):
    """Small reusable editor for include/exclude/device keyword lists."""

    changed = Signal()

    def __init__(
        self,
        values: list[str] | None = None,
        placeholder: str = "Example: *.JPG",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.list_widget = QListWidget()
        self.list_widget.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.list_widget.setMinimumHeight(84)
        self.list_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.input = QLineEdit()
        self.input.setPlaceholderText(placeholder)
        add_button = QPushButton("Add")
        remove_button = QPushButton("Remove")

        row = QHBoxLayout()
        row.addWidget(self.input, 1)
        row.addWidget(add_button)
        row.addWidget(remove_button)

        layout = QVBoxLayout(self)
        layout.addWidget(self.list_widget)
        layout.addLayout(row)

        add_button.clicked.connect(self.add_current)
        remove_button.clicked.connect(self.remove_selected)
        self.input.returnPressed.connect(self.add_current)
        self.set_values(values or [])

    def add_current(self) -> None:
        value = self.input.text().strip()
        if value and value not in self.values():
            self.list_widget.addItem(value)
            self.input.clear()
            self.changed.emit()

    def remove_selected(self) -> None:
        selected = self.list_widget.selectedItems()
        if not selected:
            return
        for item in selected:
            self.list_widget.takeItem(self.list_widget.row(item))
        self.changed.emit()

    def set_values(self, values: list[str] | tuple[str, ...]) -> None:
        old_values = self.values()
        self.list_widget.clear()
        for value in values:
            cleaned = str(value).strip()
            if cleaned:
                self.list_widget.addItem(cleaned)
        if self.values() != old_values:
            self.changed.emit()

    def values(self) -> list[str]:
        return [
            self.list_widget.item(index).text().strip()
            for index in range(self.list_widget.count())
            if self.list_widget.item(index).text().strip()
        ]


class ColorPickerControl(QWidget):
    """Color picker with an automatic theme-derived state."""

    changed = Signal()

    def __init__(
        self,
        value: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._value = value
        self._automatic_color = QColor()
        self._help_paragraphs: tuple[str, ...] = ()
        self.pick_button = QPushButton()
        self.reset_button = QPushButton("Reset")
        self.pick_button.clicked.connect(self._choose_color)
        self.reset_button.clicked.connect(self.reset)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pick_button, 1)
        layout.addWidget(self.reset_button)
        self._refresh()

    def set_help_text(self, *paragraphs: str) -> None:
        self._help_paragraphs = tuple(
            paragraph.strip() for paragraph in paragraphs if paragraph.strip()
        )
        self.setToolTip(help_tooltip_text(*self._help_paragraphs))
        self.reset_button.setToolTip(
            help_tooltip_text(
                "Return this color to the automatic value derived from the selected "
                "theme."
            )
        )
        self._refresh()

    def value(self) -> str | None:
        return self._value

    def set_value(self, value: str | None) -> None:
        self._value = value
        self._refresh()

    def set_automatic_color(self, color: QColor) -> None:
        self._automatic_color = QColor(color)
        self._refresh()

    def reset(self) -> None:
        if self._value is None:
            return
        self._value = None
        self._refresh()
        self.changed.emit()

    def _choose_color(self) -> None:
        initial = QColor(self._value) if self._value else QColor(self._automatic_color)
        selected = QColorDialog.getColor(initial, self, "Choose copy log color")
        if not selected.isValid():
            return
        self._value = selected.name(QColor.NameFormat.HexRgb).upper()
        self._refresh()
        self.changed.emit()

    def _refresh(self) -> None:
        color = QColor(self._value) if self._value else QColor(self._automatic_color)
        label = self._value or "Automatic"
        self.pick_button.setText(label)
        if color.isValid():
            foreground = "#000000" if color.lightnessF() > 0.55 else "#FFFFFF"
            self.pick_button.setStyleSheet(
                f"QPushButton {{ background-color: {color.name()}; color: {foreground}; }}"
            )
            current = (
                f"Current color: {color.name().upper()}"
                + (" (automatic)" if self._value is None else "")
            )
            self.pick_button.setToolTip(
                help_tooltip_text(*self._help_paragraphs, current)
            )
        else:
            self.pick_button.setStyleSheet("")
            self.pick_button.setToolTip("")
        self.reset_button.setEnabled(self._value is not None)
