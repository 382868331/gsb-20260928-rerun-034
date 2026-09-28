"""Turn room demands into a priority ordered equipment plan."""

from __future__ import annotations

from dataclasses import dataclass, field

from .equipment import Equipment
from .rooms import Room


@dataclass(frozen=True)
class Demand:
    room_id: str
    priority: int
    mode: str  # "heat", "cool" or "off"
    need: float  # positive required capacity in the demanded direction


@dataclass(frozen=True)
class Allocation:
    room_id: str
    mode: str
    capacity: float  # positive magnitude actually allocated


@dataclass
class Plan:
    mode: str = "off"  # "heat", "cool" or "off"
    capacity: float = 0.0
    allocations: list[Allocation] = field(default_factory=list)
    degraded_rooms: frozenset[str] = frozenset()
    unsatisfied: tuple[str, ...] = ()

    def requested_on(self) -> bool:
        return self.mode != "off" and self.capacity > 0.0


class Planner:
    """Allocate one shared, mode-exclusive capacity pool.

    Rooms with missing or stale sensors never receive capacity and are
    reported as degraded, giving an explicit conservative state. If heat
    and cool demands conflict, the highest-priority room selects the mode;
    rooms demanding the opposite mode stay unsatisfied. Within the winning
    mode, rooms are served in priority order until the capacity runs out.
    """

    def __init__(self, equipment: Equipment):
        self.equipment = equipment

    def plan(
        self,
        rooms: list[Room],
        temperatures: dict[str, float | None],
    ) -> Plan:
        degraded = frozenset(
            room.room_id for room in rooms if temperatures.get(room.room_id) is None
        )
        demands = self._demands(rooms, temperatures)
        active = [demand for demand in demands if demand.mode in ("heat", "cool")]
        if not active:
            return Plan(degraded_rooms=degraded)

        active.sort(key=lambda demand: (demand.priority, demand.room_id))
        chosen_mode = active[0].mode
        total = (
            self.equipment.heat_capacity
            if chosen_mode == "heat"
            else self.equipment.cool_capacity
        )

        remaining = total
        allocations: list[Allocation] = []
        unsatisfied: list[str] = []
        for demand in active:
            if demand.mode != chosen_mode:
                unsatisfied.append(demand.room_id)
                continue
            capacity = min(demand.need, remaining)
            if capacity > 1e-9:
                allocations.append(Allocation(demand.room_id, chosen_mode, capacity))
                remaining -= capacity
            if capacity < demand.need - 1e-9:
                unsatisfied.append(demand.room_id)

        allocated = sum(item.capacity for item in allocations)
        return Plan(
            mode=chosen_mode if allocated > 0.0 else "off",
            capacity=allocated,
            allocations=allocations,
            degraded_rooms=degraded,
            unsatisfied=tuple(unsatisfied),
        )

    def _demands(
        self,
        rooms: list[Room],
        temperatures: dict[str, float | None],
    ) -> list[Demand]:
        demands: list[Demand] = []
        for room in rooms:
            temperature = temperatures.get(room.room_id)
            mode = room.demand(temperature)
            if mode == "off" or temperature is None:
                continue
            deviation = max(abs(temperature - room.setpoint) - room.deadband, 0.0)
            demands.append(
                Demand(
                    room_id=room.room_id,
                    priority=room.priority,
                    mode=mode,
                    need=deviation * room.gain + 1.0,
                )
            )
        return demands
