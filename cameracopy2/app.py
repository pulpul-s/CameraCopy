from __future__ import annotations

import sys

from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import QApplication, QMessageBox, QSplashScreen

from .config import ConfigReadError, default_config_path, load_config
from .models import CameraCopyConfig
from .resources import resource_path
from .ui.main_window import MainWindow
from .ui.theme import apply_theme


def _set_application_icon(app: QApplication) -> None:
    icon_path = resource_path("resources", "icons", "cameracopy.ico")
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))


def _show_splash(app: QApplication) -> QSplashScreen | None:
    splash_path = resource_path("resources", "images", "splashback.png")
    if not splash_path.exists():
        return None
    pixmap = QPixmap(str(splash_path))
    if pixmap.isNull():
        return None
    splash = QSplashScreen(pixmap)
    splash.show()
    app.processEvents()
    return splash


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("CameraCopy")
    app.setApplicationDisplayName("CameraCopy")
    app.setOrganizationName("CameraCopy")
    if hasattr(app, "setDesktopFileName"):
        app.setDesktopFileName("cameracopy")
    _set_application_icon(app)
    splash = _show_splash(app)
    config_path = default_config_path()
    config_error: ConfigReadError | None = None
    try:
        config = load_config(config_path)
    except ConfigReadError as exc:
        config_error = exc
        config = CameraCopyConfig()
    apply_theme(app, config.theme)
    window = MainWindow(config_path=config_path, config=config)
    window.show()
    if splash is not None:
        splash.finish(window)
    if config_error is not None:
        QMessageBox.warning(
            window,
            "Settings could not be read",
            "CameraCopy could not read the settings file. The existing file has not "
            "been changed. Default settings will be used for this session.\n\n"
            f"{config_error.error}",
        )
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
