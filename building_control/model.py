"""房间与需求模型。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Mode(Enum):
    """房间的热需求方向。"""

    HEAT = "heat"
    COOL = "cool"
    OFF = "off"
    UNKNOWN = "unknown"


class SensorStatus(Enum):
    FRESH = "fresh"
    STALE = "stale"
    NEVER = "never"


@dataclass(frozen=True)
class RoomConfig:
    room_id: str
    name: str
    priority: int
    setpoint_c: float
    deadband_c: float = 0.5
    rated_input_kw: float = 3.0
    gain_kw_per_c: float = 2.0

    def demand(self, temperature_c: float | None) -> Mode:
        if temperature_c is None:
            return Mode.UNKNOWN
        error = temperature_c - self.setpoint_c
        if error < -self.deadband_c:
            return Mode.HEAT
        if error > self.deadband_c:
            return Mode.COOL
        return Mode.OFF

    def requested_kw(self, temperature_c: float) -> float:
        error = abs(temperature_c - self.setpoint_c)
        return min(self.rated_input_kw, self.gain_kw_per_c * error)


@dataclass(frozen=True)
class RoomState:
    config: RoomConfig
    sensor_status: SensorStatus
    temperature_c: float | None
    measured_at: float | None
    demand: Mode
    requested_kw: float

    @property
    def room_id(self) -> str:
        return self.config.room_id

    @property
    def is_known_demand(self) -> bool:
        return self.demand in (Mode.HEAT, Mode.COOL)
