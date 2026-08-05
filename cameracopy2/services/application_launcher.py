from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
from typing import Mapping

from cameracopy2.models import CameraCopyConfig

_ENVIRONMENT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PREVIEW_SAFE = re.compile(r"^[A-Za-z0-9_./:\\%+=,@-]+$")


class ApplicationConfigurationError(ValueError):
    """Raised when the configured destination application cannot be launched safely."""


@dataclass(frozen=True, slots=True)
class ApplicationLaunch:
    name: str
    executable: Path
    arguments: tuple[str, ...]
    environment: Mapping[str, str]
    destination: Path
    passes_destination: bool

    @property
    def uses_destination(self) -> bool:
        return self.passes_destination

    @property
    def argv(self) -> tuple[str, ...]:
        return (str(self.executable), *self.arguments)

    def preview(self) -> str:
        environment = [
            f"{name}={_quote_preview(value)}" for name, value in self.environment.items()
        ]
        command = [_quote_preview(value) for value in self.argv]
        return " ".join((*environment, *command))


def application_button_text(config: CameraCopyConfig) -> str:
    name = config.applicationname.strip()
    if "%d" in config.applicationarguments:
        return f"Open destination in {name}"
    return f"Open {name}"


def application_button_tooltip(config: CameraCopyConfig) -> str:
    name = config.applicationname.strip()
    if "%d" in config.applicationarguments:
        return f"Launch {name} and pass it the destination directory."
    return f"Launch {name} without passing the destination directory."


def application_integration_configured(config: CameraCopyConfig) -> bool:
    return bool(config.applicationname.strip() and config.applicationpath.strip())


def parse_application_arguments(text: str) -> tuple[str, ...]:
    if not text.strip():
        return ()
    try:
        return _split_quoted_fields(text)
    except ValueError as exc:
        raise ApplicationConfigurationError(
            f"Command-line arguments could not be parsed: {exc}"
        ) from exc


def parse_environment_variables(text: str) -> dict[str, str]:
    if not text.strip():
        return {}
    try:
        entries = _split_quoted_fields(text)
    except ValueError as exc:
        raise ApplicationConfigurationError(
            f"Environment variables could not be parsed: {exc}"
        ) from exc

    variables: dict[str, str] = {}
    normalized_names: set[str] = set()
    for entry in entries:
        if "=" not in entry:
            raise ApplicationConfigurationError(
                f"Environment variable must use NAME=value: {entry!r}"
            )
        name, value = entry.split("=", 1)
        if not _ENVIRONMENT_NAME.fullmatch(name):
            raise ApplicationConfigurationError(
                f"Invalid environment variable name: {name!r}"
            )
        normalized_name = name.casefold()
        if normalized_name in normalized_names:
            raise ApplicationConfigurationError(
                f"Environment variable is defined more than once: {name}"
            )
        variables[name] = value
        normalized_names.add(normalized_name)
    return variables


def build_application_launch(
    config: CameraCopyConfig,
    destination: str | Path,
    *,
    require_executable: bool = True,
) -> ApplicationLaunch | None:
    name = config.applicationname.strip()
    executable_text = config.applicationpath.strip()
    arguments_text = config.applicationarguments.strip()
    environment_text = config.applicationenvironment.strip()

    has_any_value = bool(name or executable_text or arguments_text or environment_text)
    if not has_any_value:
        return None
    if not name or not executable_text:
        raise ApplicationConfigurationError(
            "Enter both an application name and an application path, or leave all "
            "application integration fields empty."
        )

    executable = Path(executable_text).expanduser()
    if require_executable:
        if not executable.is_file():
            raise ApplicationConfigurationError(
                f"Application executable does not exist: {executable}"
            )
        if os.name != "nt" and not os.access(executable, os.X_OK):
            raise ApplicationConfigurationError(
                f"Application file is not executable: {executable}"
            )

    destination_path = Path(destination).expanduser()
    arguments = tuple(
        argument.replace("%d", str(destination_path))
        for argument in parse_application_arguments(arguments_text)
    )
    environment = parse_environment_variables(environment_text)
    return ApplicationLaunch(
        name=name,
        executable=executable,
        arguments=arguments,
        environment=environment,
        destination=destination_path,
        passes_destination="%d" in arguments_text,
    )


def launch_application(launch: ApplicationLaunch) -> subprocess.Popen[bytes]:
    environment = os.environ.copy()
    environment.update(launch.environment)
    kwargs: dict[str, object] = {
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    return subprocess.Popen(launch.argv, **kwargs)  # type: ignore[arg-type]


def _quote_preview(value: str) -> str:
    if value and _PREVIEW_SAFE.fullmatch(value):
        return value
    return '"' + value.replace('"', '\\"') + '"'


def _split_quoted_fields(text: str) -> tuple[str, ...]:
    lexer = shlex.shlex(text, posix=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    lexer.escape = ""
    return tuple(lexer)
