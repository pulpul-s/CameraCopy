from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, cast

from .models import (
    CONFIG_VERSION,
    DEFAULT_INCLUDE_PATTERNS,
    CameraCopyConfig,
    CollisionPolicy,
    ThemeMode,
    VolumeMatch,
    VolumeMatchMethod,
)

CONFIG_FILE_NAME = "cameracopy.json"
APP_NAME = "CameraCopy"

_DATE_TOKEN_MAP = (
    ("yyyy", "%Y"),
    ("yyy", "%Y"),
    ("yy", "%y"),
    ("MM", "%m"),
    ("dd", "%d"),
    ("HH", "%H"),
    ("hh", "%I"),
    ("mm", "%M"),
    ("ss", "%S"),
)

_INVALID_FOLDER_CHARS = re.compile(r'[<>:"/\\|?*]')
_WINDOWS_RESERVED_FOLDER_NAMES = re.compile(
    r"^(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$", re.IGNORECASE
)
_ALLOWED_AUTOFORMATS = {None, "FAT32", "exFAT", "NTFS", "ext2", "ext3", "ext4"}
_ALLOWED_COLLISION_POLICIES = {"skip", "overwrite", "rename", "ask"}
_ALLOWED_THEMES = {"system", "light", "dark"}
_HEX_COLOR = re.compile(r"^#[0-9A-Fa-f]{6}$")
_ALLOWED_VOLUME_MATCH_METHODS = {
    "device_serial",
    "fs_uuid",
    "partition_uuid",
    "label",
    "size",
    "device_path",
    "mount_point",
}


class UnsupportedConfigError(ValueError):
    """Raised when a config file is not the current CameraCopy config format."""


class ConfigReadError(OSError):
    """Raised when an existing config file cannot be read safely."""

    def __init__(self, path: Path, error: OSError) -> None:
        super().__init__(f"Could not read settings file {path}: {error}")
        self.path = path
        self.error = error


def default_config_path() -> Path:
    """Return the OS-standard CameraCopy settings path."""
    return _config_path_for_app(APP_NAME)


def _config_path_for_app(app_name: str) -> Path:
    if sys.platform == "win32":
        # Keep the Windows location explicit and stable instead of relying on
        # platformdirs defaults, which can vary between LocalAppData/Roaming and
        # may include an app-author directory.
        appdata = Path.home() / "AppData" / "Roaming"
        if os.environ.get("APPDATA"):
            appdata = Path(os.environ["APPDATA"])
        return appdata / app_name / CONFIG_FILE_NAME

    try:
        from platformdirs import user_config_dir

        config_dir = Path(user_config_dir(app_name, app_name))
    except Exception:
        config_dir = Path.home() / ".config" / app_name
    return config_dir / CONFIG_FILE_NAME


def load_config(path: str | Path = CONFIG_FILE_NAME) -> CameraCopyConfig:
    """Load the current config format or replace unsupported config with defaults."""
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        config = CameraCopyConfig()
        save_config(config, path)
        return config
    except OSError as exc:
        raise ConfigReadError(path, exc) from exc

    try:
        payload = json.loads(text)
        config = config_from_dict(payload)
    except (json.JSONDecodeError, UnsupportedConfigError, TypeError, ValueError):
        _backup_unsupported_config(path)
        config = CameraCopyConfig()
        save_config(config, path)
        return config

    return config


def save_config(config: CameraCopyConfig, path: str | Path = CONFIG_FILE_NAME) -> None:
    """Atomically save the current configuration in UTF-8 JSON format."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(config), indent=2, ensure_ascii=False)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def config_from_dict(data: Any) -> CameraCopyConfig:
    """Parse only the current v2 config schema.

    This intentionally does not support old config files. A config without
    `version: 2`, with unknown fields, or with wrong value types is unsupported and
    should be backed up by `load_config`.
    """
    if not isinstance(data, dict):
        raise UnsupportedConfigError("Config root must be a JSON object.")
    if data.get("version") != CONFIG_VERSION:
        raise UnsupportedConfigError(f"Unsupported config version: {data.get('version')!r}")

    allowed_keys = {field.name for field in fields(CameraCopyConfig)}
    legacy_ignored_keys = {"verifycloneskippedfiles"}
    unknown_keys = set(data) - allowed_keys - legacy_ignored_keys
    if unknown_keys:
        raise UnsupportedConfigError(f"Unknown config field(s): {', '.join(sorted(unknown_keys))}")

    default = CameraCopyConfig()
    return CameraCopyConfig(
        version=CONFIG_VERSION,
        source=normalize_source_subfolder(_read_str(data, "source", default.source)),
        destination=_read_destination(data, default.destination),
        includedfiles=_read_str_list(data, "includedfiles", default.includedfiles),
        excludedfiles=_read_str_list(data, "excludedfiles", default.excludedfiles),
        includeddevices=_read_str_list(data, "includeddevices", default.includeddevices),
        folderprefix=_read_str(data, "folderprefix", default.folderprefix),
        datetimestring=_read_str(data, "datetimestring", default.datetimestring),
        folderpostfix=_read_str(data, "folderpostfix", default.folderpostfix),
        defaultprimaryvolumeid=_read_str(
            data, "defaultprimaryvolumeid", default.defaultprimaryvolumeid
        ),
        defaultsecondaryvolumeid=_read_str(
            data, "defaultsecondaryvolumeid", default.defaultsecondaryvolumeid
        ),
        defaultprimaryvolumematch=_read_volume_match(
            data, "defaultprimaryvolumematch", default.defaultprimaryvolumematch
        ),
        defaultsecondaryvolumematch=_read_volume_match(
            data, "defaultsecondaryvolumematch", default.defaultsecondaryvolumematch
        ),
        minrating=_read_int(data, "minrating", default.minrating, minimum=0, maximum=5),
        useembeddedmetadata=_read_bool(data, "useembeddedmetadata", default.useembeddedmetadata),
        copysidecars=_read_bool(data, "copysidecars", default.copysidecars),
        clonemode=_read_bool(data, "clonemode", default.clonemode),
        autoformat=_read_autoformat(data, default.autoformat),
        autoremove=_read_bool(data, "autoremove", default.autoremove),
        formatprompt=_read_bool(data, "formatprompt", default.formatprompt),
        checkhash=_read_bool(data, "checkhash", default.checkhash),
        durablewrites=_read_bool(data, "durablewrites", default.durablewrites),
        fixsonytimestamps=_read_bool(data, "fixsonytimestamps", default.fixsonytimestamps),
        collisionpolicy=_read_collision_policy(data, default.collisionpolicy),
        applicationname=_read_str(data, "applicationname", default.applicationname),
        applicationpath=_read_str(data, "applicationpath", default.applicationpath),
        applicationarguments=_read_str(
            data, "applicationarguments", default.applicationarguments
        ),
        applicationenvironment=_read_str(
            data, "applicationenvironment", default.applicationenvironment
        ),
        theme=_read_theme(data, default.theme),
        copyloginformationcolor=_read_optional_color(
            data, "copyloginformationcolor", default.copyloginformationcolor
        ),
        copylogcopiedcolor=_read_optional_color(
            data, "copylogcopiedcolor", default.copylogcopiedcolor
        ),
        copylogconfirmedcolor=_read_optional_color(
            data, "copylogconfirmedcolor", default.copylogconfirmedcolor
        ),
        copylogwarningcolor=_read_optional_color(
            data, "copylogwarningcolor", default.copylogwarningcolor
        ),
        copylogerrorcolor=_read_optional_color(
            data, "copylogerrorcolor", default.copylogerrorcolor
        ),
        copylogbackgroundcolor=_read_optional_color(
            data, "copylogbackgroundcolor", default.copylogbackgroundcolor
        ),
    )


def _backup_unsupported_config(path: Path) -> Path | None:
    if not path.exists():
        return None
    backup = _backup_path(path)
    path.replace(backup)
    return backup


def _backup_path(path: Path) -> Path:
    base = path.with_name(path.name + ".bak")
    if not base.exists():
        return base
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}.bak.{index}")
        if not candidate.exists():
            return candidate
    raise OSError(f"Could not find available backup name for {path}")


def _read_str(data: dict[str, Any], key: str, default: str) -> str:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, str):
        raise UnsupportedConfigError(f"{key} must be a string.")
    return value


def _read_destination(data: dict[str, Any], default: str) -> str:
    destination = _read_str(data, "destination", default).strip()
    return destination or str(Path.home() / "Pictures")


def _read_bool(data: dict[str, Any], key: str, default: bool) -> bool:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, bool):
        raise UnsupportedConfigError(f"{key} must be true or false.")
    return value


def _read_int(
    data: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    if key not in data:
        return default
    value = data[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise UnsupportedConfigError(f"{key} must be an integer.")
    if minimum is not None and value < minimum:
        raise UnsupportedConfigError(f"{key} must be at least {minimum}.")
    if maximum is not None and value > maximum:
        raise UnsupportedConfigError(f"{key} must be at most {maximum}.")
    return value


def _read_str_list(data: dict[str, Any], key: str, default: list[str]) -> list[str]:
    if key not in data:
        return list(default)
    value = data[key]
    if not isinstance(value, list):
        raise UnsupportedConfigError(f"{key} must be a list of strings.")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise UnsupportedConfigError(f"{key} must contain only strings.")
        cleaned = item.strip()
        if cleaned:
            result.append(cleaned)
    if key == "includedfiles" and not result:
        return list(DEFAULT_INCLUDE_PATTERNS)
    return result


def _read_volume_match(data: dict[str, Any], key: str, default: VolumeMatch) -> VolumeMatch:
    if key not in data:
        return VolumeMatch(method=default.method, value=default.value)
    value = data[key]
    if not isinstance(value, dict):
        raise UnsupportedConfigError(f"{key} must be an object.")
    unknown_keys = set(value) - {"method", "value"}
    if unknown_keys:
        raise UnsupportedConfigError(
            f"{key} has unknown field(s): {', '.join(sorted(unknown_keys))}."
        )
    method = value.get("method", default.method)
    if not isinstance(method, str) or method not in _ALLOWED_VOLUME_MATCH_METHODS:
        raise UnsupportedConfigError(
            f"{key}.method must be one of {', '.join(sorted(_ALLOWED_VOLUME_MATCH_METHODS))}."
        )
    match_value = value.get("value", default.value)
    if not isinstance(match_value, (str, int)) or isinstance(match_value, bool):
        raise UnsupportedConfigError(f"{key}.value must be a string or integer.")
    if method == "size" and isinstance(match_value, str):
        if match_value.strip() == "":
            match_value = ""
        else:
            try:
                match_value = int(match_value)
            except ValueError as exc:
                raise UnsupportedConfigError(
                    f"{key}.value must be an integer when matching by size."
                ) from exc
    if method != "size" and isinstance(match_value, int):
        match_value = str(match_value)
    if method == "partition_uuid":
        return VolumeMatch()
    return VolumeMatch(method=cast(VolumeMatchMethod, method), value=match_value)


def _read_autoformat(data: dict[str, Any], default: str | None) -> str | None:
    if "autoformat" not in data:
        return default
    value = data["autoformat"]
    if value is not None and not isinstance(value, str):
        raise UnsupportedConfigError("autoformat must be null or a string.")
    if value not in _ALLOWED_AUTOFORMATS:
        raise UnsupportedConfigError(
            "autoformat must be one of null, FAT32, exFAT, NTFS, ext2, ext3, or ext4."
        )
    return value


def _read_collision_policy(data: dict[str, Any], default: CollisionPolicy) -> CollisionPolicy:
    if "collisionpolicy" not in data:
        return default
    value = data["collisionpolicy"]
    if not isinstance(value, str) or value not in _ALLOWED_COLLISION_POLICIES:
        raise UnsupportedConfigError("collisionpolicy must be skip, overwrite, rename, or ask.")
    return cast(CollisionPolicy, value)


def _read_theme(data: dict[str, Any], default: ThemeMode) -> ThemeMode:
    if "theme" not in data:
        return default
    value = data["theme"]
    if not isinstance(value, str) or value not in _ALLOWED_THEMES:
        raise UnsupportedConfigError("theme must be system, light, or dark.")
    return cast(ThemeMode, value)


def _read_optional_color(
    data: dict[str, Any], key: str, default: str | None
) -> str | None:
    if key not in data:
        return default
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str) or not _HEX_COLOR.fullmatch(value):
        raise UnsupportedConfigError(f"{key} must be null or a #RRGGBB color.")
    return value.upper()


def normalize_source_subfolder(source: str) -> str:
    """Return a safe volume-relative source subfolder."""
    text = str(source or "").strip().replace("\\", "/")
    if not text:
        return ""
    if re.match(r"^[A-Za-z]:", text):
        text = text[2:]
    text = text.lstrip("/\\")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    safe_parts = [part for part in parts if part != ".."]
    return "/".join(safe_parts)


def translate_datetime_format(fmt: str) -> str:
    """Translate common PowerShell/.NET date tokens to Python strftime tokens."""
    if not fmt:
        return ""
    translated = fmt
    for dotnet_token, strftime_token in _DATE_TOKEN_MAP:
        translated = translated.replace(dotnet_token, strftime_token)
    return translated


def validate_relative_source(source: str) -> str | None:
    if not source:
        return None
    parts = str(source).replace("\\", "/").split("/")
    if ".." in parts:
        return "Source must not contain parent directory components ('..')."
    return None


def validate_folder_component(component: str) -> str | None:
    if not component:
        return None
    if _INVALID_FOLDER_CHARS.search(component):
        return "Folder name contains characters that are invalid on Windows."
    if component.endswith((" ", ".")):
        return "Folder name must not end with a space or period on Windows."
    base_name = component.split(".", 1)[0]
    if _WINDOWS_RESERVED_FOLDER_NAMES.fullmatch(base_name):
        return "Folder name is reserved by Windows."
    return None
