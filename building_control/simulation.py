"""简化的本地房间热模型（无外部气象/建筑网络，后续迭代再扩展）。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class LocalRoomModel:
    """集总参数房间：温度向环境漂移，设备供能把温度拉向设定点方向。

    C * dT/dt = (T_amb - T)/R + P_heat - P_cool
    """

    room_id: str
    temperature_c: float
    ambient_c: float = 10.0
    thermal_capacity_kj_per_c: float = 50.0
    thermal_resistance_c_per_kw: float = 4.0

    def step(self, dt_s: float, heat_input_kw: float = 0.0, cool_input_kw: float = 0.0) -> float:
        if heat_input_kw and cool_input_kw:
            raise ValueError("同一房间不能同时制热与制冷")
        dt_h = dt_s / 3600.0
        conductive_kw = (
            (self.ambient_c - self.temperature_c) / self.thermal_resistance_c_per_kw
        )
        net_kw = conductive_kw + heat_input_kw - cool_input_kw
        self.temperature_c += net_kw * dt_h * 3600.0 / self.thermal_capacity_kj_per_c
        return self.temperature_c
