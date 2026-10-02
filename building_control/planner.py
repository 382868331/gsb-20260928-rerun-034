"""控制计划：多设备冷热模式仲裁 + 有限能力按优先级分配 + 限电预算。"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import Mode, RoomState
from .plant import DeviceView, EquipmentMode


@dataclass(frozen=True)
class Allocation:
    room_id: str
    mode: Mode
    requested_kw: float
    allocated_kw: float


@dataclass(frozen=True)
class UnmetDemand:
    """房间需求未能被真实满足的账目：缺多少功率、为什么。"""

    room_id: str
    mode: Mode
    requested_kw: float
    unmet_kw: float
    reason: str


@dataclass(frozen=True)
class ControlPlan:
    """一个周期的控制意图（不等于实际交付）。

    device_targets 给出每台设备本周期的目标模式；allocations 是按
    设备总能力（不限电）计算的“想供多少”，实际交付见 TickResult。
    """

    desired_mode: EquipmentMode
    allocations: tuple[Allocation, ...] = field(default_factory=tuple)
    notes: tuple[str, ...] = field(default_factory=tuple)
    unmet: tuple[UnmetDemand, ...] = field(default_factory=tuple)
    device_targets: tuple[tuple[str, EquipmentMode], ...] = field(default_factory=tuple)
    power_cap_kw: float = float("inf")

    def allocated_for(self, room_id: str):
        for allocation in self.allocations:
            if allocation.room_id == room_id:
                return allocation
        return None


class Planner:
    """把房间需求分配给多台能力不同的冷热设备。"""

    @staticmethod
    def allocate(rooms, mode, capacity_kw):
        """按优先级在同一模式内分配有限能力，并记录每一项未满足需求。"""

        eligible = sorted(
            (room for room in rooms if room.demand is mode),
            key=lambda room: room.config.priority,
        )
        allocations = []
        unmet = []
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

    def plan(self, rooms, devices, power_cap_kw=float("inf")):
        """逐台设备决定目标模式，再按模式汇总能力做计划分配。

        第一台可用设备服务最高优先级房间的需求模式；后续设备服务
        剩余需求最大的模式（可以是相反模式，实现一台制热一台制冷）。
        """

        demanding = sorted(
            (room for room in rooms if room.is_known_demand),
            key=lambda room: room.config.priority,
        )
        remaining_need = {
            Mode.HEAT: sum(r.requested_kw for r in demanding if r.demand is Mode.HEAT),
            Mode.COOL: sum(r.requested_kw for r in demanding if r.demand is Mode.COOL),
        }
        first_mode = demanding[0].demand if demanding else None

        targets = []
        capacity = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        for device in devices:
            mode = None
            if device.available and first_mode is not None:
                if not any(capacity[m] > 0.0 for m in (Mode.HEAT, Mode.COOL)):
                    mode = first_mode
                elif remaining_need[Mode.HEAT] <= 1e-9 and remaining_need[Mode.COOL] <= 1e-9:
                    mode = None
                else:
                    mode = (
                        Mode.HEAT
                        if remaining_need[Mode.HEAT] >= remaining_need[Mode.COOL]
                        else Mode.COOL
                    )
            if mode is None:
                targets.append((device.device_id, EquipmentMode.OFF))
                continue
            equipment_mode = EquipmentMode(mode.value)
            targets.append((device.device_id, equipment_mode))
            capacity[mode] += device.capacity_kw(equipment_mode)
            remaining_need[mode] = max(
                0.0, remaining_need[mode] - device.capacity_kw(equipment_mode)
            )

        notes = []
        unmet = []
        any_faulted = any(not device.available for device in devices)
        suppressed = [room for room in demanding if capacity[room.demand] <= 1e-9]
        if suppressed and first_mode is not None:
            notes.append(
                f"模式冲突：{first_mode.value} 胜出（最高优先级房间 {demanding[0].room_id}），"
                f"房间 {', '.join(r.room_id for r in suppressed)} 的相反需求本周期不供给"
            )
        for room in suppressed:
            unmet.append(
                UnmetDemand(
                    room_id=room.room_id,
                    mode=room.demand,
                    requested_kw=room.requested_kw,
                    unmet_kw=room.requested_kw,
                    reason="equipment_faulted" if any_faulted else "mode_conflict",
                )
            )
        served_modes = [m for m in (Mode.HEAT, Mode.COOL) if capacity[m] > 0.0]
        if len(served_modes) > 1:
            notes.append(
                "双模式并行："
                + "，".join(
                    f"{device_id}→{mode.value}"
                    for device_id, mode in targets
                    if mode.running
                )
            )

        allocations = []
        for mode in (Mode.HEAT, Mode.COOL):
            if capacity[mode] <= 0.0:
                continue
            mode_allocations, mode_unmet = self.allocate(rooms, mode, capacity[mode])
            allocations.extend(mode_allocations)
            unmet.extend(mode_unmet)

        desired_mode = targets[0][1] if targets else EquipmentMode.OFF
        return ControlPlan(
            desired_mode=desired_mode,
            allocations=tuple(allocations),
            notes=tuple(notes),
            unmet=tuple(unmet),
            device_targets=tuple(targets),
            power_cap_kw=power_cap_kw,
        )

