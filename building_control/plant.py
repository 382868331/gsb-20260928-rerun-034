"""共用冷暖设备（简化热泵）能力与状态建模。"""

from __future__ import annotations

from dataclasses import dataclass, KW_ONLY
from enum import Enum
from typing import Iterable


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
    _: KW_ONLY
    device_id: str = ""
    heating_capacity_kw: float
    cooling_capacity_kw: float
    min_on_s: float
    min_off_s: float

    def __post_init__(self) -> None:
        if not self.device_id:
            object.__setattr__(self, "device_id", self.name)


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
    def device_id(self) -> str:
        return self.spec.device_id

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
            "device_id": self.device_id,
            "mode": self.mode.value,
            "state_since": self.state_since,
            "available": self.available,
        }

    def restore(self, data: dict[str, object]) -> None:
        if "device_id" in data and str(data["device_id"]) != self.device_id:
            raise ValueError("不能把其他设备的状态恢复到当前设备")
        self.mode = EquipmentMode(str(data["mode"]))
        self.state_since = float(data["state_since"])
        self.available = bool(data.get("available", True))


class EquipmentFleet:
    """多台冷暖设备组成的共用设备组。

    设备组对外仍只有一种供能方向：任一台制热时，不允许另一台同时制冷。
    每台设备分别保留自己的最短开机/停机计时，故障只影响故障设备本身。
    """

    def __init__(self, devices: Iterable[HeatPump]) -> None:
        self.devices = list(devices)
        if not self.devices:
            raise ValueError("设备组至少需要一台设备")
        ids = [device.device_id for device in self.devices]
        if len(ids) != len(set(ids)):
            raise ValueError("设备 ID 必须唯一")

    @property
    def mode(self) -> EquipmentMode:
        running = {device.mode for device in self.devices if device.mode.running}
        if len(running) == 1:
            return next(iter(running))
        return EquipmentMode.OFF

    @property
    def available(self) -> bool:
        return any(device.available for device in self.devices)

    @property
    def faulted(self) -> bool:
        return any(device.faulted for device in self.devices)

    def get(self, device_id: str) -> HeatPump:
        for device in self.devices:
            if device.device_id == device_id:
                return device
        raise KeyError(f"未知设备：{device_id}")

    def set_available(self, available: bool, now: float, device_id: str | None = None) -> None:
        if device_id is None:
            for device in self.devices:
                device.set_available(available, now)
            return
        self.get(device_id).set_available(available, now)

    def active_devices(self, mode: EquipmentMode) -> tuple[HeatPump, ...]:
        return tuple(
            device
            for device in self.devices
            if device.available and device.mode is mode
        )

    def startable_devices(self, mode: EquipmentMode, now: float) -> tuple[HeatPump, ...]:
        return tuple(
            device
            for device in self.devices
            if device.available and device.mode is EquipmentMode.OFF and device.can_start(now)
        )

    def capacity_kw(
        self,
        mode: EquipmentMode,
        now: float | None = None,
        available_only: bool = True,
    ) -> float:
        return sum(
            device.capacity_kw(mode)
            for device in self.devices
            if not available_only or device.available
        )

    def active_capacity_kw(self, mode: EquipmentMode) -> float:
        return sum(device.capacity_kw(mode) for device in self.active_devices(mode))

    def remaining_lockout_s(self, now: float) -> float:
        values = [device.remaining_lockout_s(now) for device in self.devices]
        return max(values) if values else 0.0

    def snapshot(self) -> dict[str, object]:
        return {"type": "fleet", "devices": [device.snapshot() for device in self.devices]}

    def restore(self, data: dict[str, object]) -> None:
        if data.get("type") != "fleet":
            raise ValueError("设备组快照类型不匹配")
        devices = {device.device_id: device for device in self.devices}
        restored: list[HeatPump] = []
        for item in data["devices"]:  # type: ignore[index]
            device = devices[str(item["device_id"])]  # type: ignore[index]
            device.restore(item)  # type: ignore[arg-type]
            restored.append(device)
        if len(restored) != len(self.devices):
            raise ValueError("设备组快照必须包含全部设备")
