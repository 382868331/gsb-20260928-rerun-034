"""可储存冷热量的缓冲设施（储能罐）建模。

热量与冷量是不同性质的储量，分别存放在独立的罐中：
设备切换冷热用途时不会把两种储量相互抵销。
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Mode


@dataclass(frozen=True)
class StorageFlow:
    """一个周期内储能罐的充放功率与期末储量（对账用）。"""

    charge_heat_kw: float = 0.0
    charge_cool_kw: float = 0.0
    discharge_heat_kw: float = 0.0
    discharge_cool_kw: float = 0.0
    heat_stored_kwh: float = 0.0
    cool_stored_kwh: float = 0.0


class ThermalStorage:
    """冷/热双罐储能设施。

    - 充放分别有效率损耗：充入 x kW 只有 x*charge_efficiency 进入罐中，
      放出 x kW 需要从罐中取出 x/discharge_efficiency，损耗不能凭空补足。
    - 每个周期充放功率受 max_charge_kw / max_discharge_kw 限制，
      且受罐内实际储量/剩余空间限制。
    """

    def __init__(
        self,
        capacity_kwh: float,
        max_charge_kw: float,
        max_discharge_kw: float,
        charge_efficiency: float = 0.95,
        discharge_efficiency: float = 0.95,
        heat_kwh: float = 0.0,
        cool_kwh: float = 0.0,
    ) -> None:
        if capacity_kwh <= 0:
            raise ValueError("储能容量必须为正数")
        if not (0.0 < charge_efficiency <= 1.0) or not (0.0 < discharge_efficiency <= 1.0):
            raise ValueError("充放效率必须在 (0, 1] 区间")
        self.capacity_kwh = float(capacity_kwh)
        self.max_charge_kw = float(max_charge_kw)
        self.max_discharge_kw = float(max_discharge_kw)
        self.charge_efficiency = float(charge_efficiency)
        self.discharge_efficiency = float(discharge_efficiency)
        self.heat_kwh = float(heat_kwh)
        self.cool_kwh = float(cool_kwh)

    def stored_kwh(self, mode: Mode) -> float:
        if mode is Mode.HEAT:
            return self.heat_kwh
        if mode is Mode.COOL:
            return self.cool_kwh
        return 0.0

    def charge(self, mode: Mode, kw: float, dt_s: float) -> float:
        """尝试以 kw 充电 dt_s 秒，返回实际接受的充电功率。"""
        if kw <= 0.0 or dt_s <= 0.0 or mode not in (Mode.HEAT, Mode.COOL):
            return 0.0
        stored = self.stored_kwh(mode)
        headroom_kwh = max(0.0, self.capacity_kwh - stored)
        accepted = min(
            kw,
            self.max_charge_kw,
            headroom_kwh * 3600.0 / (dt_s * self.charge_efficiency),
        )
        accepted = max(0.0, accepted)
        added = accepted * dt_s / 3600.0 * self.charge_efficiency
        if mode is Mode.HEAT:
            self.heat_kwh = stored + added
        else:
            self.cool_kwh = stored + added
        return accepted

    def discharge(self, mode: Mode, kw: float, dt_s: float, reserve_kwh: float = 0.0) -> float:
        """尝试以 kw 放电 dt_s 秒（保留 reserve_kwh 不动），返回实际放电功率。"""
        if kw <= 0.0 or dt_s <= 0.0 or mode not in (Mode.HEAT, Mode.COOL):
            return 0.0
        stored = self.stored_kwh(mode)
        usable_kwh = max(0.0, stored - max(0.0, reserve_kwh))
        delivered_kw = min(
            kw,
            self.max_discharge_kw,
            usable_kwh * self.discharge_efficiency * 3600.0 / dt_s,
        )
        delivered_kw = max(0.0, delivered_kw)
        removed = delivered_kw * dt_s / 3600.0 / self.discharge_efficiency
        if mode is Mode.HEAT:
            self.heat_kwh = stored - removed
        else:
            self.cool_kwh = stored - removed
        return delivered_kw

    def snapshot(self) -> dict[str, float]:
        return {"heat_kwh": self.heat_kwh, "cool_kwh": self.cool_kwh}

    def restore(self, data: dict[str, float]) -> None:
        self.heat_kwh = float(data.get("heat_kwh", 0.0))
        self.cool_kwh = float(data.get("cool_kwh", 0.0))
