from __future__ import annotations

import math
import time
from dataclasses import dataclass, field


DEFAULT_SMOOTHING_SECONDS = 10.0
DEFAULT_DISPLAY_INTERVAL_SECONDS = 1.0
DEFAULT_STARTUP_SAMPLE_SECONDS = 0.5


@dataclass(slots=True)
class TransferRateEstimator:
    """Estimate transfer speed without exposing chunk-level jitter to the UI."""

    smoothing_seconds: float = DEFAULT_SMOOTHING_SECONDS
    display_interval_seconds: float = DEFAULT_DISPLAY_INTERVAL_SECONDS
    startup_sample_seconds: float = DEFAULT_STARTUP_SAMPLE_SECONDS
    _baseline_time: float = field(init=False, default=0.0)
    _baseline_bytes: int = field(init=False, default=0)
    _last_observation_time: float = field(init=False, default=0.0)
    _last_observation_bytes: int = field(init=False, default=0)
    _smoothed_speed: float | None = field(init=False, default=None)
    _visible_speed: float | None = field(init=False, default=None)
    _last_display_time: float | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.smoothing_seconds <= 0:
            raise ValueError("smoothing_seconds must be positive")
        if self.display_interval_seconds <= 0:
            raise ValueError("display_interval_seconds must be positive")
        if self.startup_sample_seconds < 0:
            raise ValueError("startup_sample_seconds cannot be negative")
        self.reset()

    def reset(self, bytes_done: int = 0, *, now: float | None = None) -> None:
        timestamp = time.perf_counter() if now is None else now
        safe_bytes = max(0, int(bytes_done))
        self._baseline_time = timestamp
        self._baseline_bytes = safe_bytes
        self._last_observation_time = timestamp
        self._last_observation_bytes = safe_bytes
        self._smoothed_speed = None
        self._visible_speed = None
        self._last_display_time = None

    def update(
        self,
        bytes_done: int,
        *,
        metered: bool,
        now: float | None = None,
        force_display: bool = False,
    ) -> float:
        """Record progress and return the speed currently suitable for display."""

        timestamp = time.perf_counter() if now is None else now
        safe_bytes = max(0, int(bytes_done))

        if safe_bytes < self._last_observation_bytes:
            self.reset(safe_bytes, now=timestamp)
            return 0.0

        if metered:
            self._record_metered_progress(safe_bytes, timestamp)
        elif safe_bytes != self._last_observation_bytes:
            self._rebase_unmetered_progress(safe_bytes, timestamp)

        self._refresh_visible_speed(timestamp, force=force_display)
        return self._visible_speed or 0.0

    def _record_metered_progress(self, bytes_done: int, now: float) -> None:
        transferred = bytes_done - self._last_observation_bytes
        elapsed = now - self._last_observation_time
        if transferred <= 0 or elapsed <= 0:
            return

        if self._smoothed_speed is None:
            startup_elapsed = now - self._baseline_time
            startup_bytes = bytes_done - self._baseline_bytes
            if startup_elapsed >= self.startup_sample_seconds and startup_bytes > 0:
                self._smoothed_speed = startup_bytes / startup_elapsed
        else:
            interval_speed = transferred / elapsed
            smoothing_weight = 1.0 - math.exp(-elapsed / self.smoothing_seconds)
            self._smoothed_speed += smoothing_weight * (
                interval_speed - self._smoothed_speed
            )

        self._last_observation_time = now
        self._last_observation_bytes = bytes_done

    def _rebase_unmetered_progress(self, bytes_done: int, now: float) -> None:
        self._last_observation_time = now
        self._last_observation_bytes = bytes_done
        if self._smoothed_speed is None:
            self._baseline_time = now
            self._baseline_bytes = bytes_done

    def _refresh_visible_speed(self, now: float, *, force: bool) -> None:
        if self._smoothed_speed is None:
            return
        if self._visible_speed is None:
            self._visible_speed = self._smoothed_speed
            self._last_display_time = now
            return
        if force or self._last_display_time is None:
            self._visible_speed = self._smoothed_speed
            self._last_display_time = now
            return
        if now - self._last_display_time >= self.display_interval_seconds:
            self._visible_speed = self._smoothed_speed
            self._last_display_time = now
