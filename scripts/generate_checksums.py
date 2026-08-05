from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate SHA256SUMS for release assets.")
    parser.add_argument("directory", type=Path)
    parser.add_argument("--output", type=Path, default=Path("SHA256SUMS.txt"))
    args = parser.parse_args()

    files = sorted(
        path
        for path in args.directory.iterdir()
        if path.is_file() and path.resolve() != args.output.resolve()
    )
    if not files:
        parser.error(f"no release assets found in {args.directory}")
    text = "".join(f"{sha256(path)}  {path.name}\n" for path in files)
    args.output.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
