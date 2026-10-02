"""冷热缓冲储能：热量与冷量分别记账，充放均有真实损耗。

设计约束：
- 热量储量与冷量储量是两种不同性质的储能，分开存放、分开计量，
  设备切换冷热用途时二者不得直接相抵。
- 充电按充电效率折算入库，放电按放电效率折损出库，
  损耗凭空消失，不会被任何环节补足。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Mode


@dataclass(frozen=True)
class StorageSpec:
    name: str
    heat_capacity_kwh: float
    cool_capacity_kwh: float
    max_charge_kw: float
    max_discharge_kw: float
    charge_efficiency: float = 0.9
    discharge_efficiency: float = 0.9

    def __post_init__(self) -> None:
        for value in (
            self.heat_capacity_kwh,
            self.cool_capacity_kwh,
            self.max_charge_kw,
            self.max_discharge_kw,
        ):
            if value < 0:
                raise ValueError("储能容量与功率限额不能为负")
        for eff in (self.charge_efficiency, self.discharge_efficiency):
            if not 0.0 < eff <= 1.0:
                raise ValueError("充放效率必须在 (0, 1] 区间")


class ThermalStorage:
    """冷热双仓缓冲储能。"""

    def __init__(self, spec: StorageSpec, heat_kwh: float = 0.0, cool_kwh: float = 0.0) -> None:
        self.spec = spec
        self.heat_kwh = min(max(heat_kwh, 0.0), spec.heat_capacity_kwh)
        self.cool_kwh = min(max(cool_kwh, 0.0), spec.cool_capacity_kwh)

    def capacity_kwh(self, mode: Mode) -> float:
        if mode is Mode.HEAT:
            return self.spec.heat_capacity_kwh
        if mode is Mode.COOL:
            return self.spec.cool_capacity_kwh
        return 0.0

    def stored_kwh(self, mode: Mode) -> float:
        if mode is Mode.HEAT:
            return self.heat_kwh
        if mode is Mode.COOL:
            return self.cool_kwh
        return 0.0

    def _add(self, mode: Mode, delta_kwh: float) -> None:
        if mode is Mode.HEAT:
            self.heat_kwh = min(max(self.heat_kwh + delta_kwh, 0.0), self.spec.heat_capacity_kwh)
        elif mode is Mode.COOL:
            self.cool_kwh = min(max(self.cool_kwh + delta_kwh, 0.0), self.spec.cool_capacity_kwh)

    def charge(self, mode: Mode, offered_kw: float, dt_h: float) -> float:
        """设备富余出力充入储能，返回实际吸收的输入功率 kW。

        入库能量 = 输入功率 * dt * 充电效率，受剩余容量与充电功率限额约束。
        """
        if dt_h <= 0.0 or offered_kw <= 0.0:
            return 0.0
        headroom_kwh = self.capacity_kwh(mode) - self.stored_kwh(mode)
        if headroom_kwh <= 0.0:
            return 0.0
        kw = min(offered_kw, self.spec.max_charge_kw, headroom_kwh / (self.spec.charge_efficiency * dt_h))
        self._add(mode, kw * dt_h * self.spec.charge_efficiency)
        return kw

    def discharge(
        self, mode: Mode, requested_kw: float, dt_h: float, reserve_floor_kwh: float = 0.0
    ) -> float:
        """从储能释放能量，返回实际输出功率 kW。

        出库能量 = 输出功率 * dt / 放电效率；reserve_floor_kwh 以下的
        储量属于应急储备，本轮不放。只释放与 mode 同性质的储量。
        """
        if dt_h <= 0.0 or requested_kw <= 0.0:
            return 0.0
        usable_kwh = max(0.0, self.stored_kwh(mode) - max(reserve_floor_kwh, 0.0))
        if usable_kwh <= 0.0:
            return 0.0
        kw = min(
            requested_kw,
            self.spec.max_discharge_kw,
            usable_kwh * self.spec.discharge_efficiency / dt_h,
        )
        self._add(mode, -kw * dt_h / self.spec.discharge_efficiency)
        return kw

    def snapshot(self) -> dict[str, object]:
        return {
            "name": self.spec.name,
            "heat_kwh": self.heat_kwh,
            "cool_kwh": self.cool_kwh,
        }

    def restore(self, data: dict[str, object]) -> None:
        self.heat_kwh = min(max(float(data.get("heat_kwh", 0.0)), 0.0), self.spec.heat_capacity_kwh)
        self.cool_kwh = min(max(float(data.get("cool_kwh", 0.0)), 0.0), self.spec.cool_capacity_kwh)
