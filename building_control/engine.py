"""控制引擎：多设备协同、冷热储能缓冲、限电与储能策略，并可保存/恢复状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Callable, Sequence

from .model import Mode, RoomConfig, RoomState, SensorStatus
from .planner import Allocation, ControlPlan, Planner, UnmetDemand
from .plant import EquipmentMode, HeatPump, PowerSchedule
from .sensing import Measurement, SensorRegistry
from .storage import ThermalStorage


class StoragePolicy(Enum):
    """储能调度策略。

    COMFORT_FIRST：舒适优先，设备供能不足时储能立即补足，储备可放空。
    PRESERVE_RESERVE：保留应急储备，储量放到 reserve_fraction 下限为止，
    宁可留下未满足需求也不动用储备。
    """

    COMFORT_FIRST = "comfort_first"
    PRESERVE_RESERVE = "preserve_reserve"


@dataclass(frozen=True)
class DeviceTickInfo:
    """每台设备本周期的真实状态与出力账目。"""

    device_id: str
    previous_mode: EquipmentMode
    applied_mode: EquipmentMode
    faulted: bool
    lockout_remaining_s: float
    delivered_kw: float


@dataclass(frozen=True)
class StorageTickInfo:
    """本周期储能变化账目：充/放功率与期末两仓储量。"""

    mode: EquipmentMode
    charge_kw: float
    discharge_kw: float
    reserve_floor_kwh: float
    heat_kwh: float
    cool_kwh: float
    heat_charge_kw: float = 0.0
    cool_charge_kw: float = 0.0
    heat_discharge_kw: float = 0.0
    cool_discharge_kw: float = 0.0


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
    devices: tuple[DeviceTickInfo, ...] = field(default_factory=tuple)
    storage: StorageTickInfo | None = None
    power_cap_kw: float = float("inf")

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

    def device_info(self, device_id: str):
        for info in self.devices:
            if info.device_id == device_id:
                return info
        return None


class BuildingEngine:
    def __init__(
        self,
        rooms: list[RoomConfig],
        equipment: HeatPump | Sequence[HeatPump],
        sensor_stale_after_s: float = 30.0,
        time_fn: Callable[[], float] | None = None,
        storage: ThermalStorage | None = None,
        power_schedule: PowerSchedule | None = None,
        storage_policy: StoragePolicy = StoragePolicy.COMFORT_FIRST,
        reserve_fraction: float = 0.5,
    ) -> None:
        if not rooms:
            raise ValueError("至少需要一个房间")
        ids = [room.room_id for room in rooms]
        if len(ids) != len(set(ids)):
            raise ValueError("房间 ID 必须唯一")
        devices = list(equipment) if isinstance(equipment, (list, tuple)) else [equipment]
        if not devices:
            raise ValueError("至少需要一台设备")
        device_ids = [device.device_id for device in devices]
        if len(device_ids) != len(set(device_ids)):
            raise ValueError("设备 ID 必须唯一")
        if not 0.0 <= reserve_fraction <= 1.0:
            raise ValueError("reserve_fraction 必须在 [0, 1] 区间")
        self.rooms = rooms
        self.devices = devices
        self.equipment = devices[0]  # 兼容单设备用法
        self.storage = storage
        self.power_schedule = power_schedule
        self.storage_policy = storage_policy
        self.reserve_fraction = reserve_fraction
        self.sensors = SensorRegistry(sensor_stale_after_s)
        self.planner = Planner(devices[0])
        self.time_fn = time_fn
        self._last_tick: float | None = None
        self._ledger: dict[str, object] = {
            "devices_kwh": {device.device_id: 0.0 for device in devices},
            "storage_charge_kwh": 0.0,
            "storage_discharge_kwh": 0.0,
            "unmet_rooms_kwh": {},
        }

    def now(self) -> float:
        return self.time_fn() if self.time_fn is not None else 0.0

    def ingest(self, measurement: Measurement) -> None:
        if measurement.room_id not in {room.room_id for room in self.rooms}:
            raise KeyError(f"未知房间：{measurement.room_id}")
        self.sensors.ingest(measurement)

    def energy_ledger(self) -> dict[str, object]:
        """累计供能账目：各设备供能、储能充放、各房间未满足能量（kWh）。"""
        return json.loads(json.dumps(self._ledger))

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
                RoomState(
                    config=config,
                    sensor_status=status,
                    temperature_c=temperature,
                    measured_at=measured_at,
                    demand=demand,
                    requested_kw=requested,
                )
            )
        return states

    def _reserve_floor_kwh(self, mode: Mode) -> float:
        if self.storage is None or self.storage_policy is not StoragePolicy.PRESERVE_RESERVE:
            return 0.0
        return self.reserve_fraction * self.storage.capacity_kwh(mode)

    def tick(self, now: float | None = None) -> TickResult:
        current = self.now() if now is None else now
        dt_h = 0.0
        if self._last_tick is not None and current > self._last_tick:
            dt_h = (current - self._last_tick) / 3600.0

        room_states = self._room_states(current)
        winner, arbitration_notes = Planner.arbitrate(room_states)
        any_valid_demand = any(room.is_known_demand for room in room_states)
        desired = (
            EquipmentMode(winner.value) if winner in (Mode.HEAT, Mode.COOL) else EquipmentMode.OFF
        )
        demanded_modes = {room.demand for room in room_states if room.is_known_demand}

        # 固定方向设备登记到对应需求；可用双向设备先覆盖不同方向，剩余再给
        # 高优先级方向。两台可用双向设备可分别同时制热/制冷。
        assigned: dict[str, EquipmentMode] = {}
        demanded_priorities = {
            mode: min(
                (room.config.priority for room in room_states if room.demand is mode),
                default=10**9,
            )
            for mode in (Mode.HEAT, Mode.COOL)
        }
        demanded_modes_in_priority_order = sorted(
            demanded_modes, key=lambda mode: demanded_priorities[mode]
        )
        for device in self.devices:
            capable = {
                equipment_mode
                for equipment_mode in (EquipmentMode.HEAT, EquipmentMode.COOL)
                if device.capacity_kw(equipment_mode) > 0
                and equipment_mode.demand() in demanded_modes
            }
            assigned[device.device_id] = (
                next(iter(capable)) if len(capable) == 1 else EquipmentMode.OFF
            )

        bidirectional = sorted(
            (
                device
                for device in self.devices
                if device.available
                and device.capacity_kw(EquipmentMode.HEAT) > 0
                and device.capacity_kw(EquipmentMode.COOL) > 0
            ),
            key=lambda device: (
                -max(
                    device.capacity_kw(EquipmentMode.HEAT),
                    device.capacity_kw(EquipmentMode.COOL),
                ),
                device.device_id,
            ),
        )
        bidirectional_iter = iter(bidirectional)
        for demand_mode in demanded_modes_in_priority_order:
            equipment_mode = EquipmentMode(demand_mode.value)
            if any(target is equipment_mode for target in assigned.values()):
                continue
            try:
                device = next(bidirectional_iter)
            except StopIteration:
                continue
            assigned[device.device_id] = equipment_mode
        if winner in (Mode.HEAT, Mode.COOL):
            for device in bidirectional_iter:
                assigned[device.device_id] = EquipmentMode(winner.value)

        # 计划：按各方向被分配设备的额定能力分配，不考虑等待/故障/限电
        # （计划量 ≠ 实际交付）。没有任何设备被分配到某方向时，该方向
        # 房间保留 0.0 kW 计划记录并写入未满足原因。
        plan_unmet: list[UnmetDemand] = []
        served_allocations: list[Allocation] = []
        suppressed_allocations: list[Allocation] = []
        suppressed_rooms: list[RoomState] = []
        for mode in (Mode.HEAT, Mode.COOL):
            equipment_mode = EquipmentMode(mode.value)
            mode_rooms = [
                room for room in room_states if room.is_known_demand and room.demand is mode
            ]
            if not mode_rooms:
                continue
            planned_capacity = sum(
                device.capacity_kw(equipment_mode)
                for device in self.devices
                if assigned[device.device_id] is equipment_mode
            )
            if planned_capacity > 0:
                allocations, capacity_unmet = Planner.allocate(room_states, mode, planned_capacity)
                served_allocations.extend(allocations)
                plan_unmet.extend(capacity_unmet)
            else:
                capable_any = any(
                    device.capacity_kw(equipment_mode) > 0 for device in self.devices
                )
                reason = "mode_conflict" if capable_any else "no_capability"
                for room in sorted(mode_rooms, key=lambda r: r.config.priority):
                    suppressed_allocations.append(
                        Allocation(room.room_id, mode, room.requested_kw, 0.0)
                    )
                    plan_unmet.append(
                        UnmetDemand(room.room_id, mode, room.requested_kw, room.requested_kw, reason)
                    )
                    suppressed_rooms.append(room)
        notes = arbitration_notes if suppressed_rooms else ()
        plan = ControlPlan(
            desired, tuple(suppressed_allocations) + tuple(served_allocations), notes, tuple(plan_unmet)
        )

        power_cap = self.power_schedule.cap_kw(current) if self.power_schedule else float("inf")

        # 实际交付：设备当刻产出先供房间，富余能力给储能充电，缺口由储能放电补足。
        demanding_rooms = sorted(
            (room for room in room_states if room.is_known_demand),
            key=lambda room: room.config.priority,
        )
        requests_by_mode = {
            mode: sorted(
                (room for room in room_states if room.is_known_demand and room.demand is mode),
                key=lambda room: room.config.priority,
            )
            for mode in (Mode.HEAT, Mode.COOL)
        }
        requested_total_by_mode = {
            mode: sum(room.requested_kw for room in requests_by_mode[mode])
            for mode in (Mode.HEAT, Mode.COOL)
        }

        # 零预算不能产生“有方向、零出力”的设备状态。预算按房间优先级跨设备
        # 分配，避免设备登记顺序影响实际供给。
        remaining_power = max(0.0, power_cap)
        remaining_request = dict(requested_total_by_mode)
        device_budget_kw = {device.device_id: 0.0 for device in self.devices}
        power_limited_modes: set[Mode] = set()
        startable_devices = []
        for device in self.devices:
            target = assigned[device.device_id]
            if (
                target.running
                and device.available
                and (
                    device.mode is target
                    or (device.mode is EquipmentMode.OFF and device.can_start(current))
                )
            ):
                startable_devices.append(device)
        for device in sorted(
            startable_devices,
            key=lambda item: (
                demanded_priorities[Mode(assigned[item.device_id].value)],
                item.device_id,
            ),
        ):
            target = assigned[device.device_id]
            mode = Mode(target.value)
            wanted = min(device.capacity_kw(target), remaining_request[mode])
            budget = min(wanted, remaining_power)
            if wanted > 1e-9 and budget < wanted - 1e-9:
                power_limited_modes.add(mode)
            device_budget_kw[device.device_id] = budget
            remaining_power -= budget
            remaining_request[mode] -= budget
        for device in sorted(startable_devices, key=lambda item: item.device_id):
            target = assigned[device.device_id]
            spare_capacity = device.capacity_kw(target) - device_budget_kw[device.device_id]
            spare_budget = min(spare_capacity, remaining_power)
            device_budget_kw[device.device_id] += spare_budget
            remaining_power -= spare_budget
        # 逐台设备执行模式切换：各自保留最短开/停等待，冷热不能直接互切。
        device_infos: list[DeviceTickInfo] = []
        for device in self.devices:
            previous = device.mode
            target = assigned[device.device_id]
            if (
                device.available
                and target.running
                and device_budget_kw[device.device_id] <= 1e-9
            ):
                target = EquipmentMode.OFF
            if previous.running and previous is not target:
                target = EquipmentMode.OFF
            if device.allow(target, current):
                device.apply(target, current)
            device_infos.append(
                DeviceTickInfo(
                    device_id=device.device_id,
                    previous_mode=previous,
                    applied_mode=device.mode,
                    faulted=device.faulted,
                    lockout_remaining_s=device.remaining_lockout_s(current),
                    delivered_kw=0.0,
                )
            )
        room_device_kw = {room.room_id: 0.0 for room in demanding_rooms}
        device_output = {device.device_id: 0.0 for device in self.devices}
        charge_by_mode = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        discharge_by_mode = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        active_devices = [device for device in self.devices if device.available and device.mode.running]
        device_remaining_budget = {
            device.device_id: device_budget_kw[device.device_id] for device in active_devices
        }
        for room in demanding_rooms:
            for device in sorted(
                (item for item in active_devices if Mode(item.mode.value) is room.demand),
                key=lambda item: (-item.capacity_kw(item.mode), item.device_id),
            ):
                need = room.requested_kw - room_device_kw[room.room_id]
                give = min(need, device_remaining_budget[device.device_id])
                if give <= 1e-9:
                    continue
                room_device_kw[room.room_id] += give
                device_remaining_budget[device.device_id] -= give
                device_output[device.device_id] += give
        if self.storage is not None:
            for device in active_devices:
                leftover = device_remaining_budget[device.device_id]
                if leftover <= 1e-9:
                    continue
                demand_mode = Mode(device.mode.value)
                accepted = self.storage.charge(demand_mode, leftover, dt_h)
                charge_by_mode[demand_mode] += accepted
                device_output[device.device_id] += accepted

        room_storage_kw = {room.room_id: 0.0 for room in demanding_rooms}
        reserve_floors = {
            mode: self._reserve_floor_kwh(mode) for mode in (Mode.HEAT, Mode.COOL)
        }
        if self.storage is not None:
            for mode in (Mode.HEAT, Mode.COOL):
                for room in requests_by_mode[mode]:
                    shortfall = room.requested_kw - room_device_kw[room.room_id]
                    if shortfall <= 1e-9:
                        continue
                    got = self.storage.discharge(mode, shortfall, dt_h, reserve_floors[mode])
                    room_storage_kw[room.room_id] += got
                    discharge_by_mode[mode] += got

        delivered = tuple(
            Allocation(
                room_id=room.room_id,
                mode=room.demand,
                requested_kw=room.requested_kw,
                allocated_kw=room_device_kw[room.room_id] + room_storage_kw[room.room_id],
            )
            for room in demanding_rooms
        )
        delivered_map = {a.room_id: a.allocated_kw for a in delivered}

        unmet = self._unmet(
            room_states, assigned, delivered_map, active_devices,
            reserve_floors, power_limited_modes,
        )

        running_modes = [
            device.mode for device in self.devices if device.mode.running and device.available
        ]
        if desired.running and desired in running_modes:
            delivered_mode = desired
        elif running_modes:
            delivered_mode = running_modes[0]
        else:
            delivered_mode = EquipmentMode.OFF
        device_infos = [
            DeviceTickInfo(
                device_id=info.device_id,
                previous_mode=info.previous_mode,
                applied_mode=info.applied_mode,
                faulted=info.faulted,
                lockout_remaining_s=info.lockout_remaining_s,
                delivered_kw=device_output[info.device_id],
            )
            for info in device_infos
        ]
        storage_info = None
        if self.storage is not None:
            charge_kw = charge_by_mode[Mode.HEAT] + charge_by_mode[Mode.COOL]
            discharge_kw = discharge_by_mode[Mode.HEAT] + discharge_by_mode[Mode.COOL]
            storage_info = StorageTickInfo(
                mode=desired,
                charge_kw=charge_kw,
                discharge_kw=discharge_kw,
                reserve_floor_kwh=reserve_floors[winner] if desired.running else 0.0,
                heat_kwh=self.storage.heat_kwh,
                cool_kwh=self.storage.cool_kwh,
                heat_charge_kw=charge_by_mode[Mode.HEAT],
                cool_charge_kw=charge_by_mode[Mode.COOL],
                heat_discharge_kw=discharge_by_mode[Mode.HEAT],
                cool_discharge_kw=discharge_by_mode[Mode.COOL],
            )

        # 供能账目累计（kWh）。
        ledger = self._ledger
        devices_kwh = ledger["devices_kwh"]
        for device_id, kw in device_output.items():
            devices_kwh[device_id] = devices_kwh.get(device_id, 0.0) + kw * dt_h
        ledger["storage_charge_kwh"] = (
            ledger["storage_charge_kwh"] + sum(charge_by_mode.values()) * dt_h
        )
        ledger["storage_discharge_kwh"] = (
            ledger["storage_discharge_kwh"] + sum(discharge_by_mode.values()) * dt_h
        )
        unmet_rooms = ledger["unmet_rooms_kwh"]
        for item in unmet:
            unmet_rooms[item.room_id] = unmet_rooms.get(item.room_id, 0.0) + item.unmet_kw * dt_h
        self._last_tick = current

        degraded = any(room.sensor_status is not SensorStatus.FRESH for room in room_states)
        return TickResult(
            timestamp=current,
            plan=plan,
            previous_mode=device_infos[0].previous_mode,
            applied_mode=device_infos[0].applied_mode,
            rooms=tuple(room_states),
            degraded=degraded,
            conservative_shutdown=not any_valid_demand,
            lockout_remaining_s=max((info.lockout_remaining_s for info in device_infos), default=0.0),
            delivered_mode=delivered_mode,
            delivered=delivered,
            unmet=unmet,
            devices=tuple(device_infos),
            storage=storage_info,
            power_cap_kw=power_cap,
        )

    def _unmet(
        self,
        rooms: list[RoomState],
        assigned: dict[str, EquipmentMode],
        delivered_map: dict[str, float],
        active_devices: list[HeatPump],
        reserve_floors: dict[Mode, float],
        power_limited_modes: set[Mode],
    ) -> tuple[UnmetDemand, ...]:
        unmet: list[UnmetDemand] = []

        def storage_withheld(mode: Mode) -> bool:
            return (
                self.storage is not None
                and reserve_floors.get(mode, 0.0) > 0.0
                and self.storage.stored_kwh(mode) > 1e-9
            )

        for room in sorted(rooms, key=lambda r: r.config.priority):
            if not room.is_known_demand:
                continue
            shortfall = room.requested_kw - delivered_map.get(room.room_id, 0.0)
            if shortfall <= 1e-9:
                continue
            equipment_mode = EquipmentMode(room.demand.value)
            capable = [
                device for device in self.devices if device.capacity_kw(equipment_mode) > 0
            ]
            serving = [
                device for device in capable if assigned[device.device_id] is equipment_mode
            ]
            active = [
                device
                for device in serving
                if device.available and device.mode is equipment_mode
            ]
            if not capable:
                # 没有任何设备具备该方向能力。
                reason = "no_capability"
            elif not serving:
                # 有设备具备该方向能力，但本周期被仲裁到相反方向。
                reason = "mode_conflict"
            elif not active:
                if all(device.faulted for device in serving):
                    reason = "equipment_faulted"
                elif room.demand in power_limited_modes:
                    reason = "power_limited"
                else:
                    # 受最短开/停保护，本周期不能切换到位。
                    reason = "min_off_not_satisfied"
            elif room.demand in power_limited_modes:
                reason = "power_limited"
            elif storage_withheld(room.demand):
                reason = "storage_reserve_held"
            else:
                reason = "capacity_exceeded"
            unmet.append(
                UnmetDemand(room.room_id, room.demand, room.requested_kw, shortfall, reason)
            )
        return tuple(unmet)

    def save_state(self, path: str | Path) -> None:
        payload = {
            "sensor_stale_after_s": self.sensors._stale_after_s,
            "devices": [device.snapshot() for device in self.devices],
            "equipment": self.devices[0].snapshot(),
            "readings": self.sensors.snapshot(),
            "storage": self.storage.snapshot() if self.storage else None,
            "power_schedule": self.power_schedule.snapshot() if self.power_schedule else None,
            "storage_policy": self.storage_policy.value,
            "reserve_fraction": self.reserve_fraction,
            "ledger": self._ledger,
            "last_tick": self._last_tick,
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_state(self, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        snapshots = payload.get("devices") or [payload["equipment"]]
        snapshots_by_id = {
            str(snapshot.get("device_id")): snapshot for snapshot in snapshots
        }
        for index, device in enumerate(self.devices):
            fallback = snapshots[index] if index < len(snapshots) else None
            snapshot = snapshots_by_id.get(device.device_id, fallback)
            if snapshot is None:
                continue
            device.restore(snapshot)
        self.sensors.restore(payload.get("readings", {}))
        if self.storage is not None and payload.get("storage"):
            self.storage.restore(payload["storage"])
        if payload.get("ledger"):
            self._ledger = payload["ledger"]
        self._last_tick = payload.get("last_tick")

    @classmethod
    def restore(cls, path, rooms, equipment, storage=None, time_fn=None):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        schedule = PowerSchedule.restore(payload.get("power_schedule"))
        engine = cls(
            rooms=rooms,
            equipment=equipment,
            sensor_stale_after_s=float(payload["sensor_stale_after_s"]),
            time_fn=time_fn,
            storage=storage,
            power_schedule=schedule,
            storage_policy=StoragePolicy(payload.get("storage_policy", "comfort_first")),
            reserve_fraction=float(payload.get("reserve_fraction", 0.5)),
        )
        engine.load_state(path)
        return engine
