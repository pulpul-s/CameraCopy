from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

SidecarKind = Literal["xmp", "rrdata"]
SidecarNaming = Literal["full_name", "stem"]

_SUPPORTED_EXTENSIONS: tuple[tuple[SidecarKind, str], ...] = (
    ("xmp", ".xmp"),
    ("rrdata", ".rrdata"),
)
_SUPPORTED_SUFFIXES = frozenset(extension for _, extension in _SUPPORTED_EXTENSIONS)


@dataclass(frozen=True, slots=True)
class SidecarMatch:
    path: Path
    kind: SidecarKind
    naming: SidecarNaming

    def destination_for(self, media_destination: Path) -> Path:
        extension = self.path.suffix
        if self.naming == "full_name":
            return media_destination.with_name(media_destination.name + extension)
        return media_destination.with_name(media_destination.stem + extension)


class SidecarIndex:
    """Case-insensitive sidecar lookup with one directory scan per folder."""

    def __init__(self) -> None:
        self._directories: dict[Path, dict[str, list[Path]]] = {}

    def matching(
        self,
        media_path: Path,
        *,
        kinds: set[SidecarKind] | None = None,
    ) -> list[SidecarMatch]:
        directory = media_path.parent
        entries = self._directory_entries(directory)
        matches: list[SidecarMatch] = []
        seen: set[Path] = set()

        for kind, extension in _SUPPORTED_EXTENSIONS:
            if kinds is not None and kind not in kinds:
                continue
            names = (
                (media_path.name + extension, "full_name"),
                (media_path.stem + extension, "stem"),
            )
            for candidate_name, naming in names:
                for candidate in entries.get(candidate_name.casefold(), ()):
                    if candidate in seen:
                        continue
                    seen.add(candidate)
                    matches.append(
                        SidecarMatch(
                            path=candidate,
                            kind=kind,
                            naming=naming,
                        )
                    )
        return matches

    def _directory_entries(self, directory: Path) -> dict[str, list[Path]]:
        cached = self._directories.get(directory)
        if cached is not None:
            return cached

        entries: dict[str, list[Path]] = {}
        try:
            children = list(directory.iterdir())
        except OSError:
            children = []
        for child in children:
            try:
                if child.is_symlink() or not child.is_file():
                    continue
            except OSError:
                continue
            entries.setdefault(child.name.casefold(), []).append(child)
        self._directories[directory] = entries
        return entries


def is_sidecar_path(path: Path) -> bool:
    return path.suffix.casefold() in _SUPPORTED_SUFFIXES
