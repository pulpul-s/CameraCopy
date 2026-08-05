from __future__ import annotations

import pytest

from cameracopy2.core.transfer_rate import TransferRateEstimator

MIB = 1024 * 1024


def test_short_copy_gets_an_eta_during_startup() -> None:
    estimator = TransferRateEstimator()
    estimator.reset(now=0.0)

    assert estimator.update(4 * MIB, metered=True, now=0.2) == 0.0
    speed = estimator.update(10 * MIB, metered=True, now=0.5)

    assert speed == pytest.approx(20 * MIB)
    remaining_seconds = (100 * MIB - 10 * MIB) / speed
    assert remaining_seconds == pytest.approx(4.5)


def test_visible_speed_refresh_is_limited_to_once_per_second() -> None:
    estimator = TransferRateEstimator()
    estimator.reset(now=0.0)

    initial = estimator.update(50 * MIB, metered=True, now=0.5)
    during_interval = estimator.update(55 * MIB, metered=True, now=1.0)
    refreshed = estimator.update(60 * MIB, metered=True, now=1.5)

    assert initial == pytest.approx(100 * MIB)
    assert during_interval == pytest.approx(initial)
    assert refreshed < initial


def test_inter_file_pause_changes_visible_speed_once_not_per_chunk() -> None:
    estimator = TransferRateEstimator()
    estimator.reset(now=0.0)
    estimator.update(50 * MIB, metered=True, now=0.5)
    estimator.update(150 * MIB, metered=True, now=1.5)

    after_pause = estimator.update(151 * MIB, metered=True, now=3.5)
    rapid_update_one = estimator.update(161 * MIB, metered=True, now=3.6)
    rapid_update_two = estimator.update(201 * MIB, metered=True, now=4.0)
    next_display_update = estimator.update(251 * MIB, metered=True, now=4.5)

    assert 0 < after_pause < 100 * MIB
    assert rapid_update_one == pytest.approx(after_pause)
    assert rapid_update_two == pytest.approx(after_pause)
    assert next_display_update > after_pause


def test_unmetered_progress_does_not_inflate_transfer_speed() -> None:
    estimator = TransferRateEstimator()
    estimator.reset(now=0.0)
    initial = estimator.update(50 * MIB, metered=True, now=0.5)

    estimator.update(500 * MIB, metered=False, now=0.6)
    estimator.update(510 * MIB, metered=True, now=0.7)
    refreshed = estimator.update(600 * MIB, metered=True, now=1.6)

    assert initial == pytest.approx(100 * MIB)
    assert refreshed == pytest.approx(100 * MIB)


def test_sustained_slowdown_changes_estimate_gradually() -> None:
    estimator = TransferRateEstimator()
    estimator.reset(now=0.0)
    initial = estimator.update(50 * MIB, metered=True, now=0.5)

    first_slow_update = estimator.update(90 * MIB, metered=True, now=2.5)
    sustained_slow_update = estimator.update(290 * MIB, metered=True, now=12.5)

    assert initial == pytest.approx(100 * MIB)
    assert 20 * MIB < first_slow_update < initial
    assert 20 * MIB < sustained_slow_update < first_slow_update


def test_progress_regression_resets_estimator() -> None:
    estimator = TransferRateEstimator()
    estimator.reset(now=0.0)
    assert estimator.update(50 * MIB, metered=True, now=0.5) > 0

    assert estimator.update(5 * MIB, metered=True, now=0.6) == 0.0
    assert estimator.update(15 * MIB, metered=True, now=1.1) == pytest.approx(20 * MIB)
