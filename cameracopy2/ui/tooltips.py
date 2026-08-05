from __future__ import annotations

from PySide6.QtWidgets import QFormLayout, QLayout, QWidget

from cameracopy2.ui.tooltip_text import help_tooltip_text

__all__ = ["help_tooltip_text", "set_form_row_tooltip", "set_help_tooltip"]


def set_help_tooltip(widget: QWidget, *paragraphs: str) -> None:
    widget.setToolTip(help_tooltip_text(*paragraphs))


def set_form_row_tooltip(
    form: QFormLayout,
    field: QWidget | QLayout,
    *paragraphs: str,
    controls: tuple[QWidget, ...] = (),
) -> None:
    """Apply the same help text to a form label and its practical hover targets."""
    tooltip = help_tooltip_text(*paragraphs)
    label = form.labelForField(field)
    if label is not None:
        label.setToolTip(tooltip)
    if isinstance(field, QWidget):
        field.setToolTip(tooltip)
    for control in controls:
        control.setToolTip(tooltip)
