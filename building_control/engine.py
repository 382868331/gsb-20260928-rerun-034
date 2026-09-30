"""控制引擎：汇集测量、生成计划、驱动共用设备，并可保存/恢复状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .accounting import EnergyLedger, RoomEnergy
from .model import Mode, RoomConfig, RoomState, SensorStatus
from .planner import Allocation, ControlPlan, Planner, UnmetDemand
from .plant import EquipmentFleet, EquipmentMode, HeatPump
from .schedule import PowerSchedule
from .sensing import Measurement, SensorRegistry


@dataclass(frozen=True)
class TickResult:
    timestamp: float
    plan: ControlPlan
    previous_mode: EquipmentMode
    applied_mode: EquipmentMode
    rooms: tuple[RoomState, ...]
    degraded: bool
    conservative_shutdown: bool
    lockout_remaining_s: float
    delivered_mode: EquipmentMode
    delivered: tuple[Allocation, ...]
    unmet: tuple[UnmetDemand, ...]
    device_modes: tuple[tuple[str, EquipmentMode], ...] = ()
    device_capacities_kw: tuple[tuple[str, float], ...] = ()
    total_power_limit_kw: float = 0.0
    energy: tuple[tuple[str, RoomEnergy], ...] = ()

    def allocation_for(self, room_id: str):
        for allocation in self.delivered:
            if allocation.room_id == room_id:
                return allocation
        return None

    def unmet_for(self, room_id: str):
        for item in self.unmet:
            if item.room_id == room_id:
                return item
        return None


class BuildingEngine:
    def __init__(
        self,
        rooms: list[RoomConfig],
        equipment: HeatPump | EquipmentFleet,
        sensor_stale_after_s: float = 30.0,
        time_fn: Callable[[], float] | None = None,
        power_schedule: PowerSchedule | None = None,
    ) -> None:
        if not rooms:
            raise ValueError("至少需要一个房间")
        ids = [room.room_id for room in rooms]
        if len(ids) != len(set(ids)):
            raise ValueError("房间 ID 必须唯一")
        self.rooms = rooms
        self.equipment = (
            equipment if isinstance(equipment, EquipmentFleet) else EquipmentFleet([equipment])
        )
        self.sensors = SensorRegistry(sensor_stale_after_s)
        self.power_schedule = power_schedule or PowerSchedule()
        self.planner = Planner(self.equipment)
        self.time_fn = time_fn
        self.ledger = EnergyLedger()
        self._last_tick_at: float | None = None
        self._last_delivered: tuple[Allocation, ...] = ()

    def now(self) -> float:
        return self.time_fn() if self.time_fn is not None else 0.0

    def ingest(self, measurement: Measurement) -> None:
        if measurement.room_id not in {room.room_id for room in self.rooms}:
            raise KeyError(f"未知房间：{measurement.room_id}")
        self.sensors.ingest(measurement)

    def _room_states(self, now: float) -> list[RoomState]:
        states: list[RoomState] = []
        for config in self.rooms:
            status, temperature, measured_at = self.sensors.view(config.room_id, now)
            if status is SensorStatus.FRESH and temperature is not None:
                demand = config.demand(temperature)
                requested = config.requested_kw(temperature) if demand is not Mode.OFF else 0.0
            else:
                demand = Mode.UNKNOWN
                requested = 0.0
            states.append(
                RoomState(config, status, temperature, measured_at, demand, requested)
            )
        return states

    def tick(self, now: float | None = None) -> TickResult:
        current = self.now() if now is None else now
        if self._last_tick_at is not None and current < self._last_tick_at:
            raise ValueError("控制时间不能倒退")
        dt_s = 0.0 if self._last_tick_at is None else current - self._last_tick_at

        room_states = self._room_states(current)
        plan = self.planner.plan(
            room_states,
            current,
            heating_cap_kw=self.power_schedule.limit_kw("heat", current),
            cooling_cap_kw=self.power_schedule.limit_kw("cool", current),
        )
        previous_mode = self.equipment.mode
        self._apply_plan(plan, current)
        applied_mode = self.equipment.mode

        delivered_mode = (
            applied_mode if self.equipment.available and applied_mode.running else EquipmentMode.OFF
        )
        delivered, unmet = self._deliver(room_states, plan, delivered_mode, current)
        self.ledger.record(self._last_delivered, dt_s)
        self._last_delivered = delivered
        self._last_tick_at = current

        return TickResult(
            timestamp=current,
            plan=plan,
            previous_mode=previous_mode,
            applied_mode=applied_mode,
            rooms=tuple(room_states),
            degraded=any(room.sensor_status is not SensorStatus.FRESH for room in room_states),
            conservative_shutdown=not any(room.is_known_demand for room in room_states),
            lockout_remaining_s=self.equipment.remaining_lockout_s(current),
            delivered_mode=delivered_mode,
            delivered=delivered,
            unmet=unmet,
            device_modes=tuple((d.device_id, d.mode) for d in self.equipment.devices),
            device_capacities_kw=tuple(
                (d.device_id, d.capacity_kw(delivered_mode))
                for d in self.equipment.active_devices(delivered_mode)
            ),
            total_power_limit_kw=(
                self.power_schedule.limit_kw(delivered_mode.value, current)
                if delivered_mode.running
                else 0.0
            ),
            energy=tuple(sorted(self.ledger.accounts.items())),
        )

    def _apply_plan(self, plan: ControlPlan, now: float) -> None:
        fleet = self.equipment
        target_mode = plan.desired_mode
        if target_mode is EquipmentMode.OFF:
            for device in fleet.devices:
                if device.available and device.mode.running and device.can_turn_off(now):
                    device.apply(EquipmentMode.OFF, now)
            return

        for device in fleet.devices:
            if (
                device.available
                and device.mode.running
                and device.mode is not target_mode
                and device.can_turn_off(now)
            ):
                device.apply(EquipmentMode.OFF, now)

        if any(
            device.available and device.mode.running and device.mode is not target_mode
            for device in fleet.devices
        ):
            return

        power_limit = self.power_schedule.limit_kw(target_mode.value, now)
        requested = sum(
            allocation.allocated_kw
            for allocation in plan.allocations
            if allocation.mode.value == target_mode.value
        )
        needed = min(requested, power_limit)
        active_capacity = fleet.active_capacity_kw(target_mode)
        startable = sorted(fleet.startable_devices(target_mode, now), key=lambda d: d.state_since)
        for device in startable:
            if active_capacity + 1e-9 >= needed:
                break
            device.apply(target_mode, now)
            active_capacity += device.capacity_kw(target_mode)
        self._shutdown_redundant_capacity(fleet, target_mode, active_capacity, needed, now)

    def _shutdown_redundant_capacity(self, fleet, mode, active_capacity, needed, now):
        active = sorted(
            fleet.active_devices(mode),
            key=lambda device: (device.capacity_kw(mode), device.state_since),
        )
        for device in active:
            if active_capacity <= needed + 1e-9:
                break
            capacity = device.capacity_kw(mode)
            if device.can_turn_off(now) and active_capacity - capacity + 1e-9 >= needed:
                device.apply(EquipmentMode.OFF, now)
                active_capacity -= capacity

    def _deliver(self, rooms, plan, delivered_mode, now):
        if not delivered_mode.running:
            return (), tuple(self._unmet_without_delivery(rooms, plan, now))

        winner = Mode(delivered_mode.value)
        device_capacity = self.equipment.active_capacity_kw(delivered_mode)
        external_limit = self.power_schedule.limit_kw(delivered_mode.value, now)
        capacity = min(device_capacity, external_limit)
        delivered, _ = self.planner.allocate(rooms, winner, capacity)
        delivered = self._assign_devices(delivered, delivered_mode)

        unmet: list[UnmetDemand] = []
        delivered_map = {a.room_id: a.allocated_kw for a in delivered}
        for room in sorted(rooms, key=lambda r: r.config.priority):
            if not room.is_known_demand:
                continue
            if room.demand is winner:
                shortfall = room.requested_kw - delivered_map.get(room.room_id, 0.0)
                if shortfall > 1e-9:
                    requested_total = sum(r.requested_kw for r in rooms if r.demand is winner)
                    reason = (
                        "power_limit_exceeded"
                        if external_limit + 1e-9 < requested_total
                        and external_limit + 1e-9 < device_capacity
                        else "capacity_exceeded"
                    )
                    unmet.append(
                        UnmetDemand(room.room_id, room.demand, room.requested_kw, shortfall, reason)
                    )
                continue
            unmet.append(
                UnmetDemand(
                    room.room_id,
                    room.demand,
                    room.requested_kw,
                    room.requested_kw,
                    "mode_conflict",
                )
            )
        return tuple(delivered), tuple(unmet)

    def _assign_devices(self, allocations, mode):
        devices = self.equipment.active_devices(mode)
        remaining = [device.capacity_kw(mode) for device in devices]
        assigned: list[Allocation] = []
        for allocation in allocations:
            amount = allocation.allocated_kw
            chosen = None
            device_shares: list[tuple[str, float]] = []
            for index, device in enumerate(devices):
                share = min(amount, remaining[index])
                if share > 1e-9:
                    remaining[index] -= share
                    amount -= share
                    chosen = chosen or device.device_id
                    device_shares.append((device.device_id, share))
                if amount <= 1e-9:
                    break
            assigned.append(
                Allocation(
                    allocation.room_id,
                    allocation.mode,
                    allocation.requested_kw,
                    allocation.allocated_kw,
                    chosen,
                    tuple(device_shares),
                )
            )
        return tuple(assigned)

    def _unmet_without_delivery(self, rooms, plan, now):
        opposite_running = any(
            device.mode.running and device.mode is not plan.desired_mode
            for device in self.equipment.devices
        )
        all_faulted = all(device.faulted for device in self.equipment.devices)
        if all_faulted:
            reason = "equipment_faulted"
        elif opposite_running:
            reason = "min_on_not_satisfied"
        elif plan.desired_mode.running:
            reason = "min_off_not_satisfied"
        else:
            reason = "not_running"
        return [
            UnmetDemand(room.room_id, room.demand, room.requested_kw, room.requested_kw, reason)
            for room in sorted(rooms, key=lambda r: r.config.priority)
            if room.is_known_demand
        ]

    def save_state(self, path: str | Path) -> None:
        payload = {
            "sensor_stale_after_s": self.sensors._stale_after_s,
            "equipment": self.equipment.snapshot(),
            "readings": self.sensors.snapshot(),
            "power_schedule": self.power_schedule.snapshot(),
            "energy": self.ledger.snapshot(),
            "last_tick_at": self._last_tick_at,
            "last_delivered": [
                {
                    "room_id": item.room_id,
                    "mode": item.mode.value,
                    "requested_kw": item.requested_kw,
                    "allocated_kw": item.allocated_kw,
                    "device_id": item.device_id,
                    "device_allocations": list(item.device_allocations),
                }
                for item in self._last_delivered
            ],
        }
        Path(path).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        equipment_data = payload["equipment"]
        if equipment_data.get("type") == "fleet":
            self.equipment.restore(equipment_data)
        else:
            if len(self.equipment.devices) != 1:
                raise ValueError("旧版单设备快照不能恢复到多设备组")
            self.equipment.devices[0].restore(equipment_data)
        self.sensors.restore(payload.get("readings", {}))
        self.power_schedule = PowerSchedule.restore(payload.get("power_schedule"))
        self.ledger = EnergyLedger.restore(payload.get("energy", {}))
        self._last_tick_at = payload.get("last_tick_at")
        self._last_delivered = tuple(
            Allocation(
                room_id=item["room_id"],
                mode=Mode(item["mode"]),
                requested_kw=float(item["requested_kw"]),
                allocated_kw=float(item["allocated_kw"]),
                device_id=item.get("device_id"),
                device_allocations=tuple(
                    (str(device_id), float(amount))
                    for device_id, amount in item.get("device_allocations", ())
                ),
            )
            for item in payload.get("last_delivered", [])
        )

    @classmethod
    def restore(cls, path, rooms, equipment, time_fn=None, power_schedule=None):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        saved_schedule = PowerSchedule.restore(payload.get("power_schedule"))
        engine = cls(
            rooms=rooms,
            equipment=equipment,
            sensor_stale_after_s=float(payload["sensor_stale_after_s"]),
            time_fn=time_fn,
            power_schedule=power_schedule or saved_schedule,
        )
        engine.load_state(path)
        return engine
