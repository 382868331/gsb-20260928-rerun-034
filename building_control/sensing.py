"""测量值建模与断流判定。"""

from __future__ import annotations

from dataclasses import dataclass

from .model import SensorStatus


@dataclass(frozen=True)
class Measurement:
    room_id: str
    temperature_c: float
    timestamp: float


@dataclass
class _Reading:
    temperature_c: float
    timestamp: float


class SensorRegistry:
    """保存每个房间最近一次测量，并按超时阈值判定断流。

    断流后 view 不会再返回旧温度，避免把旧读数当最新值。
    """

    def __init__(self, stale_after_s: float) -> None:
        if stale_after_s <= 0:
            raise ValueError("stale_after_s 必须为正数")
        self._stale_after_s = stale_after_s
        self._readings: dict[str, _Reading] = {}

    def ingest(self, measurement: Measurement) -> None:
        self._readings[measurement.room_id] = _Reading(
            temperature_c=measurement.temperature_c,
            timestamp=measurement.timestamp,
        )

    def view(self, room_id: str, now: float):
        reading = self._readings.get(room_id)
        if reading is None:
            return SensorStatus.NEVER, None, None
        if now - reading.timestamp > self._stale_after_s:
            return SensorStatus.STALE, None, reading.timestamp
        return SensorStatus.FRESH, reading.temperature_c, reading.timestamp

    def snapshot(self) -> dict[str, list[float]]:
        return {
            room_id: [reading.temperature_c, reading.timestamp]
            for room_id, reading in self._readings.items()
        }

    def restore(self, data: dict[str, list[float]]) -> None:
        self._readings = {
            room_id: _Reading(temperature_c=values[0], timestamp=values[1])
            for room_id, values in data.items()
        }
