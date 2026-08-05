from __future__ import annotations

from cameracopy2.services.volume_scan_requests import VolumeScanRequest, prefer_scan_request


def test_scan_request_priority_prefers_manual_over_automatic() -> None:
    poll = VolumeScanRequest("poll", ())
    manual = VolumeScanRequest("manual", ("camera",))

    assert prefer_scan_request(poll, manual) is manual
    assert prefer_scan_request(manual, poll) is manual


def test_scan_request_priority_prefers_pre_copy_over_other_requests() -> None:
    manual = VolumeScanRequest("manual", ("camera",))
    pre_copy = VolumeScanRequest("pre_copy", ("reader",))

    assert prefer_scan_request(manual, pre_copy) is pre_copy
    assert prefer_scan_request(pre_copy, manual) is pre_copy


def test_scan_request_coalescing_keeps_the_newest_equal_priority_request() -> None:
    first = VolumeScanRequest("poll", ("old",))
    latest = VolumeScanRequest("post_format", ("new",))

    assert prefer_scan_request(first, latest) is latest


def test_settings_dialog_scan_is_coalesced_as_automatic_work() -> None:
    poll = VolumeScanRequest("poll", ("old",))
    settings = VolumeScanRequest("settings_dialog", ())

    assert prefer_scan_request(poll, settings) is settings
