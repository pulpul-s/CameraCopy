from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import sys


@contextmanager
def windows_com_initialized() -> Iterator[None]:
    """Initialize COM for the current worker thread on Windows."""
    if sys.platform != "win32":
        yield
        return

    import pythoncom

    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()
