"""Sensor measurements with explicit staleness handling.

Once a sensor stream stops, callers see a missing reading rather than the
last received value. Treating an old reading as current is explicitly
unsafe, so freshness is checked on every lookup.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Reading:
    value: float
    timestamp: float


@dataclass
class MeasurementBus:
    timeout_seconds: float = 60.0
    _readings: dict[str, Reading] | None = None

    def __post_init__(self) -> None:
        if self._readings is None:
            self._readings = {}

    def publish(self, sensor_id: str, value: float, timestamp: float) -> None:
        self._readings[sensor_id] = Reading(float(value), float(timestamp))

    def latest(self, sensor_id: str, now: float) -> float | None:
        """Return a fresh value or ``None`` when missing or stale.

        A stale reading is retained for diagnostics but is never returned;
        downstream control treats the room as sensor-degraded.
        """

        reading = self._readings.get(sensor_id)
        if reading is None:
            return None
        if now - reading.timestamp > self.timeout_seconds:
            return None
        return reading.value

    def age(self, sensor_id: str, now: float) -> float | None:
        reading = self._readings.get(sensor_id)
        if reading is None:
            return None
        return now - reading.timestamp

    def is_stale(self, sensor_id: str, now: float) -> bool:
        return self.latest(sensor_id, now) is None and sensor_id in self._readings
