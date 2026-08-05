from __future__ import annotations

import fnmatch
import re
from pathlib import Path


_NATURAL_PART_RE = re.compile(r"(\d+)")


def _matches_any(name: str, patterns: list[str]) -> bool:
    if not patterns:
        return False
    lower = name.lower()
    return any(fnmatch.fnmatchcase(lower, pattern.lower()) for pattern in patterns)


def natural_path_key(path: Path) -> tuple[tuple[int, object], ...]:
    """Sort paths in the order people expect: image2 before image10."""
    parts: list[tuple[int, object]] = []
    for path_part in path.parts:
        for token in _NATURAL_PART_RE.split(path_part.lower()):
            if not token:
                continue
            parts.append((1, int(token)) if token.isdigit() else (2, token))
        parts.append((0, ""))
    return tuple(parts)


def find_candidate_files(
    source_root: Path,
    include_patterns: list[str],
    exclude_patterns: list[str] | None = None,
) -> list[Path]:
    """Recursively find files matching include patterns and not matching exclusions."""
    source_root = Path(source_root)
    exclude_patterns = exclude_patterns or []
    include_patterns = include_patterns or ["*"]

    if not source_root.exists() or not source_root.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_root}")

    resolved_root = source_root.resolve()
    candidates: list[Path] = []
    for path in source_root.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        try:
            path.resolve().relative_to(resolved_root)
        except (OSError, ValueError):
            continue
        name = path.name
        if _matches_any(name, exclude_patterns):
            continue
        if "*" in include_patterns or _matches_any(name, include_patterns):
            candidates.append(path)
    return sorted(candidates, key=natural_path_key)
