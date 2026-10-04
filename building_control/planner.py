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
    unmet: tuple["UnmetDemand", ...] = field(default_factory=tuple)

    def allocated_for(self, room_id: str):
        for allocation in self.allocations:
            if allocation.room_id == room_id:
                return allocation
        return None


@dataclass(frozen=True)
class UnmetDemand:
    """房间需求未能被真实满足的账目：缺多少功率、为什么。"""

    room_id: str
    mode: Mode
    requested_kw: float
    unmet_kw: float
    reason: str


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

    @staticmethod
    def allocate(
        rooms: list[RoomState], mode: Mode, capacity_kw: float
    ) -> tuple[tuple[Allocation, ...], tuple["UnmetDemand", ...]]:
        """按优先级在同一模式内分配有限能力，并记录每一项未满足需求。"""

        eligible = sorted(
            (room for room in rooms if room.demand is mode),
            key=lambda room: room.config.priority,
        )
        allocations: list[Allocation] = []
        unmet: list[UnmetDemand] = []
        remaining = capacity_kw
        for room in eligible:
            allocated = max(0.0, min(room.requested_kw, remaining))
            remaining -= allocated
            allocations.append(
                Allocation(
                    room_id=room.room_id,
                    mode=mode,
                    requested_kw=room.requested_kw,
                    allocated_kw=allocated,
                )
            )
            shortfall = room.requested_kw - allocated
            if shortfall > 1e-9:
                unmet.append(
                    UnmetDemand(
                        room_id=room.room_id,
                        mode=mode,
                        requested_kw=room.requested_kw,
                        unmet_kw=shortfall,
                        reason="capacity_exceeded",
                    )
                )
        return tuple(allocations), tuple(unmet)

    def plan(self, rooms: list[RoomState]) -> ControlPlan:
        winner, notes = self.arbitrate(rooms)
        unmet: list[UnmetDemand] = []
        suppressed_allocations: list[Allocation] = []
        for room in sorted(rooms, key=lambda r: r.config.priority):
            if not room.is_known_demand:
                continue
            if room.demand is winner:
                continue
            suppressed_allocations.append(
                Allocation(
                    room_id=room.room_id,
                    mode=room.demand,
                    requested_kw=room.requested_kw,
                    allocated_kw=0.0,
                )
            )
            unmet.append(
                UnmetDemand(
                    room_id=room.room_id,
                    mode=room.demand,
                    requested_kw=room.requested_kw,
                    unmet_kw=room.requested_kw,
                    reason="mode_conflict",
                )
            )
        if winner is Mode.OFF:
            return ControlPlan(EquipmentMode.OFF, (), notes, tuple(unmet))
        equipment_mode = EquipmentMode(winner.value)
        capacity = self.equipment.capacity_kw(equipment_mode)
        allocations, capacity_unmet = self.allocate(rooms, winner, capacity)
        return ControlPlan(
            equipment_mode,
            tuple(suppressed_allocations) + allocations,
            notes,
            tuple(unmet) + capacity_unmet,
        )
