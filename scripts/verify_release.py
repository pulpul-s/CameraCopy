from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+")


def read_version() -> str:
    value = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    if not VERSION_PATTERN.fullmatch(value):
        raise ValueError(f"VERSION must contain x.y.z, found {value!r}")
    return value


def normalized_tag(tag: str) -> str:
    return tag[1:] if tag.startswith("v") else tag


def verify(tag: str | None = None) -> str:
    version = read_version()
    init_text = (ROOT / "cameracopy2" / "__init__.py").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")

    expected_init = f'__version__ = "{version}"'
    expected_project = f'version = "{version}"'
    errors: list[str] = []
    if expected_init not in init_text:
        errors.append(f"cameracopy2/__init__.py does not contain {expected_init!r}")
    if expected_project not in pyproject:
        errors.append(f"pyproject.toml does not contain {expected_project!r}")
    if tag is not None and normalized_tag(tag) != version:
        errors.append(f"release tag {tag!r} does not match VERSION {version!r}")
    if errors:
        raise ValueError("\n".join(errors))
    return version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify CameraCopy release metadata.")
    parser.add_argument("--tag", help="Git tag to compare with VERSION; an optional leading v is allowed")
    args = parser.parse_args(argv)
    try:
        version = verify(args.tag)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
