from __future__ import annotations

import os
import time
from dataclasses import dataclass

UDISKS_SERVICE = "org.freedesktop.UDisks2"
UDISKS_MANAGER_PATH = "/org/freedesktop/UDisks2/Manager"
UDISKS_BLOCK_INTERFACE = "org.freedesktop.UDisks2.Block"
UDISKS_FILESYSTEM_INTERFACE = "org.freedesktop.UDisks2.Filesystem"
DBUS_PEER_INTERFACE = "org.freedesktop.DBus.Peer"
DBUS_PROPERTIES_INTERFACE = "org.freedesktop.DBus.Properties"
SYSTEM_QUERY_TIMEOUT_MS = 15_000
DEVICE_STATE_TIMEOUT_MS = 30_000
FORMAT_TIMEOUT_MS = 10 * 60_000
UNMOUNT_VERIFY_ATTEMPTS = 10
UNMOUNT_VERIFY_DELAY_SECONDS = 0.2
REMOUNT_ATTEMPTS = 2
REMOUNT_RETRY_DELAY_SECONDS = 2
UDISKS_NOT_MOUNTED_ERROR = "org.freedesktop.UDisks2.Error.NotMounted"


@dataclass(frozen=True, slots=True)
class UDisksCallResult:
    ok: bool
    message: str = ""
    arguments: tuple[object, ...] = ()
    error_name: str = ""


def _load_qtdbus():  # noqa: ANN202 - keeps Qt optional during non-GUI source tests
    from PySide6.QtDBus import QDBus, QDBusConnection, QDBusMessage, QDBusVariant

    return QDBus, QDBusConnection, QDBusMessage, QDBusVariant


def qt_dbus_support_error() -> str | None:
    try:
        _load_qtdbus()
    except Exception as exc:  # noqa: BLE001 - dependency diagnostics must report import failures
        detail = str(exc).strip()
        suffix = f": {detail}" if detail else ""
        return f"Qt D-Bus support is unavailable{suffix}."
    return None


class LinuxUDisksClient:
    """Persistent in-process UDisks2 client for one CameraCopy process.

    All formatting calls share Qt's system-bus connection. That keeps one
    D-Bus caller identity across multiple cards, allowing polkit to reuse a
    temporary authorization when the local policy permits it.
    """

    def __init__(self) -> None:
        qdbus, connection_type, message_type, variant_type = _load_qtdbus()
        self._qdbus = qdbus
        self._message_type = message_type
        self._variant_type = variant_type
        self._connection = connection_type.systemBus()

    def service_error(self) -> str | None:
        if not self._connection.isConnected():
            error = self._connection.lastError()
            detail = str(error.message()).strip() if error is not None else ""
            return "Could not connect to the system D-Bus" + (f": {detail}" if detail else ".")
        result = self._call(
            UDISKS_MANAGER_PATH,
            DBUS_PEER_INTERFACE,
            "Ping",
            [],
            timeout_ms=SYSTEM_QUERY_TIMEOUT_MS,
            interactive=False,
        )
        return None if result.ok else result.message

    def format_device(
        self,
        device: str,
        filesystem_type: str,
        label: str | None,
    ) -> UDisksCallResult:
        object_path = self.object_path_for_device(device)
        if object_path is None:
            return UDisksCallResult(False, f"Could not resolve UDisks object path for {device}.")

        options: dict[str, object] = {
            "tear-down": True,
            "update-partition-type": True,
        }
        normalized_label = (label or "").strip()
        if normalized_label:
            options["label"] = normalized_label

        return self._call(
            object_path,
            UDISKS_BLOCK_INTERFACE,
            "Format",
            [filesystem_type, self._variant_map(options)],
            timeout_ms=FORMAT_TIMEOUT_MS,
            interactive=True,
        )

    def unmount_device(self, device: str) -> UDisksCallResult:
        object_path = self.object_path_for_device(device)
        if object_path is None:
            return UDisksCallResult(False, f"Could not resolve UDisks object path for {device}.")

        mount_points = self._mount_points(object_path)
        if not mount_points.ok:
            return UDisksCallResult(
                False,
                f"Could not inspect the mount state for {device}: {mount_points.message}",
            )
        if not mount_points.arguments:
            return UDisksCallResult(True, "The filesystem is already unmounted.")

        result = self._call(
            object_path,
            UDISKS_FILESYSTEM_INTERFACE,
            "Unmount",
            [self._variant_map({"auth.no_user_interaction": False})],
            timeout_ms=DEVICE_STATE_TIMEOUT_MS,
            interactive=True,
        )
        if not result.ok and result.error_name != UDISKS_NOT_MOUNTED_ERROR:
            return result

        last_state = mount_points
        for attempt in range(UNMOUNT_VERIFY_ATTEMPTS):
            last_state = self._mount_points(object_path)
            if not last_state.ok:
                return UDisksCallResult(
                    False,
                    f"Could not verify that {device} was unmounted: {last_state.message}",
                )
            if not last_state.arguments:
                return UDisksCallResult(True)
            if attempt + 1 < UNMOUNT_VERIFY_ATTEMPTS:
                time.sleep(UNMOUNT_VERIFY_DELAY_SECONDS)

        remaining = ", ".join(str(path) for path in last_state.arguments)
        return UDisksCallResult(
            False,
            f"The filesystem is still mounted at {remaining}.",
        )

    def mount_device(self, device: str) -> UDisksCallResult:
        object_path = self.object_path_for_device(device)
        if object_path is None:
            return UDisksCallResult(False, f"Could not resolve UDisks object path for {device}.")

        last_result = UDisksCallResult(False, "Mount was not attempted.")
        for attempt in range(REMOUNT_ATTEMPTS):
            last_result = self._call(
                object_path,
                UDISKS_FILESYSTEM_INTERFACE,
                "Mount",
                [self._variant_map({"auth.no_user_interaction": False})],
                timeout_ms=DEVICE_STATE_TIMEOUT_MS,
                interactive=True,
            )
            if last_result.ok:
                return last_result
            if attempt + 1 < REMOUNT_ATTEMPTS:
                time.sleep(REMOUNT_RETRY_DELAY_SECONDS)
                continue
            break
        return last_result

    def _mount_points(self, object_path: str) -> UDisksCallResult:
        result = self._call(
            object_path,
            DBUS_PROPERTIES_INTERFACE,
            "Get",
            [UDISKS_FILESYSTEM_INTERFACE, "MountPoints"],
            timeout_ms=DEVICE_STATE_TIMEOUT_MS,
            interactive=False,
        )
        if not result.ok:
            return result
        if not result.arguments:
            return UDisksCallResult(False, "UDisks returned no MountPoints property value.")

        value = self._unwrap_variant(result.arguments[0])
        try:
            raw_points = self._mount_point_values(value)
        except (RuntimeError, TypeError):
            return UDisksCallResult(False, "UDisks returned an invalid MountPoints value.")

        decoded: list[str] = []
        for raw_point in raw_points:
            try:
                if isinstance(raw_point, str):
                    point = raw_point.rstrip("\x00")
                else:
                    point = bytes(raw_point).rstrip(b"\x00").decode("utf-8")
            except (TypeError, UnicodeDecodeError, ValueError):
                return UDisksCallResult(False, "UDisks returned an invalid mount path.")
            if point:
                decoded.append(point)
        return UDisksCallResult(True, arguments=tuple(decoded))

    @staticmethod
    def _unwrap_variant(value: object) -> object:
        variant = getattr(value, "variant", None)
        return variant() if callable(variant) else value

    def _mount_point_values(self, value: object) -> list[object]:
        begin_array = getattr(value, "beginArray", None)
        at_end = getattr(value, "atEnd", None)
        end_array = getattr(value, "endArray", None)
        as_variant = getattr(value, "asVariant", None)
        if not all(
            callable(method) for method in (begin_array, at_end, end_array, as_variant)
        ):
            return list(value)  # type: ignore[arg-type]

        mount_points: list[object] = []
        begin_array()
        try:
            while not at_end():
                mount_points.append(as_variant())
        finally:
            end_array()
        return mount_points

    @classmethod
    def object_path_for_device(cls, device: str) -> str | None:
        device_name = os.path.basename(os.path.realpath(device))
        if not device_name:
            return None
        return "/org/freedesktop/UDisks2/block_devices/" + cls._encode_object_component(
            device_name
        )

    @staticmethod
    def _encode_object_component(value: str) -> str:
        encoded: list[str] = []
        for char in value:
            if char == "_" or (char.isascii() and char.isalnum()):
                encoded.append(char)
            else:
                encoded.append(f"_{ord(char):02x}")
        return "".join(encoded)

    def _variant_map(self, values: dict[str, object]) -> dict[str, object]:
        return {key: self._variant_type(value) for key, value in values.items()}

    def _call(
        self,
        object_path: str,
        interface: str,
        method: str,
        arguments: list[object],
        *,
        timeout_ms: int,
        interactive: bool,
    ) -> UDisksCallResult:
        message = self._message_type.createMethodCall(
            UDISKS_SERVICE,
            object_path,
            interface,
            method,
        )
        message.setArguments(arguments)
        message.setInteractiveAuthorizationAllowed(interactive)
        reply = self._connection.call(
            message,
            self._qdbus.CallMode.Block,
            timeout_ms,
        )
        if reply.type() == self._message_type.MessageType.ErrorMessage:
            error_name = str(reply.errorName()).strip()
            error_message = str(reply.errorMessage()).strip()
            if error_name and error_message:
                detail = f"{error_name}: {error_message}"
            else:
                detail = error_message or error_name or "Unknown D-Bus error"
            return UDisksCallResult(False, detail, error_name=error_name)
        if reply.type() != self._message_type.MessageType.ReplyMessage:
            return UDisksCallResult(False, "UDisks returned an invalid D-Bus reply.")
        return UDisksCallResult(True, arguments=tuple(reply.arguments()))

