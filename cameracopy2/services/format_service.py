from __future__ import annotations

import base64
import os
import platform as py_platform
import re
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from cameracopy2.models import CopyReport, VolumeInfo
from cameracopy2.services.linux_udisks import LinuxUDisksClient, qt_dbus_support_error
from cameracopy2.services.volume_service import (
    FormatTargetMatchReason,
    VolumeService,
    create_volume_service,
)

WINDOWS_FORMATS = ("exFAT", "FAT32", "NTFS")
LINUX_FORMATS = ("exFAT", "FAT32", "ext2", "ext3", "ext4")
SUPPORTED_FORMATS = tuple(dict.fromkeys((*WINDOWS_FORMATS, *LINUX_FORMATS)))
WINDOWS_ELEVATION_REQUIRED = "__CAMERACOPY_ELEVATION_REQUIRED__"
LINUX_FS_TYPES = {
    "exFAT": "exfat",
    "FAT32": "vfat",
    "ext2": "ext2",
    "ext3": "ext3",
    "ext4": "ext4",
}
LINUX_TOOL_CHECKS = {
    "exFAT": (("mkfs.exfat", "mkexfatfs"), "Install exfatprogs."),
    "FAT32": (("mkfs.vfat", "mkdosfs"), "Install dosfstools."),
    "ext2": (("mkfs.ext2", "mke2fs"), "Install e2fsprogs."),
    "ext3": (("mkfs.ext3", "mke2fs"), "Install e2fsprogs."),
    "ext4": (("mkfs.ext4", "mke2fs"), "Install e2fsprogs."),
}


def platform_filesystems(system: str | None = None) -> tuple[str, ...]:
    system_name = (system or py_platform.system()).lower()
    if system_name == "windows":
        return WINDOWS_FORMATS
    if system_name == "linux":
        return LINUX_FORMATS
    return SUPPORTED_FORMATS


@dataclass(slots=True)
class FormatResult:
    ok: bool
    message: str
    target_rejected: bool = False
    rejection_reason: FormatTargetMatchReason | None = None


@dataclass(frozen=True, slots=True)
class FormatDependencyStatus:
    name: str
    available: bool
    detail: str = ""

    def display_line(self) -> str:
        icon = "✅" if self.available else "❌"
        return f"{self.name}: {icon}" if not self.detail else f"{self.name}: {icon} {self.detail}"


class FormatService:
    def __init__(
        self,
        volume_service_factory: Callable[[], VolumeService] | None = None,
        linux_udisks_factory: Callable[[], LinuxUDisksClient] | None = None,
    ) -> None:
        self._udisks_service_error_checked = False
        self._udisks_service_error: str | None = None
        self._volume_service_factory = volume_service_factory or create_volume_service
        self._linux_udisks_factory = linux_udisks_factory or LinuxUDisksClient
        self._linux_udisks_client: LinuxUDisksClient | None = None

    @classmethod
    def format_target_description(cls, volume: VolumeInfo, filesystem: str) -> str:
        """Return a user-facing description of a pending format operation."""
        details = [
            f"mount: {volume.mount_path}",
            f"device: {volume.device_path or 'unknown'}",
            f"current label: {volume.label or 'unlabeled'}",
            f"current filesystem: {volume.filesystem or 'unknown'}",
        ]
        if volume.size_bytes is not None:
            details.append(f"size: {cls._format_bytes(volume.size_bytes)}")
        return f"{volume.display_name} as {filesystem} ({'; '.join(details)})"

    @staticmethod
    def _format_bytes(size_bytes: int) -> str:
        units = ["B", "KB", "MB", "GB", "TB"]
        value = float(size_bytes)
        unit = units[0]
        for unit in units:
            if abs(value) < 1024 or unit == units[-1]:
                break
            value /= 1024
        if unit == "B":
            return f"{int(value)} B"
        return f"{value:.1f} {unit}"

    def available_filesystems(self) -> dict[str, str | None]:
        """Return platform-supported filesystems and a message when unavailable."""
        system = py_platform.system().lower()
        platform_formats = platform_filesystems(system)
        if system == "windows":
            if self._windows_shell() is None:
                return {filesystem: "PowerShell was not found." for filesystem in platform_formats}
            if not self._windows_wmi_available():
                return {
                    filesystem: (
                        "WMI support was not found; removable-drive detection is "
                        "unavailable."
                    )
                    for filesystem in platform_formats
                }
            return {filesystem: None for filesystem in platform_formats}
        if system != "linux":
            return {
                filesystem: f"Formatting is not implemented for {system}."
                for filesystem in platform_formats
            }

        base_error = self._linux_base_dependency_error()
        if base_error:
            return {filesystem: base_error for filesystem in platform_formats}

        udisks_error = self._linux_udisks_service_error()
        if udisks_error:
            return {filesystem: udisks_error for filesystem in platform_formats}

        availability: dict[str, str | None] = {}
        for filesystem in platform_formats:
            tools, missing_message = LINUX_TOOL_CHECKS[filesystem]
            availability[filesystem] = (
                None if any(shutil.which(tool) for tool in tools) else missing_message
            )
        return availability

    def can_format(self, filesystem: str | None) -> bool:
        if filesystem not in platform_filesystems():
            return False
        return self.available_filesystems().get(filesystem) is None

    def compatibility_statuses(self) -> list[FormatDependencyStatus]:
        """Return a diagnostic checklist for formatting support on this platform."""
        system = py_platform.system().lower()
        if system == "windows":
            shell = self._windows_shell()
            wmi_available = self._windows_wmi_available()
            is_admin = self._windows_is_admin()
            return [
                FormatDependencyStatus("PowerShell", shell is not None, shell or "not found"),
                FormatDependencyStatus(
                    "Administrator access",
                    True,
                    "already elevated" if is_admin else "UAC requested only if required",
                ),
                FormatDependencyStatus(
                    "WMI support",
                    wmi_available,
                    "available" if wmi_available else "not found",
                ),
                FormatDependencyStatus(
                    "Removable detection",
                    wmi_available,
                    "available via WMI" if wmi_available else "unavailable without WMI",
                ),
                FormatDependencyStatus("Windows filesystems", True, ", ".join(WINDOWS_FORMATS)),
            ]
        if system != "linux":
            return [FormatDependencyStatus("Formatting", False, f"not implemented for {system}")]

        statuses: list[FormatDependencyStatus] = []
        qt_dbus_error = qt_dbus_support_error()
        statuses.append(
            FormatDependencyStatus(
                "Qt D-Bus",
                qt_dbus_error is None,
                "available via PySide6" if qt_dbus_error is None else qt_dbus_error,
            )
        )
        if qt_dbus_error:
            statuses.append(
                FormatDependencyStatus("UDisks2 service", False, "Qt D-Bus unavailable")
            )
        else:
            udisks_error = self._linux_udisks_service_error()
            statuses.append(
                FormatDependencyStatus(
                    "UDisks2 service", udisks_error is None, udisks_error or "available"
                )
            )

        for filesystem in LINUX_FORMATS:
            tools, missing_message = LINUX_TOOL_CHECKS[filesystem]
            found = next((tool for tool in tools if shutil.which(tool)), None)
            name = f"{filesystem} formatter"
            detail = (
                found if found is not None else f"missing: {' or '.join(tools)}; {missing_message}"
            )
            statuses.append(FormatDependencyStatus(name, found is not None, detail))
        return statuses

    def compatibility_report_lines(self) -> list[str]:
        return [status.display_line() for status in self.compatibility_statuses()]

    def compatibility_report(self) -> str:
        return "\n".join(self.compatibility_report_lines())

    def format_volume(
        self,
        volume: VolumeInfo,
        filesystem: str,
        report: CopyReport,
        before_format: Callable[[VolumeInfo], None] | None = None,
    ) -> FormatResult:
        if not report.completed_cleanly:
            return FormatResult(
                False, "Formatting blocked because the copy report has failures or was cancelled."
            )

        system = py_platform.system().lower()
        if filesystem not in SUPPORTED_FORMATS:
            return FormatResult(False, f"Unsupported filesystem: {filesystem}")
        if filesystem not in platform_filesystems(system):
            return FormatResult(False, f"{filesystem} formatting is not supported on {system}.")

        unavailable_reason = self.available_filesystems().get(filesystem)
        if unavailable_reason:
            return FormatResult(False, unavailable_reason)

        match = self._volume_service_factory().find_matching_format_target(volume)
        if not match.ok or match.volume is None:
            return FormatResult(
                False,
                self._format_target_rejection_message(volume, match.reason),
                target_rejected=True,
                rejection_reason=match.reason,
            )
        current_volume = match.volume
        if before_format is not None:
            before_format(current_volume)

        if system == "windows":
            return self._format_windows(current_volume, filesystem)
        if system == "linux":
            return self._format_linux(current_volume, filesystem)
        return FormatResult(False, f"Formatting is not implemented for {system}.")

    @staticmethod
    def _format_target_rejection_message(
        volume: VolumeInfo, reason: FormatTargetMatchReason
    ) -> str:
        explanations = {
            "disconnected": "the original volume is no longer connected",
            "changed": "the drive letter or device path now belongs to a different volume",
            "ambiguous": "more than one connected volume has the recorded identity",
            "identity_unavailable": (
                "the volume does not have a strong filesystem or partition identity"
            ),
            "enumeration_failed": "CameraCopy could not inspect the currently connected volumes",
            "matched": "the volume identity could not be verified",
        }
        explanation = explanations[reason]
        return (
            f'Formatting skipped for "{volume.display_name}": {explanation}. '
            "No formatting command was run."
        )

    def _format_windows(self, volume: VolumeInfo, filesystem: str) -> FormatResult:
        mount = str(volume.mount_path)
        match = re.match(r"^([A-Za-z]):\\?$", mount)
        if not match:
            return FormatResult(False, f"Cannot determine Windows drive letter from {mount!r}")
        drive_letter = match.group(1).upper()
        safety_error = self._windows_format_safety_error(volume, drive_letter)
        if safety_error:
            return FormatResult(False, safety_error)

        shell = self._windows_shell()
        if shell is None:
            return FormatResult(False, "PowerShell was not found.")
        if not self._windows_wmi_available():
            return FormatResult(
                False, "WMI support was not found; removable-drive detection is unavailable."
            )

        format_command = self._windows_format_command(
            drive_letter, filesystem, volume.label
        )
        command = [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            format_command,
        ]
        success_message = f"Formatted {drive_letter}: as {filesystem}."
        result = self._run_command(command, success_message)
        if result.ok or WINDOWS_ELEVATION_REQUIRED not in result.message:
            return result

        elevated_command = self._windows_elevated_command(shell, format_command)
        elevated_result = self._run_command(elevated_command, success_message)
        if elevated_result.ok:
            return elevated_result
        return FormatResult(
            False,
            elevated_result.message
            or "Administrator approval was cancelled or formatting failed.",
        )

    def _format_linux(self, volume: VolumeInfo, filesystem: str) -> FormatResult:
        safety_error = self._linux_format_safety_error(volume)
        if safety_error:
            return FormatResult(False, safety_error)
        device = volume.device_path
        if device is None:  # Defensive; _linux_format_safety_error already checks this.
            return FormatResult(False, "Cannot determine Linux block device.")
        if filesystem not in LINUX_FS_TYPES:
            return FormatResult(False, f"{filesystem} formatting is not supported on Linux.")

        try:
            client = self._linux_udisks()
            unmounted = client.unmount_device(device)
            if not unmounted.ok:
                return FormatResult(
                    False,
                    f"Could not unmount {device} before formatting: {unmounted.message}",
                )
            formatted = client.format_device(
                device,
                LINUX_FS_TYPES[filesystem],
                volume.label,
            )
        except Exception as exc:  # noqa: BLE001 - surface Qt D-Bus initialization failures
            return FormatResult(False, f"Could not start Qt D-Bus formatting: {exc}")
        if not formatted.ok:
            return FormatResult(False, formatted.message)

        mounted = client.mount_device(device)
        if not mounted.ok:
            return FormatResult(
                False,
                f"Formatted {device} as {filesystem}, but mounting it again failed: "
                f"{mounted.message}",
            )
        return FormatResult(True, f"Formatted {device} as {filesystem} and mounted it again.")

    @classmethod
    def _windows_format_command(cls, drive_letter: str, filesystem: str, label: str | None) -> str:
        parts = [
            "Format-Volume",
            f"-DriveLetter {drive_letter}",
            f"-FileSystem {filesystem}",
        ]
        normalized_label = (label or "").strip()
        if normalized_label:
            parts.append(f"-NewFileSystemLabel {cls._powershell_single_quoted(normalized_label)}")
        parts.extend(["-Confirm:$false", "-Force", "-ErrorAction Stop"])
        command = " ".join(parts)
        return (
            f"try {{ {command} | Out-Null }} catch {{ "
            "$errorText = ($_ | Out-String).Trim(); "
            "$errorCode = $_.Exception.HResult; "
            "$nativeCode = $_.Exception.NativeErrorCode; "
            "$errorId = $_.FullyQualifiedErrorId; "
            "$errorCategory = $_.CategoryInfo.Category; "
            "$needsElevation = "
            "($errorCategory -eq 'PermissionDenied') -or "
            "($errorCode -eq -2147024891) -or "
            "($nativeCode -eq 5) -or ($nativeCode -eq 740) -or "
            "($errorId -match 'AccessDenied|UnauthorizedAccess') -or "
            "($errorText -match 'access (is )?denied|requires elevation|required privilege'); "
            "if ($needsElevation) { "
            f"[Console]::Error.WriteLine('{WINDOWS_ELEVATION_REQUIRED}') }}; "
            "[Console]::Error.WriteLine($errorText); exit 1 }"
        )

    @classmethod
    def _windows_elevated_command(cls, shell: str, format_command: str) -> list[str]:
        encoded_command = base64.b64encode(format_command.encode("utf-16le")).decode("ascii")
        quoted_shell = cls._powershell_single_quoted(shell)
        quoted_encoded = cls._powershell_single_quoted(encoded_command)
        launcher = (
            "$ErrorActionPreference = 'Stop'; "
            "try { "
            f"$process = Start-Process -FilePath {quoted_shell} "
            "-ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-EncodedCommand',"
            f"{quoted_encoded}) -Verb RunAs -WindowStyle Hidden -Wait -PassThru; "
            "if ($process.ExitCode -ne 0) { "
            "[Console]::Error.WriteLine('Elevated formatting failed.'); "
            "exit $process.ExitCode }; exit 0 "
            "} catch { "
            "if ($_.Exception.NativeErrorCode -eq 1223) { "
            "[Console]::Error.WriteLine('Administrator approval was cancelled.'); exit 1 }; "
            "[Console]::Error.WriteLine(($_ | Out-String).Trim()); exit 1 }"
        )
        return [
            shell,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            launcher,
        ]

    @staticmethod
    def _powershell_single_quoted(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    @staticmethod
    def _windows_format_safety_error(volume: VolumeInfo, drive_letter: str) -> str | None:
        if FormatService._is_windows_system_drive(drive_letter):
            return f"Refusing to format Windows system drive {drive_letter}:"
        if volume.removable is False:
            return f"Refusing to format non-removable Windows volume {volume.display_name}."
        if volume.removable is not True:
            return (
                "Refusing to format this Windows volume because CameraCopy could not "
                f"confirm it is removable: {volume.display_name}."
            )
        return None

    @staticmethod
    def _linux_format_safety_error(volume: VolumeInfo) -> str | None:
        device = volume.device_path
        if not device or not device.startswith("/dev/"):
            return f"Cannot determine Linux block device from {volume.device_path!r}"

        mount_text = volume.mount_path.expanduser().as_posix()
        critical_mounts = {"/", "/boot", "/boot/efi", "/home", "/usr", "/var", "/tmp", "/opt"}
        try:
            resolved_mount = volume.mount_path.expanduser().resolve()
        except OSError:
            resolved_mount = Path(mount_text)
        if resolved_mount.as_posix() in critical_mounts or mount_text in critical_mounts:
            return f"Refusing to format critical Linux mount {volume.mount_path}."

        if volume.removable is False:
            return f"Refusing to format non-removable Linux volume {volume.display_name}."
        if volume.removable is not True and not FormatService._is_linux_removable_media_mount(
            volume.mount_path
        ):
            return (
                f"Refusing to format {volume.display_name}: volume is not marked removable "
                "and is not mounted under /media or /run/media."
            )
        return None

    @staticmethod
    def _is_linux_removable_media_mount(mount_path: Path) -> bool:
        mount_text = str(mount_path.expanduser())
        return mount_text.startswith(("/media/", "/run/media/"))

    @staticmethod
    def _windows_shell() -> str | None:
        return shutil.which("powershell.exe") or shutil.which("powershell") or shutil.which("pwsh")

    @staticmethod
    def _windows_is_admin() -> bool:
        try:
            import ctypes

            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    @staticmethod
    def _windows_wmi_available() -> bool:
        try:
            __import__("wmi")
        except Exception:
            return False
        return True

    @staticmethod
    def _is_windows_system_drive(drive_letter: str) -> bool:
        system_drive = os.environ.get("SystemDrive", "C:").strip().upper().rstrip("\\")
        return system_drive == f"{drive_letter.upper()}:"

    @staticmethod
    def _linux_base_dependency_error() -> str | None:
        return qt_dbus_support_error()

    def _linux_udisks(self) -> LinuxUDisksClient:
        if self._linux_udisks_client is None:
            self._linux_udisks_client = self._linux_udisks_factory()
        return self._linux_udisks_client

    def _linux_udisks_service_error(self) -> str | None:
        if self._udisks_service_error_checked:
            return self._udisks_service_error
        try:
            service_error = self._linux_udisks().service_error()
        except Exception as exc:  # noqa: BLE001 - dependency diagnostics must not crash settings
            service_error = str(exc) or exc.__class__.__name__
        self._udisks_service_error_checked = True
        self._udisks_service_error = (
            None
            if service_error is None
            else f"UDisks2 service is not available: {service_error}"
        )
        return self._udisks_service_error

    @staticmethod
    def _run_command(
        command: list[str],
        success_message: str,
        timeout_seconds: float | None = None,
    ) -> FormatResult:
        run_options: dict[str, object] = {
            "check": False,
            "stdin": subprocess.DEVNULL,
            "capture_output": True,
            "text": True,
            "timeout": timeout_seconds,
        }
        if os.name == "nt" or py_platform.system().lower() == "windows":
            run_options["creationflags"] = getattr(
                subprocess, "CREATE_NO_WINDOW", 0x08000000
            )
        try:
            completed = subprocess.run(command, **run_options)
        except subprocess.TimeoutExpired:
            return FormatResult(False, f"Command timed out: {' '.join(command)}")
        except OSError as exc:
            return FormatResult(False, str(exc))
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        message = stderr or stdout
        if completed.returncode == 0:
            return FormatResult(True, success_message or stdout or stderr)
        return FormatResult(False, message or f"Command failed: {' '.join(command)}")
