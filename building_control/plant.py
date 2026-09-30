"""共用冷暖设备（简化热泵）能力与状态建模。"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class EquipmentMode(Enum):
    HEAT = "heat"
    COOL = "cool"
    OFF = "off"

    @property
    def running(self) -> bool:
        return self is not EquipmentMode.OFF


@dataclass(frozen=True)
class HeatPumpSpec:
    name: str
    heating_capacity_kw: float
    cooling_capacity_kw: float
    min_on_s: float
    min_off_s: float


class HeatPump:
    """带最短开/停时间约束的共用设备。

    制热与制冷互为相反模式，切换必须先回到 OFF 并满足最短停止时间，
    不允许 HEAT/COOL 直接互切。
    """

    def __init__(self, spec: HeatPumpSpec, now: float = 0.0) -> None:
        self.spec = spec
        self.mode = EquipmentMode.OFF
        self.state_since = now
        self.available = True

    @property
    def faulted(self) -> bool:
        return not self.available

    def set_available(self, available: bool, now: float) -> None:
        """标记设备可用/临时故障。

        故障视为急停：立即回到 OFF，最短停机计时从急停时刻开始；
        故障恢复不能跳过该等待，也不会重置开关约束。
        """
        available = bool(available)
        if available == self.available:
            return
        self.available = available
        if not available and self.mode is not EquipmentMode.OFF:
            self.mode = EquipmentMode.OFF
            self.state_since = now

    def capacity_kw(self, mode: EquipmentMode) -> float:
        if mode is EquipmentMode.HEAT:
            return self.spec.heating_capacity_kw
        if mode is EquipmentMode.COOL:
            return self.spec.cooling_capacity_kw
        return 0.0

    def _elapsed(self, now: float) -> float:
        return now - self.state_since

    def can_turn_off(self, now: float) -> bool:
        return not self.mode.running or self._elapsed(now) >= self.spec.min_on_s

    def can_start(self, now: float) -> bool:
        return self.mode is EquipmentMode.OFF and self._elapsed(now) >= self.spec.min_off_s

    def allow(self, target: EquipmentMode, now: float) -> bool:
        if not self.available:
            return target is self.mode
        if target is self.mode:
            return True
        if target is EquipmentMode.OFF:
            return self.can_turn_off(now)
        if self.mode is EquipmentMode.OFF:
            return self.can_start(now)
        return False

    def apply(self, target: EquipmentMode, now: float) -> None:
        if not self.allow(target, now):
            raise RuntimeError(
                f"设备 {self.spec.name} 不允许在 {now} 从 {self.mode.value} 切到 {target.value}"
            )
        if target is not self.mode:
            self.mode = target
            self.state_since = now

    def remaining_lockout_s(self, now: float) -> float:
        if self.mode.running:
            return max(0.0, self.spec.min_on_s - self._elapsed(now))
        return max(0.0, self.spec.min_off_s - self._elapsed(now))

    def snapshot(self) -> dict[str, object]:
        return {
            "mode": self.mode.value,
            "state_since": self.state_since,
            "available": self.available,
        }

    def restore(self, data: dict[str, object]) -> None:
        self.mode = EquipmentMode(str(data["mode"]))
        self.state_since = float(data["state_since"])  # type: ignore[arg-type]
        self.available = bool(data.get("available", True))
