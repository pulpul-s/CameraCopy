from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cameracopy2.models import VolumeInfo
from cameracopy2.platform.linux import LinuxVolumeService
from cameracopy2.services import linux_udisks
from cameracopy2.services.format_service import FormatService
from cameracopy2.services.linux_udisks import (
    DBUS_PROPERTIES_INTERFACE,
    DEVICE_STATE_TIMEOUT_MS,
    FORMAT_TIMEOUT_MS,
    REMOUNT_RETRY_DELAY_SECONDS,
    SYSTEM_QUERY_TIMEOUT_MS,
    UDISKS_FILESYSTEM_INTERFACE,
    UDISKS_NOT_MOUNTED_ERROR,
    LinuxUDisksClient,
    UDisksCallResult,
)


class FakeVariant:
    def __init__(self, value: object) -> None:
        self.value = value


class FakeMessage:
    class MessageType:
        ErrorMessage = "error"
        ReplyMessage = "reply"

    def __init__(self, service: str, path: str, interface: str, method: str) -> None:
        self.service = service
        self.path = path
        self.interface = interface
        self.method = method
        self.sent_arguments: list[object] = []
        self.interactive = False

    @classmethod
    def createMethodCall(
        cls,
        service: str,
        path: str,
        interface: str,
        method: str,
    ) -> FakeMessage:
        return cls(service, path, interface, method)

    def setArguments(self, arguments: list[object]) -> None:  # noqa: N802 - Qt API spelling
        self.sent_arguments = arguments

    def setInteractiveAuthorizationAllowed(self, enabled: bool) -> None:  # noqa: N802
        self.interactive = enabled


class FakeReply:
    def __init__(self, arguments: tuple[object, ...] = ()) -> None:
        self._arguments = arguments

    def type(self) -> str:
        return FakeMessage.MessageType.ReplyMessage

    def arguments(self) -> list[object]:
        return list(self._arguments)


class FakeQDBus:
    class CallMode:
        Block = "block"


class FakeConnection:
    def __init__(self) -> None:
        self.calls: list[tuple[FakeMessage, object, int]] = []

    def isConnected(self) -> bool:  # noqa: N802 - Qt API spelling
        return True

    def call(self, message: FakeMessage, mode: object, timeout: int) -> FakeReply:
        self.calls.append((message, mode, timeout))
        if message.method == "Mount":
            return FakeReply(("/run/media/olli/CARD",))
        return FakeReply()


class FakeConnectionType:
    connection = FakeConnection()

    @classmethod
    def systemBus(cls) -> FakeConnection:  # noqa: N802 - Qt API spelling
        return cls.connection


def _fake_qtdbus():
    return FakeQDBus, FakeConnectionType, FakeMessage, FakeVariant


def test_linux_format_calls_share_one_system_bus_connection(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    FakeConnectionType.connection = FakeConnection()
    monkeypatch.setattr(linux_udisks, "_load_qtdbus", _fake_qtdbus)
    client = LinuxUDisksClient()

    first = client.format_device("/dev/sdb1", "exfat", "CARD ONE")
    second = client.format_device("/dev/sdc1", "exfat", "CARD TWO")

    assert first.ok and second.ok
    assert len(FakeConnectionType.connection.calls) == 2
    first_message = FakeConnectionType.connection.calls[0][0]
    second_message = FakeConnectionType.connection.calls[1][0]
    assert first_message.method == second_message.method == "Format"
    assert first_message.interactive and second_message.interactive
    assert first_message.sent_arguments[0] == "exfat"
    assert second_message.sent_arguments[0] == "exfat"
    assert first_message.sent_arguments[1]["label"].value == "CARD ONE"
    assert second_message.sent_arguments[1]["label"].value == "CARD TWO"
    assert first_message.sent_arguments[1]["tear-down"].value is True
    assert first_message.sent_arguments[1]["update-partition-type"].value is True


def test_linux_mount_uses_same_client_and_returns_mount_path(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    FakeConnectionType.connection = FakeConnection()
    monkeypatch.setattr(linux_udisks, "_load_qtdbus", _fake_qtdbus)
    client = LinuxUDisksClient()

    result = client.mount_device("/dev/sdb1")

    assert result.ok
    assert result.arguments == ("/run/media/olli/CARD",)
    [call] = FakeConnectionType.connection.calls
    message, mode, timeout = call
    assert message.method == "Mount"
    assert message.interactive
    assert mode == FakeQDBus.CallMode.Block
    assert timeout == linux_udisks.DEVICE_STATE_TIMEOUT_MS
    assert message.sent_arguments[0]["auth.no_user_interaction"].value is False


def _client_with_fake_call(fake_call):  # noqa: ANN001, ANN202
    client = LinuxUDisksClient.__new__(LinuxUDisksClient)
    client._variant_type = lambda value: value  # type: ignore[attr-defined]
    client._call = fake_call  # type: ignore[method-assign]
    return client


class FakeMountPointArgument:
    def __init__(self, values: list[bytes]) -> None:
        self._values = iter(values)
        self._current: bytes | None = None
        self._ended = False

    def beginArray(self) -> None:  # noqa: N802 - Qt API spelling
        self._advance()

    def atEnd(self) -> bool:  # noqa: N802 - Qt API spelling
        return self._ended

    def endArray(self) -> None:  # noqa: N802 - Qt API spelling
        return None

    def asVariant(self) -> bytes:  # noqa: N802 - Qt API spelling
        assert self._current is not None
        current = self._current
        self._advance()
        return current

    def __rshift__(self, _target: object):
        raise AssertionError("QDBusArgument stream extraction must not be used")

    def _advance(self) -> None:
        try:
            self._current = next(self._values)
        except StopIteration:
            self._current = None
            self._ended = True


def test_linux_unmount_verifies_mount_points_before_and_after() -> None:
    calls: list[tuple[str, str, list[object], int, bool]] = []
    mount_states = iter(
        [
            UDisksCallResult(True, arguments=([b"/run/media/olli/CARD\x00"],)),
            UDisksCallResult(True, arguments=([],)),
        ]
    )

    def fake_call(  # noqa: ANN001, ANN202
        path, interface, method, arguments, *, timeout_ms, interactive
    ):
        calls.append((interface, method, arguments, timeout_ms, interactive))
        if method == "Get":
            return next(mount_states)
        return UDisksCallResult(True)

    client = _client_with_fake_call(fake_call)

    result = client.unmount_device("/dev/sdb1")

    assert result.ok
    assert [method for _interface, method, *_rest in calls] == ["Get", "Unmount", "Get"]
    first_interface, _, first_arguments, first_timeout, first_interactive = calls[0]
    assert first_interface == DBUS_PROPERTIES_INTERFACE
    assert first_arguments == [UDISKS_FILESYSTEM_INTERFACE, "MountPoints"]
    assert first_timeout == DEVICE_STATE_TIMEOUT_MS
    assert not first_interactive
    unmount_interface, _, unmount_arguments, _, unmount_interactive = calls[1]
    assert unmount_interface == UDISKS_FILESYSTEM_INTERFACE
    assert unmount_arguments[0]["auth.no_user_interaction"] is False
    assert unmount_interactive


def test_linux_mount_point_property_demarshals_qdbus_argument() -> None:
    replies = iter(
        [
            UDisksCallResult(
                True,
                arguments=(FakeMountPointArgument([b"/run/media/olli/CARD\x00"]),),
            ),
            UDisksCallResult(True),
            UDisksCallResult(True, arguments=(FakeMountPointArgument([]),)),
        ]
    )

    def fake_call(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return next(replies)

    client = _client_with_fake_call(fake_call)

    assert client.unmount_device("/dev/sdb1").ok


def test_linux_unmount_skips_dbus_unmount_when_already_unmounted() -> None:
    methods: list[str] = []

    def fake_call(  # noqa: ANN001, ANN202
        _path, _interface, method, _arguments, *, timeout_ms, interactive
    ):
        methods.append(method)
        assert timeout_ms == DEVICE_STATE_TIMEOUT_MS
        assert not interactive
        return UDisksCallResult(True, arguments=([],))

    client = _client_with_fake_call(fake_call)

    result = client.unmount_device("/dev/sdb1")

    assert result.ok
    assert "already unmounted" in result.message.lower()
    assert methods == ["Get"]


def test_linux_unmount_accepts_not_mounted_race_after_state_check() -> None:
    calls = 0

    def fake_call(  # noqa: ANN001, ANN202
        _path, _interface, method, _arguments, *, timeout_ms, interactive
    ):
        nonlocal calls
        calls += 1
        if method == "Get":
            state = [b"/run/media/olli/CARD\x00"] if calls == 1 else []
            return UDisksCallResult(True, arguments=(state,))
        return UDisksCallResult(
            False,
            "The device is not mounted.",
            error_name=UDISKS_NOT_MOUNTED_ERROR,
        )

    client = _client_with_fake_call(fake_call)

    assert client.unmount_device("/dev/sdb1").ok


def test_linux_format_stops_before_format_when_unmount_fails(tmp_path: Path) -> None:
    calls: list[str] = []

    class FakeClient:
        def unmount_device(self, _device: str) -> UDisksCallResult:
            calls.append("unmount")
            return UDisksCallResult(False, "Device is busy")

        def format_device(self, *_args):  # noqa: ANN002, ANN202
            calls.append("format")
            raise AssertionError("format must not run after an unmount failure")

        def mount_device(self, *_args):  # noqa: ANN002, ANN202
            calls.append("mount")
            raise AssertionError("mount must not run after an unmount failure")

    mount = tmp_path / "card"
    mount.mkdir()
    volume = VolumeInfo(
        id="card",
        display_name="Card",
        mount_path=mount,
        device_path="/dev/sdb1",
        removable=True,
        platform="linux",
    )
    service = FormatService(linux_udisks_factory=FakeClient)  # type: ignore[arg-type]

    result = service._format_linux(volume, "exFAT")  # noqa: SLF001

    assert not result.ok
    assert result.message == "Could not unmount /dev/sdb1 before formatting: Device is busy"
    assert calls == ["unmount"]


def test_linux_lsblk_uses_system_query_timeout(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    captured: dict[str, object] = {}

    def fake_run(command, **kwargs):  # noqa: ANN001, ANN202
        captured.update(kwargs)
        return SimpleNamespace(stdout='{"blockdevices": []}')

    monkeypatch.setattr("cameracopy2.platform.linux.subprocess.run", fake_run)

    assert LinuxVolumeService._lsblk_by_path() == {}  # noqa: SLF001
    assert captured["timeout"] == SYSTEM_QUERY_TIMEOUT_MS // 1000


def test_linux_udisks_service_check_is_cached() -> None:
    calls: list[str] = []

    class FakeClient:
        def service_error(self) -> str | None:
            calls.append("service_error")
            return None

    service = FormatService(linux_udisks_factory=FakeClient)  # type: ignore[arg-type]

    assert service._linux_udisks_service_error() is None  # noqa: SLF001
    assert service._linux_udisks_service_error() is None  # noqa: SLF001
    assert calls == ["service_error"]


def test_linux_dbus_uses_long_format_timeout_and_short_device_timeout() -> None:
    assert FORMAT_TIMEOUT_MS > DEVICE_STATE_TIMEOUT_MS
    assert SYSTEM_QUERY_TIMEOUT_MS < DEVICE_STATE_TIMEOUT_MS


def test_linux_remount_retries_once_after_failure(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    client = LinuxUDisksClient.__new__(LinuxUDisksClient)
    client._variant_type = lambda value: value  # type: ignore[attr-defined]
    calls: list[tuple[str, int]] = []
    sleeps: list[float] = []

    def fake_call(_path, _interface, method, _arguments, *, timeout_ms, interactive):  # noqa: ANN001, ANN202
        calls.append((method, timeout_ms))
        if len(calls) == 1:
            return UDisksCallResult(False, "Unknown interface while filesystem refreshes")
        return UDisksCallResult(True, arguments=("/run/media/olli/CARD",))

    client._call = fake_call  # type: ignore[method-assign]
    monkeypatch.setattr("cameracopy2.services.linux_udisks.time.sleep", sleeps.append)

    result = client.mount_device("/dev/sdb1")

    assert result.ok
    assert calls == [
        ("Mount", DEVICE_STATE_TIMEOUT_MS),
        ("Mount", DEVICE_STATE_TIMEOUT_MS),
    ]
    assert sleeps == [REMOUNT_RETRY_DELAY_SECONDS]
