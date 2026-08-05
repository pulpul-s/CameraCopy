from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from cameracopy2.models import ThemeMode
_DEFAULT_STYLE_NAME: str | None = None
_DEFAULT_PALETTE: QPalette | None = None


def apply_theme(app: QApplication, theme: ThemeMode) -> None:
    """Apply CameraCopy's simple Qt appearance setting.

    `system` means leave Qt/PySide on the platform default look. Light and dark use
    Qt's built-in Fusion style with an explicit palette so behavior is consistent
    across Linux and Windows.
    """
    _capture_defaults(app)

    if theme == "system":
        _restore_system_theme(app)
    elif theme == "light":
        _apply_light_theme(app)
    elif theme == "dark":
        _apply_dark_theme(app)


def preview_palette(app: QApplication, theme: ThemeMode) -> QPalette:
    """Return the palette used to preview theme-aware copy-log colors."""
    _capture_defaults(app)
    if theme == "system" and _DEFAULT_PALETTE is not None:
        return QPalette(_DEFAULT_PALETTE)
    palette = QPalette(app.palette())
    if theme == "light":
        palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
        palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0))
    elif theme == "dark":
        palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
        palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    return palette


def _capture_defaults(app: QApplication) -> None:
    global _DEFAULT_STYLE_NAME, _DEFAULT_PALETTE
    if _DEFAULT_STYLE_NAME is None:
        _DEFAULT_STYLE_NAME = app.style().objectName()
    if _DEFAULT_PALETTE is None:
        _DEFAULT_PALETTE = QPalette(app.palette())


def _restore_system_theme(app: QApplication) -> None:
    if _DEFAULT_STYLE_NAME:
        app.setStyle(_DEFAULT_STYLE_NAME)
    if _DEFAULT_PALETTE is not None:
        app.setPalette(QPalette(_DEFAULT_PALETTE))
    else:
        app.setPalette(app.style().standardPalette())


def _apply_light_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Base, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(245, 245, 245))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 220))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Text, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.Button, QColor(240, 240, 240))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(0, 0, 0))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Link, QColor(0, 0, 238))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(0, 120, 215))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(120, 120, 120)
    )
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(120, 120, 120))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(120, 120, 120)
    )
    app.setPalette(palette)


def _apply_dark_theme(app: QApplication) -> None:
    app.setStyle("Fusion")
    palette = QPalette()
    palette.setColor(QPalette.ColorRole.Window, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Base, QColor(35, 35, 35))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(45, 45, 45))
    palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.Button, QColor(53, 53, 53))
    palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    palette.setColor(QPalette.ColorRole.Link, QColor(100, 170, 255))
    palette.setColor(QPalette.ColorRole.Highlight, QColor(42, 130, 218))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(140, 140, 140)
    )
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(140, 140, 140))
    palette.setColor(
        QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(140, 140, 140)
    )
    app.setPalette(palette)
