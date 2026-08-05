from __future__ import annotations

from typing import Literal

from cameracopy2.models import VolumeInfo, VolumeMatch, VolumeMatchMethod
from cameracopy2.services.volume_service import human_size


SelectionState = Literal["auto_pending", "auto_selected", "user_selected", "user_cleared"]
AUTO_PENDING: SelectionState = "auto_pending"
AUTO_SELECTED: SelectionState = "auto_selected"
USER_SELECTED: SelectionState = "user_selected"
USER_CLEARED: SelectionState = "user_cleared"

MATCH_METHODS: tuple[VolumeMatchMethod, ...] = (
    "device_serial",
    "fs_uuid",
    "label",
    "size",
    "device_path",
    "mount_point",
)

MATCH_METHOD_LABELS: dict[VolumeMatchMethod, str] = {
    "device_serial": "Device serial",
    "fs_uuid": "Filesystem UUID",
    "label": "Label",
    "size": "Size",
    "device_path": "Device path",
    "mount_point": "Mount point",
}


def volume_match_value(volume: VolumeInfo, method: VolumeMatchMethod) -> str | int:
    if method == "device_serial":
        return volume.device_serial or ""
    if method == "fs_uuid":
        return volume.uuid or ""
    if method == "label":
        return volume.label or ""
    if method == "size":
        return volume.size_bytes if volume.size_bytes is not None else ""
    if method == "device_path":
        return volume.hardware_path or volume.device_path or ""
    if method == "mount_point":
        return str(volume.mount_path) if volume.mount_path else ""
    raise ValueError(f"Unsupported volume match method: {method}")


def volume_match_display_value(volume: VolumeInfo | None, method: VolumeMatchMethod) -> str:
    if volume is None:
        return "unavailable"
    value = volume_match_value(volume, method)
    if value == "" or value is None:
        return "unavailable"
    if method == "size" and isinstance(value, int):
        return f"{human_size(value)} ({value} bytes)"
    return str(value)


def volume_match_label(volume: VolumeInfo | None, method: VolumeMatchMethod) -> str:
    return f"{MATCH_METHOD_LABELS[method]} — {volume_match_display_value(volume, method)}"


def build_volume_match(volume: VolumeInfo | None, method: VolumeMatchMethod) -> VolumeMatch:
    if volume is None:
        return VolumeMatch(method=method, value="")
    return VolumeMatch(method=method, value=volume_match_value(volume, method))


def find_volume_by_match(volumes: list[VolumeInfo], match: VolumeMatch | None) -> VolumeInfo | None:
    if match is None or match.value == "" or match.value is None:
        return None
    matches = [
        volume for volume in volumes if volume_match_value(volume, match.method) == match.value
    ]
    return matches[0] if len(matches) == 1 else None


def volume_match_warning(
    volumes: list[VolumeInfo], volume: VolumeInfo | None, method: VolumeMatchMethod
) -> str | None:
    """Return a user-facing warning for unavailable or ambiguous match choices."""
    if volume is None:
        return None
    value = volume_match_value(volume, method)
    label = MATCH_METHOD_LABELS[method]
    if value == "" or value is None:
        return f"{label} is unavailable for {volume.display_name}."
    matches = [
        candidate
        for candidate in volumes
        if volume_match_value(candidate, method) == value
    ]
    if len(matches) > 1:
        return (
            f"{label} value {value} is shared by {len(matches)} mounted volumes and "
            "may not select a unique default."
        )
    return None


def find_same_volume_after_refresh(
    volumes: list[VolumeInfo], previous_volume: VolumeInfo | None
) -> str:
    """Return the refreshed id only for the same current attachment.

    Device path and mount point describe where the selected volume is currently
    attached. On-media identifiers and device serials may be duplicated, so they
    can reject a conflicting candidate but never select one at a new location.
    Partition UUID is intentionally ignored.
    """
    if previous_volume is None:
        return ""

    def normalized(value: object | None) -> str | None:
        if value is None:
            return None
        text = str(value).strip().strip("{}").casefold()
        return text or None

    def metadata_compatible(candidate: VolumeInfo, *, allow_uuid_change: bool) -> bool:
        if (
            previous_volume.size_bytes is not None
            and candidate.size_bytes is not None
            and previous_volume.size_bytes != candidate.size_bytes
        ):
            return False
        attributes = ["device_serial", "hardware_path"]
        if not allow_uuid_change:
            attributes.append("uuid")
        for attribute in attributes:
            previous_value = normalized(getattr(previous_volume, attribute))
            candidate_value = normalized(getattr(candidate, attribute))
            if previous_value and candidate_value and previous_value != candidate_value:
                return False
        return True

    previous_device = normalized(previous_volume.device_path)
    if previous_device:
        device_matches = [
            volume
            for volume in volumes
            if normalized(volume.device_path) == previous_device
            and metadata_compatible(volume, allow_uuid_change=True)
        ]
        if len(device_matches) == 1:
            return device_matches[0].id
        if device_matches:
            return ""

    mount_matches = [
        volume
        for volume in volumes
        if volume.mount_path == previous_volume.mount_path
        and metadata_compatible(volume, allow_uuid_change=False)
    ]
    return mount_matches[0].id if len(mount_matches) == 1 else ""


def configured_default_volume_id(
    volumes: list[VolumeInfo],
    desired_id: str | None,
    match: VolumeMatch | None,
    *,
    excluded_ids: set[str] | None = None,
) -> str:
    """Return the configured default volume id, without guessing a fallback.

    This intentionally does not pick the first available volume. Hotplug refreshes
    should only auto-select a volume when it matches the user's configured default
    rule or legacy saved id.
    """
    excluded_ids = excluded_ids or set()
    matched = find_volume_by_match(volumes, match)
    if matched is not None and matched.id not in excluded_ids:
        return matched.id

    ids = {volume.id for volume in volumes}
    if desired_id and desired_id in ids and desired_id not in excluded_ids:
        return desired_id
    return ""


def resolve_refreshed_volume_ids(
    volumes: list[VolumeInfo],
    previous_primary_id: str,
    previous_secondary_id: str,
    primary_state: SelectionState,
    secondary_state: SelectionState,
    default_primary_id: str | None,
    default_secondary_id: str | None,
    default_primary_match: VolumeMatch | None,
    default_secondary_match: VolumeMatch | None,
    *,
    previous_primary_volume: VolumeInfo | None = None,
    previous_secondary_volume: VolumeInfo | None = None,
) -> tuple[str, str, SelectionState, SelectionState]:
    """Resolve selections after a hotplug/manual volume-list refresh.

    The list may change freely, but selected source volumes should only change
    automatically when a selector is still following its configured default. Manual
    user selections are preserved while present. A deliberately cleared selector
    remains empty.
    """
    ids = {volume.id for volume in volumes}

    def preserved_id(previous_id: str, previous_volume: VolumeInfo | None) -> str:
        if previous_volume is not None:
            return find_same_volume_after_refresh(volumes, previous_volume)
        return previous_id if previous_id in ids else ""

    preserved_primary_id = preserved_id(previous_primary_id, previous_primary_volume)
    preserved_secondary_id = preserved_id(previous_secondary_id, previous_secondary_volume)

    primary_id = ""
    secondary_id = ""

    if primary_state != USER_CLEARED and preserved_primary_id:
        primary_id = preserved_primary_id
    elif primary_state == USER_SELECTED:
        primary_state = AUTO_PENDING

    if secondary_state != USER_CLEARED and preserved_secondary_id:
        secondary_id = preserved_secondary_id
    elif secondary_state == USER_SELECTED:
        secondary_state = AUTO_PENDING

    if primary_id and secondary_id and primary_id == secondary_id:
        # This should not happen in normal UI use, but manual primary wins over
        # automatic secondary to avoid silently replacing the main source.
        if secondary_state != USER_SELECTED or primary_state == USER_SELECTED:
            secondary_id = ""
            if secondary_state == AUTO_SELECTED:
                secondary_state = AUTO_PENDING
        else:
            primary_id = ""
            if primary_state == AUTO_SELECTED:
                primary_state = AUTO_PENDING

    if primary_state in {AUTO_PENDING, AUTO_SELECTED} and not primary_id:
        primary_id = configured_default_volume_id(
            volumes,
            default_primary_id,
            default_primary_match,
            excluded_ids={secondary_id} if secondary_id else set(),
        )
        primary_state = AUTO_SELECTED if primary_id else AUTO_PENDING

    if secondary_state in {AUTO_PENDING, AUTO_SELECTED} and not secondary_id:
        secondary_id = configured_default_volume_id(
            volumes,
            default_secondary_id,
            default_secondary_match,
            excluded_ids={primary_id} if primary_id else set(),
        )
        secondary_state = AUTO_SELECTED if secondary_id else AUTO_PENDING

    if primary_id and secondary_id and primary_id == secondary_id:
        secondary_id = ""
        if secondary_state == AUTO_SELECTED:
            secondary_state = AUTO_PENDING

    return primary_id, secondary_id, primary_state, secondary_state
