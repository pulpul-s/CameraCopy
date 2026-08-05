from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path

IGNORED_DIRECTORIES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "__pycache__",
}
IGNORED_SUFFIXES = {".pyc", ".pyo"}


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return {
        name
        for name in names
        if name in IGNORED_DIRECTORIES or Path(name).suffix in IGNORED_SUFFIXES
    }


def _normalize_modes(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError(f"Linux package source must not contain symlinks: {path}")
        if path.is_dir():
            path.chmod(0o755)
        elif path.is_file():
            path.chmod(0o644)


def stage(source: Path, destination: Path) -> None:
    source = source.resolve(strict=True)
    destination = destination.resolve()

    if destination.exists():
        shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, destination, ignore=_ignore, symlinks=True)
    _normalize_modes(destination)

    leaked = [
        path
        for path in destination.rglob("*")
        if path.name in IGNORED_DIRECTORIES or path.suffix in IGNORED_SUFFIXES
    ]
    if leaked:
        raise RuntimeError(f"ignored build artifacts leaked into staging: {leaked}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage normalized CameraCopy Linux sources.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    stage(args.source, args.destination)
    return os.EX_OK


if __name__ == "__main__":
    raise SystemExit(main())
