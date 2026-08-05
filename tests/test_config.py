from __future__ import annotations

from pathlib import Path

import pytest

from cameracopy2 import config as config_module
from cameracopy2.config import (
    UnsupportedConfigError,
    config_from_dict,
    load_config,
    save_config,
    validate_folder_component,
)
from cameracopy2.models import CameraCopyConfig


def test_optional_copy_log_colors_round_trip() -> None:
    config = config_from_dict(
        {
            "version": 2,
            "copyloginformationcolor": "#abcdef",
            "copylogcopiedcolor": None,
            "copylogconfirmedcolor": "#123456",
            "copylogwarningcolor": "#AABBCC",
            "copylogerrorcolor": "#ff0000",
            "copylogbackgroundcolor": "#010203",
        }
    )

    assert config.copyloginformationcolor == "#ABCDEF"
    assert config.copylogcopiedcolor is None
    assert config.copylogconfirmedcolor == "#123456"
    assert config.copylogwarningcolor == "#AABBCC"
    assert config.copylogerrorcolor == "#FF0000"
    assert config.copylogbackgroundcolor == "#010203"


@pytest.mark.parametrize("value", ["red", "#123", "#1234567", 123, True])
def test_invalid_copy_log_color_is_rejected(value: object) -> None:
    with pytest.raises(UnsupportedConfigError):
        config_from_dict({"version": 2, "copylogerrorcolor": value})


def test_clone_mode_preference_is_backward_compatible_and_saved(tmp_path: Path) -> None:
    assert not config_from_dict({"version": 2}).clonemode

    path = tmp_path / "config.json"
    save_config(config_from_dict({"version": 2, "clonemode": True}), path)

    assert load_config(path).clonemode


def test_legacy_clone_skip_setting_is_not_saved(tmp_path: Path) -> None:
    config = config_from_dict({"version": 2, "verifycloneskippedfiles": True})
    path = tmp_path / "config.json"
    save_config(config, path)

    assert "verifycloneskippedfiles" not in path.read_text(encoding="utf-8")


def test_atomic_config_save_preserves_existing_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "cameracopy.json"
    original = '{"existing": true}\n'
    path.write_text(original, encoding="utf-8")

    def fail_replace(_source: Path, _destination: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(config_module.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        save_config(CameraCopyConfig(destination=str(tmp_path / "pictures")), path)

    assert path.read_text(encoding="utf-8") == original
    assert list(tmp_path.glob(f".{path.name}.*.tmp")) == []


def test_application_config_fields_are_backward_compatible() -> None:
    default = config_from_dict({"version": 2})
    configured = config_from_dict(
        {
            "version": 2,
            "applicationname": "RapidRaw",
            "applicationpath": "/usr/bin/rraw",
            "applicationarguments": "--directory %d --fast",
            "applicationenvironment": "__NV_DISABLE_EXPLICIT_SYNC=1",
        }
    )

    assert default.applicationname == ""
    assert default.applicationpath == ""
    assert configured.applicationname == "RapidRaw"
    assert configured.applicationarguments == "--directory %d --fast"


@pytest.mark.parametrize(
    "component",
    ["CON", "con.jpg", "PRN", "AUX.data", "NUL", "COM1", "com9.txt", "LPT1.log"],
)
def test_windows_reserved_folder_names_are_rejected(component: str) -> None:
    assert validate_folder_component(component) == "Folder name is reserved by Windows."


@pytest.mark.parametrize("component", ["photos ", "photos."])
def test_windows_trailing_space_or_period_is_rejected(component: str) -> None:
    assert validate_folder_component(component) == (
        "Folder name must not end with a space or period on Windows."
    )
