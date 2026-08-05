from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QPalette

from cameracopy2.core.log_messages import LogMessageType
from cameracopy2.models import CameraCopyConfig, ThemeMode


@dataclass(frozen=True, slots=True)
class LogColors:
    information: QColor
    copied: QColor
    confirmed: QColor
    warning: QColor
    error: QColor
    background: QColor

    def for_message(self, message_type: LogMessageType) -> QColor:
        return {
            LogMessageType.INFORMATION: self.information,
            LogMessageType.COPIED: self.copied,
            LogMessageType.CONFIRMED: self.confirmed,
            LogMessageType.WARNING: self.warning,
            LogMessageType.ERROR: self.error,
        }[message_type]


def resolved_log_colors(config: CameraCopyConfig, palette: QPalette) -> LogColors:
    defaults = automatic_log_colors(config.theme, palette)
    return LogColors(
        information=_custom_or_default(config.copyloginformationcolor, defaults.information),
        copied=_custom_or_default(config.copylogcopiedcolor, defaults.copied),
        confirmed=_custom_or_default(config.copylogconfirmedcolor, defaults.confirmed),
        warning=_custom_or_default(config.copylogwarningcolor, defaults.warning),
        error=_custom_or_default(config.copylogerrorcolor, defaults.error),
        background=_custom_or_default(config.copylogbackgroundcolor, defaults.background),
    )


def automatic_log_colors(theme: ThemeMode, palette: QPalette) -> LogColors:
    background = QColor(palette.color(QPalette.ColorRole.Base))
    information = QColor(palette.color(QPalette.ColorRole.Text))
    is_dark = theme == "dark" or (theme == "system" and background.lightnessF() < 0.5)

    if is_dark:
        return LogColors(
            information=information,
            copied=QColor("#7BD88F"),
            confirmed=QColor("#64D8CB"),
            warning=QColor("#FFB454"),
            error=QColor("#FF6B6B"),
            background=background,
        )
    return LogColors(
        information=information,
        copied=QColor("#2E7D32"),
        confirmed=QColor("#00796B"),
        warning=QColor("#9A5B00"),
        error=QColor("#B3261E"),
        background=background,
    )


def _custom_or_default(value: str | None, default: QColor) -> QColor:
    return QColor(value) if value else QColor(default)
