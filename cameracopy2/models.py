from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Literal

CONFIG_VERSION = 2

DEFAULT_INCLUDE_PATTERNS = [
    "*.NEF",
    "*.NRW",
    "*.CR3",
    "*.CR2",
    "*.CRW",
    "*.ARW",
    "*.SR2",
    "*.SRF",
    "*.DNG",
    "*.RWL",
    "*.RAF",
    "*.JPG",
    "*.JPEG",
    "*.MP4",
    "*.MOV",
]

CopyAction = Literal["copied", "skipped", "removed", "failed", "verified_existing"]
FormatRiskKind = Literal[
    "rating_excluded",
    "clone_kept_first_source",
    "clone_used_second_source",
    "volume_mismatch_skipped",
    "volume_mismatch_kept_existing",
]

CopyMode = Literal[
    "copied",
    "overwrote",
    "renamed_copy",
    "verified_existing",
    "clone_verified",
    "clone_mismatch_kept",
    "clone_mismatch_replaced",
    "clone_mismatch_skipped",
    "volume_mismatch_copied",
    "volume_mismatch_kept_both",
    "volume_mismatch_replaced",
    "volume_mismatch_kept_existing",
    "volume_mismatch_verified_existing",
    "volume_mismatch_skipped",
]
CollisionPolicy = Literal["skip", "overwrite", "rename", "ask"]
ThemeMode = Literal["system", "light", "dark"]
VolumeMatchMethod = Literal[
    "device_serial",
    "fs_uuid",
    "label",
    "size",
    "device_path",
    "mount_point",
]
CollisionDecision = Literal["skip", "overwrite", "rename", "cancel"]
CloneMismatchDecision = Literal["keep_both", "replace", "skip", "cancel"]
VolumeMismatchDecision = Literal[
    "copy", "keep_both", "keep_existing", "replace", "skip", "cancel"
]

_WRITTEN_COPY_MODES = {
    "copied",
    "overwrote",
    "renamed_copy",
    "clone_mismatch_kept",
    "clone_mismatch_replaced",
    "volume_mismatch_copied",
    "volume_mismatch_kept_both",
    "volume_mismatch_replaced",
}


@dataclass(frozen=True, slots=True)
class FormatRisk:
    kind: FormatRiskKind
    source: Path


@dataclass(slots=True)
class CloneMismatchResponse:
    decision: CloneMismatchDecision
    remove_source: bool = False


@dataclass(slots=True)
class VolumeMatch:
    method: VolumeMatchMethod = "device_serial"
    value: str | int = ""


@dataclass(slots=True)
class CameraCopyConfig:
    version: int = CONFIG_VERSION
    source: str = "DCIM"
    destination: str = field(default_factory=lambda: str(Path.home() / "Pictures"))
    includedfiles: list[str] = field(default_factory=lambda: list(DEFAULT_INCLUDE_PATTERNS))
    excludedfiles: list[str] = field(default_factory=list)
    includeddevices: list[str] = field(default_factory=list)
    folderprefix: str = ""
    datetimestring: str = "yyyy-MM-dd"
    folderpostfix: str = ""
    defaultprimaryvolumeid: str = ""
    defaultsecondaryvolumeid: str = ""
    defaultprimaryvolumematch: VolumeMatch = field(default_factory=VolumeMatch)
    defaultsecondaryvolumematch: VolumeMatch = field(default_factory=VolumeMatch)
    minrating: int = 0
    useembeddedmetadata: bool = False
    copysidecars: bool = False
    clonemode: bool = False
    autoformat: str | None = None
    autoremove: bool = False
    formatprompt: bool = True
    checkhash: bool = True
    durablewrites: bool = True
    fixsonytimestamps: bool = True
    collisionpolicy: CollisionPolicy = "ask"
    applicationname: str = ""
    applicationpath: str = ""
    applicationarguments: str = ""
    applicationenvironment: str = ""
    theme: ThemeMode = "system"
    copyloginformationcolor: str | None = None
    copylogcopiedcolor: str | None = None
    copylogconfirmedcolor: str | None = None
    copylogwarningcolor: str | None = None
    copylogerrorcolor: str | None = None
    copylogbackgroundcolor: str | None = None


@dataclass(slots=True)
class VolumeInfo:
    id: str
    display_name: str
    mount_path: Path
    device_path: str | None = None
    label: str | None = None
    model: str | None = None
    size_bytes: int | None = None
    filesystem: str | None = None
    removable: bool | None = None
    uuid: str | None = None
    transport: str | None = None
    device_serial: str | None = None
    partition_uuid: str | None = None
    hardware_path: str | None = None
    platform: str = "unknown"

    @property
    def is_available(self) -> bool:
        return self.mount_path.exists()

    @property
    def is_likely_removable(self) -> bool:
        if self.removable is True:
            return True
        mount = str(self.mount_path)
        return mount.startswith(("/media", "/run/media", "/mnt"))

    def matches_keywords(self, keywords: list[str]) -> bool:
        normalized_keywords = [
            keyword.strip().lower() for keyword in keywords if keyword.strip()
        ]
        if not normalized_keywords:
            return True
        haystack = " ".join(
            str(part or "")
            for part in (
                self.display_name,
                self.mount_path,
                self.device_path,
                self.label,
                self.model,
                self.filesystem,
                self.uuid,
                self.transport,
                self.device_serial,
                self.partition_uuid,
                self.hardware_path,
            )
        ).lower()
        return any(keyword in haystack for keyword in normalized_keywords)


@dataclass(slots=True)
class CopyJob:
    primary: VolumeInfo
    secondary: VolumeInfo | None
    config: CameraCopyConfig
    clone_mode: bool = False
    autoremove: bool = False
    format_after_copy: str | None = None

    def selected_volumes(self) -> list[VolumeInfo]:
        volumes = [self.primary]
        if self.secondary is not None:
            volumes.append(self.secondary)
        return volumes


@dataclass(slots=True)
class HashResult:
    source_hash: str | None
    destination_hash: str | None
    ok: bool
    error: str | None = None


@dataclass(slots=True)
class FileCopyResult:
    source: Path
    destination: Path | None
    action: CopyAction
    hash_ok: bool | None = None
    rating: int | None = None
    timestamp: datetime | None = None
    reason: str | None = None
    error: str | None = None
    size_bytes: int = 0
    bytes_copied: int = 0
    timestamp_source: str | None = None
    rating_source: str | None = None
    copy_mode: CopyMode | None = None
    volume_mismatch: bool = False
    first_source_destination: Path | None = None
    first_source_size_bytes: int | None = None

    @property
    def failed(self) -> bool:
        return self.action == "failed" or self.error is not None or self.hash_ok is False

    @property
    def cancelled(self) -> bool:
        return self.action == "skipped" and (self.reason or "").startswith("copy cancelled")


@dataclass(slots=True)
class CopyReport:
    started_at: datetime
    finished_at: datetime | None = None
    results: list[FileCopyResult] = field(default_factory=list)
    logs: list[str] = field(default_factory=list)
    cancelled: bool = False
    prompt_wait_seconds: float = 0.0
    format_risks: list[FormatRisk] = field(default_factory=list)

    def add_log(self, message: str) -> None:
        self.logs.append(message)

    def add_result(self, result: FileCopyResult) -> None:
        self.results.append(result)

    def add_format_risk(self, kind: FormatRiskKind, source: Path) -> None:
        risk = FormatRisk(kind=kind, source=source)
        if risk not in self.format_risks:
            self.format_risks.append(risk)

    @property
    def failures(self) -> list[FileCopyResult]:
        return [result for result in self.results if result.failed]

    @property
    def has_failures(self) -> bool:
        return bool(self.failures)

    @property
    def completed_cleanly(self) -> bool:
        return not self.cancelled and not self.has_failures

    @property
    def copied_count(self) -> int:
        return sum(1 for result in self.results if result.copy_mode in _WRITTEN_COPY_MODES)

    @property
    def new_copy_count(self) -> int:
        return sum(
            1
            for result in self.results
            if result.copy_mode in {"copied", "volume_mismatch_copied"}
        )

    @property
    def kept_both_count(self) -> int:
        return sum(
            1
            for result in self.results
            if result.copy_mode in {
                "renamed_copy",
                "clone_mismatch_kept",
                "volume_mismatch_kept_both",
            }
        )

    @property
    def replaced_count(self) -> int:
        return sum(
            1
            for result in self.results
            if result.copy_mode in {
                "overwrote",
                "clone_mismatch_replaced",
                "volume_mismatch_replaced",
            }
        )

    @property
    def skipped_count(self) -> int:
        return sum(1 for result in self.results if result.action == "skipped")

    @property
    def removed_count(self) -> int:
        return sum(1 for result in self.results if result.action == "removed")

    @property
    def verified_existing_count(self) -> int:
        return sum(
            1
            for result in self.results
            if result.copy_mode in {
                "verified_existing",
                "volume_mismatch_verified_existing",
            }
        )

    @property
    def clone_verified_count(self) -> int:
        return sum(1 for result in self.results if result.copy_mode == "clone_verified")

    def format_risk_counts_for(self, volume: VolumeInfo) -> dict[FormatRiskKind, int]:
        """Return data-loss warning counts for one selected source volume."""
        try:
            mount_root = volume.mount_path.resolve()
        except OSError:
            mount_root = volume.mount_path

        counts: dict[FormatRiskKind, int] = {}
        for risk in self.format_risks:
            try:
                risk.source.resolve().relative_to(mount_root)
            except (OSError, ValueError):
                continue
            counts[risk.kind] = counts.get(risk.kind, 0) + 1
        return counts

    @property
    def failed_count(self) -> int:
        return len(self.failures)

    @property
    def sha256_failure_count(self) -> int:
        return sum(
            1
            for result in self.results
            if result.hash_ok is False
            or "SHA256" in (result.reason or "").upper()
            or "SHA256" in (result.error or "").upper()
        )

    @property
    def total_count(self) -> int:
        return len(self.results)

    @property
    def bytes_copied(self) -> int:
        return sum(result.bytes_copied for result in self.results)

    @property
    def rating_source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            if result.rating_source:
                source_type = result.rating_source.partition(":")[0]
                counts[source_type] = counts.get(source_type, 0) + 1
        return counts

    @property
    def copy_seconds(self) -> float | None:
        if self.finished_at is None:
            return None
        elapsed = (self.finished_at - self.started_at).total_seconds()
        return max(0.0, elapsed - self.prompt_wait_seconds)

    def summary_lines(self) -> list[str]:
        if self.cancelled:
            status = "cancelled"
        elif self.has_failures:
            status = "finished with failures"
        else:
            status = "finished successfully"
        lines = [f"Status: {status}"]
        lines.append("Files:")
        lines.append(f"  {self.total_count} total")
        self._append_count_line(lines, self.copied_count, "copied")
        self._append_count_line(lines, self.new_copy_count, "new")
        self._append_count_line(lines, self.kept_both_count, "kept both")
        self._append_count_line(lines, self.replaced_count, "replaced")
        self._append_count_line(lines, self.verified_existing_count, "verified existing")
        self._append_count_line(lines, self.clone_verified_count, "verified clones")
        self._append_count_line(lines, self.skipped_count, "skipped")
        lines.append(f"  {self.failed_count} failed")
        if self.sha256_failure_count:
            lines.append(f"SHA256 failures: {self.sha256_failure_count}")
        self._append_count_line(lines, self.removed_count, "source files removed")
        if self.rating_source_counts:
            lines.append("Ratings:")
            lines.extend(_format_rating_source_counts(self.rating_source_counts))
        lines.append(f"Bytes written: {self.bytes_copied:,}")
        if self.copy_seconds is not None:
            if self.bytes_copied > 0 and self.copy_seconds > 0:
                average_speed = _format_byte_speed(self.bytes_copied / self.copy_seconds)
                lines.append(f"Average write speed: {average_speed}")
            lines.append(f"Copy time: {_format_elapsed_seconds(self.copy_seconds)}")
        return lines

    @staticmethod
    def _append_count_line(lines: list[str], count: int, label: str) -> None:
        if count:
            lines.append(f"  {count} {label}")


def _format_byte_speed(bytes_per_second: float) -> str:
    return f"{_format_byte_count(int(bytes_per_second))}/s"


def _format_byte_count(size_bytes: int) -> str:
    size = float(max(0, size_bytes))
    units = ["bytes", "KB", "MB", "GB", "TB"]
    unit_index = 0
    while size >= 1024 and unit_index < len(units) - 1:
        size /= 1024
        unit_index += 1
    if unit_index == 0:
        return f"{int(size)} bytes"
    if size >= 100:
        return f"{size:.0f} {units[unit_index]}"
    if size >= 10:
        return f"{size:.1f} {units[unit_index]}"
    return f"{size:.2f} {units[unit_index]}"


def _format_elapsed_seconds(seconds: float) -> str:
    if seconds < 1:
        return f"{seconds:.3f} seconds"
    if seconds < 10:
        return f"{seconds:.2f} seconds"
    return f"{seconds:.1f} seconds"


def _format_rating_source_counts(counts: dict[str, int]) -> list[str]:
    labels = {
        "xmp_sidecar": "XMP sidecar",
        "rrdata_sidecar": "RapidRaw sidecar",
        "embedded_xmp": "Embedded metadata",
        "missing": "No rating found (treated as 0)",
    }
    preferred_order = ("xmp_sidecar", "rrdata_sidecar", "embedded_xmp", "missing")
    keys = [key for key in preferred_order if key in counts]
    keys.extend(sorted(set(counts) - set(preferred_order)))
    lines = []
    for key in keys:
        count = counts[key]
        noun = "file" if count == 1 else "files"
        label = labels.get(key, key.replace("_", " ").capitalize())
        lines.append(f"  {label}: {count} {noun}")
    return lines


@dataclass(slots=True)
class CopyCallbacks:
    log: Callable[[str], None] | None = None
    typed_log: Callable[[str, str], None] | None = None
    progress: Callable[[int, int], None] | None = None
    source_started: Callable[[int, int, str, int], None] | None = None
    source_progress: Callable[[int, int, bool], None] | None = None
    result: Callable[[FileCopyResult], None] | None = None
    file_started: Callable[[Path, int, int, int], None] | None = None
    byte_progress: Callable[[int, int, int], None] | None = None
    collision_decision: Callable[[Path, Path], CollisionDecision] | None = None
    clone_mismatch_decision: Callable[[Path, Path, bool], CloneMismatchResponse] | None = None
    volume_mismatch_decision: (
        Callable[[Path, Path, int, int, bool], VolumeMismatchDecision] | None
    ) = None

    def emit_log(self, message: str) -> None:
        if self.log:
            self.log(message)

    def emit_typed_log(self, message: str, message_type: str) -> bool:
        if self.typed_log:
            self.typed_log(message, message_type)
            return True
        return False

    def emit_progress(self, current: int, total: int) -> None:
        if self.progress:
            self.progress(current, total)

    def emit_source_started(self, index: int, total: int, action: str, total_bytes: int) -> None:
        if self.source_started:
            self.source_started(index, total, action, total_bytes)

    def emit_source_progress(
        self, bytes_done: int, total_bytes: int, metered: bool = False
    ) -> None:
        if self.source_progress:
            self.source_progress(bytes_done, total_bytes, metered)

    def emit_result(self, result: FileCopyResult) -> None:
        if self.result:
            self.result(result)

    def emit_file_started(self, source: Path, index: int, total: int, size_bytes: int) -> None:
        if self.file_started:
            self.file_started(source, index, total, size_bytes)

    def emit_byte_progress(self, bytes_done: int, total_bytes: int, file_index: int) -> None:
        if self.byte_progress:
            self.byte_progress(bytes_done, total_bytes, file_index)

    def ask_collision_decision(self, source: Path, destination: Path) -> CollisionDecision:
        if self.collision_decision:
            return self.collision_decision(source, destination)
        return "skip"

    def ask_clone_mismatch_decision(
        self,
        source: Path,
        destination: Path,
        allow_remove: bool,
    ) -> CloneMismatchResponse:
        if self.clone_mismatch_decision:
            return self.clone_mismatch_decision(source, destination, allow_remove)
        return CloneMismatchResponse(decision="skip", remove_source=False)

    def ask_volume_mismatch_decision(
        self,
        source: Path,
        destination: Path,
        size_bytes: int,
        companion_count: int,
        destination_exists: bool,
    ) -> VolumeMismatchDecision:
        if self.volume_mismatch_decision:
            return self.volume_mismatch_decision(
                source, destination, size_bytes, companion_count, destination_exists
            )
        return "keep_existing" if destination_exists else "skip"
