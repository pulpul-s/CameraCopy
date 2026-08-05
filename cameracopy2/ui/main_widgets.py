from __future__ import annotations

from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QFontMetrics
from PySide6.QtWidgets import QComboBox, QLabel, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    """Single-line label that preserves the full value in its tooltip."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._full_text = ""
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.setWordWrap(False)
        self.set_full_text(text)

    def set_full_text(self, text: str) -> None:
        self._full_text = text
        self.setToolTip(text)
        self._apply_elide()

    def resizeEvent(self, event: Any) -> None:  # noqa: ANN401, N802 - Qt override
        super().resizeEvent(event)
        self._apply_elide()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().sizeHint()
        return QSize(80, hint.height())

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        hint = super().minimumSizeHint()
        return QSize(20, hint.height())

    def _apply_elide(self) -> None:
        metrics = QFontMetrics(self.font())
        width = max(20, self.contentsRect().width())
        self.setText(metrics.elidedText(self._full_text, Qt.TextElideMode.ElideRight, width))


class VolumeComboBox(QComboBox):
    """Combo box with a popup wide enough for long mount and device names."""

    def showPopup(self) -> None:  # noqa: N802 - Qt override
        view = self.view()
        view.setTextElideMode(Qt.TextElideMode.ElideNone)
        metrics = self.fontMetrics()
        widest = self.width()
        for index in range(self.count()):
            widest = max(widest, metrics.horizontalAdvance(self.itemText(index)) + 60)
        view.setMinimumWidth(min(widest, 1200))
        super().showPopup()
