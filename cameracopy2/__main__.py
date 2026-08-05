from __future__ import annotations

import argparse
from collections.abc import Sequence

from . import __version__


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cameracopy",
        description="Copy and verify camera media.",
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--version",
        action="store_true",
        help="show the CameraCopy version and exit",
    )
    group.add_argument(
        "--self-test",
        action="store_true",
        help="verify the installed runtime and packaged resources, then exit",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args, qt_args = parser.parse_known_args(argv)

    if args.version:
        if qt_args:
            parser.error("--version does not accept additional arguments")
        print(__version__)
        return 0

    if args.self_test:
        if qt_args:
            parser.error("--self-test does not accept additional arguments")
        from .self_test import run_self_test

        return run_self_test()

    # Keep Qt's own command-line switches available for normal GUI startup.
    from .app import main as run_application

    return run_application()


if __name__ == "__main__":
    raise SystemExit(main())
