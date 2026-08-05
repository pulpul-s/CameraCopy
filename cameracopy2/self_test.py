from __future__ import annotations

import importlib
import platform
import shutil
import sys
from collections.abc import Callable

from . import __version__
from .resources import resource_path


Check = tuple[str, Callable[[], str]]


def _import_check(module_name: str) -> str:
    module = importlib.import_module(module_name)
    version = getattr(module, "__version__", None)
    return str(version) if version else "available"


def _resource_check(*parts: str) -> str:
    path = resource_path(*parts)
    if not path.is_file():
        raise FileNotFoundError(path)
    return str(path)


def _command_check(command: str) -> str:
    path = shutil.which(command)
    if path is None:
        raise FileNotFoundError(f"required command not found: {command}")
    return path


def _checks() -> list[Check]:
    checks: list[Check] = [
        ("Python", lambda: platform.python_version()),
        ("PySide6", lambda: _import_check("PySide6")),
        ("PySide6.QtCore", lambda: _import_check("PySide6.QtCore")),
        ("PySide6.QtGui", lambda: _import_check("PySide6.QtGui")),
        ("PySide6.QtWidgets", lambda: _import_check("PySide6.QtWidgets")),
        ("psutil", lambda: _import_check("psutil")),
        ("platformdirs", lambda: _import_check("platformdirs")),
        ("dateutil", lambda: _import_check("dateutil")),
        (
            "application icon",
            lambda: _resource_check("resources", "icons", "cameracopy.ico"),
        ),
        (
            "splash image",
            lambda: _resource_check("resources", "images", "splashback.png"),
        ),
    ]
    if sys.platform.startswith("linux"):
        checks.extend(
            [
                ("PySide6.QtDBus", lambda: _import_check("PySide6.QtDBus")),
                ("pyudev", lambda: _import_check("pyudev")),
                ("lsblk", lambda: _command_check("lsblk")),
            ]
        )
    elif sys.platform == "win32":
        checks.append(("pythoncom", lambda: _import_check("pythoncom")))
    return checks


def _optional_checks() -> list[Check]:
    if sys.platform == "win32":
        # WMI enriches Windows volume information, but the application deliberately
        # falls back to psutil when it is unavailable.
        return [("WMI volume metadata", lambda: _import_check("wmi"))]
    return []


def run_self_test() -> int:
    print(f"CameraCopy {__version__} self-test")
    failures: list[tuple[str, Exception]] = []

    for label, check in _checks():
        try:
            detail = check()
        except Exception as exc:  # Report every broken runtime component coherently.
            failures.append((label, exc))
            print(f"FAIL {label}: {type(exc).__name__}: {exc}")
        else:
            print(f"OK   {label}: {detail}")

    optional_warnings = 0
    for label, check in _optional_checks():
        try:
            detail = check()
        except Exception as exc:
            optional_warnings += 1
            print(f"WARN {label}: {type(exc).__name__}: {exc}")
        else:
            print(f"OK   {label}: {detail}")

    if failures:
        print(f"Self-test failed: {len(failures)} check(s) failed.")
        return 1

    if optional_warnings:
        print(f"Self-test passed with {optional_warnings} optional warning(s).")
    else:
        print("Self-test passed.")
    return 0
