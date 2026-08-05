from __future__ import annotations

from pathlib import Path

import pytest

from cameracopy2.models import CameraCopyConfig
from cameracopy2.services import application_launcher
from cameracopy2.services.application_launcher import (
    ApplicationConfigurationError,
    application_button_text,
    application_button_tooltip,
    application_integration_configured,
    build_application_launch,
    launch_application,
    parse_application_arguments,
    parse_environment_variables,
)


def test_environment_variables_are_space_separated_and_support_quotes() -> None:
    assert parse_environment_variables(
        'CACHE_PATH="/home/olli/RapidRaw Cache" WINDOWS_PATH=C:\\RapidRaw MODE=fast EMPTY='
    ) == {
        "CACHE_PATH": "/home/olli/RapidRaw Cache",
        "WINDOWS_PATH": "C:\\RapidRaw",
        "MODE": "fast",
        "EMPTY": "",
    }


@pytest.mark.parametrize(
    "text, expected",
    [
        ("BROKEN", "NAME=value"),
        ("9INVALID=value", "Invalid environment variable name"),
        ("DUP=one DUP=two", "defined more than once"),
        ("Path=one PATH=two", "defined more than once"),
        ('VALUE="unterminated', "could not be parsed"),
    ],
)
def test_invalid_environment_variables_are_rejected(text: str, expected: str) -> None:
    with pytest.raises(ApplicationConfigurationError, match=expected):
        parse_environment_variables(text)


def test_application_arguments_allow_empty_and_optional_destination(tmp_path: Path) -> None:
    executable = tmp_path / "rraw"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    destination = tmp_path / "Pictures with spaces"

    no_arguments = build_application_launch(
        CameraCopyConfig(applicationname="RapidRaw", applicationpath=str(executable)),
        destination,
    )
    without_destination = build_application_launch(
        CameraCopyConfig(
            applicationname="RapidRaw",
            applicationpath=str(executable),
            applicationarguments="--fast",
        ),
        destination,
    )
    with_destination = build_application_launch(
        CameraCopyConfig(
            applicationname="RapidRaw",
            applicationpath=str(executable),
            applicationarguments="--directory %d --fast",
            applicationenvironment="__NV_DISABLE_EXPLICIT_SYNC=1",
        ),
        destination,
    )

    assert no_arguments is not None and no_arguments.arguments == ()
    assert no_arguments is not None and not no_arguments.uses_destination
    assert without_destination is not None and without_destination.arguments == ("--fast",)
    assert without_destination is not None and not without_destination.uses_destination
    assert with_destination is not None
    assert with_destination.arguments == ("--directory", str(destination), "--fast")
    assert with_destination.uses_destination
    assert with_destination.preview() == (
        f'__NV_DISABLE_EXPLICIT_SYNC=1 {executable} --directory "{destination}" --fast'
    )


def test_application_button_requires_name_and_path() -> None:
    assert not application_integration_configured(CameraCopyConfig())
    assert not application_integration_configured(
        CameraCopyConfig(applicationname="RapidRaw")
    )
    assert not application_integration_configured(
        CameraCopyConfig(applicationpath="/usr/bin/rraw")
    )
    assert application_integration_configured(
        CameraCopyConfig(applicationname="RapidRaw", applicationpath="/usr/bin/rraw")
    )


@pytest.mark.parametrize(
    "config",
    [
        CameraCopyConfig(applicationname="RapidRaw"),
        CameraCopyConfig(applicationpath="/usr/bin/rraw"),
        CameraCopyConfig(applicationarguments="--fast"),
        CameraCopyConfig(applicationenvironment="MODE=fast"),
    ],
)
def test_partial_application_configuration_is_rejected(config: CameraCopyConfig) -> None:
    with pytest.raises(ApplicationConfigurationError, match="both an application name"):
        build_application_launch(config, "/pictures", require_executable=False)


def test_application_is_launched_without_a_shell(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executable = tmp_path / "rraw"
    executable.write_text("", encoding="utf-8")
    executable.chmod(0o755)
    launch = build_application_launch(
        CameraCopyConfig(
            applicationname="RapidRaw",
            applicationpath=str(executable),
            applicationarguments="--directory %d",
            applicationenvironment="MODE=fast",
        ),
        tmp_path / "Pictures",
    )
    assert launch is not None
    captured: dict[str, object] = {}
    sentinel = object()

    def fake_popen(argv: tuple[str, ...], **kwargs: object) -> object:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(application_launcher.subprocess, "Popen", fake_popen)

    assert launch_application(launch) is sentinel
    assert captured["argv"] == (
        str(executable),
        "--directory",
        str(tmp_path / "Pictures"),
    )
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert "shell" not in kwargs
    environment = kwargs["env"]
    assert isinstance(environment, dict)
    assert environment["MODE"] == "fast"


def test_argument_parser_reports_unmatched_quotes() -> None:
    with pytest.raises(ApplicationConfigurationError, match="could not be parsed"):
        parse_application_arguments('--directory "unterminated')


def test_argument_parser_preserves_windows_backslashes() -> None:
    assert parse_application_arguments(r'--cache C:\RapidRaw --library "C:\Photo Library"') == (
        "--cache",
        r"C:\RapidRaw",
        "--library",
        r"C:\Photo Library",
    )


def test_application_button_mentions_destination_only_when_placeholder_is_used() -> None:
    with_destination = CameraCopyConfig(
        applicationname="RapidRaw",
        applicationpath="/usr/bin/rraw",
        applicationarguments="--directory %d --fast",
    )
    without_destination = CameraCopyConfig(
        applicationname="RapidRaw",
        applicationpath="/usr/bin/rraw",
        applicationarguments="--fast",
    )

    assert application_button_text(with_destination) == "Open destination in RapidRaw"
    assert application_button_text(without_destination) == "Open RapidRaw"
    assert application_button_tooltip(with_destination) == (
        "Launch RapidRaw and pass it the destination directory."
    )
    assert application_button_tooltip(without_destination) == (
        "Launch RapidRaw without passing the destination directory."
    )
