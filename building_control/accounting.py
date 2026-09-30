"""房间累计供能账目。"""

from __future__ import annotations

from dataclasses import dataclass

from .planner import Allocation


@dataclass(frozen=True)
class RoomEnergy:
    heating_kwh: float = 0.0
    cooling_kwh: float = 0.0

    @property
    def total_kwh(self) -> float:
        return self.heating_kwh + self.cooling_kwh


class EnergyLedger:
    def __init__(self, accounts: dict[str, RoomEnergy] | None = None) -> None:
        self.accounts = dict(accounts or {})

    def record(self, allocations: list[Allocation] | tuple[Allocation, ...], dt_s: float) -> None:
        hours = dt_s / 3600.0
        for allocation in allocations:
            current = self.accounts.get(allocation.room_id, RoomEnergy())
            heat = current.heating_kwh
            cool = current.cooling_kwh
            energy = allocation.allocated_kw * hours
            if allocation.mode.value == "heat":
                heat += energy
            elif allocation.mode.value == "cool":
                cool += energy
            self.accounts[allocation.room_id] = RoomEnergy(heat, cool)

    def snapshot(self) -> dict[str, dict[str, float]]:
        return {
            room_id: {
                "heating_kwh": account.heating_kwh,
                "cooling_kwh": account.cooling_kwh,
            }
            for room_id, account in self.accounts.items()
        }

    @classmethod
    def restore(cls, data: dict[str, dict[str, float]] | None) -> "EnergyLedger":
        return cls(
            {
                room_id: RoomEnergy(
                    heating_kwh=float(values.get("heating_kwh", 0.0)),
                    cooling_kwh=float(values.get("cooling_kwh", 0.0)),
                )
                for room_id, values in (data or {}).items()
            }
        )
