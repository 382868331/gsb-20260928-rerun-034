"""测量与控制计划：冷热冲突仲裁 + 有限能力按优先级分配。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Mode, RoomState
from .plant import EquipmentMode, HeatPump


@dataclass(frozen=True)
class Allocation:
    room_id: str
    mode: Mode
    requested_kw: float
    allocated_kw: float


@dataclass(frozen=True)
class ControlPlan:
    desired_mode: EquipmentMode
    allocations: tuple[Allocation, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)

    def allocated_for(self, room_id: str):
        for allocation in self.allocations:
            if allocation.room_id == room_id:
                return allocation
        return None


class Planner:
    def __init__(self, equipment: HeatPump) -> None:
        self.equipment = equipment

    @staticmethod
    def arbitrate(rooms: list[RoomState]):
        demanding = sorted(
            (room for room in rooms if room.is_known_demand),
            key=lambda room: room.config.priority,
        )
        if not demanding:
            return Mode.OFF, ()
        winner = demanding[0].demand
        suppressed = tuple(
            room.room_id for room in demanding if room.demand is not winner
        )
        notes = ()
        if suppressed:
            notes = (
                f"模式冲突：{winner.value} 胜出（最高优先级房间 {demanding[0].room_id}），"
                f"房间 {', '.join(suppressed)} 的相反需求本周期不供给",
            )
        return winner, notes

    def plan(self, rooms: list[RoomState]) -> ControlPlan:
        winner, notes = self.arbitrate(rooms)
        if winner is Mode.OFF:
            return ControlPlan(EquipmentMode.OFF, (), notes)
        equipment_mode = EquipmentMode(winner.value)
        capacity = self.equipment.capacity_kw(equipment_mode)

        eligible = sorted(
            (
                room
                for room in rooms
                if room.demand is winner and room.sensor_status.name == "FRESH"
            ),
            key=lambda room: room.config.priority,
        )
        allocations: list[Allocation] = []
        remaining = capacity
        for room in eligible:
            allocated = max(0.0, min(room.requested_kw, remaining))
            remaining -= allocated
            allocations.append(
                Allocation(
                    room_id=room.room_id,
                    mode=winner,
                    requested_kw=room.requested_kw,
                    allocated_kw=allocated,
                )
            )
        return ControlPlan(equipment_mode, tuple(allocations), notes)
