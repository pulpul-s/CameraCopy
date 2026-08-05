from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cameracopy2.config import translate_datetime_format, validate_folder_component
from cameracopy2.models import CameraCopyConfig


def build_destination_directory(config: CameraCopyConfig, timestamp: datetime) -> Path:
    destination_root = Path(config.destination).expanduser()
    date_format = translate_datetime_format(config.datetimestring)
    date_part = timestamp.strftime(date_format) if date_format else ""
    folder_name = f"{config.folderprefix}{date_part}{config.folderpostfix}"
    if not folder_name:
        return destination_root
    validation_error = validate_folder_component(folder_name)
    if validation_error:
        raise ValueError(f"Invalid destination folder name {folder_name!r}: {validation_error}")
    return destination_root / folder_name


def build_destination_file(
    config: CameraCopyConfig, source_file: Path, timestamp: datetime
) -> Path:
    return build_destination_directory(config, timestamp) / source_file.name


def preview_destination(config: CameraCopyConfig, sample_name: str = "DSC0001.ARW") -> str:
    timestamp = datetime.now().replace(microsecond=0)
    return str(build_destination_directory(config, timestamp) / sample_name)
