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
        any_valid_demand = any(room.is_known_demand for room in room_states)

        demanded_modes = sorted(
            {room.demand for room in room_states if room.is_known_demand},
            key=lambda mode: min(
                room.config.priority for room in room_states if room.demand is mode
            ),
        )

        # 逐台设备按其真实方向能力选择本周期服务方向：
        # 只制冷的设备绝不被指向制热，只制热的设备绝不被指向制冷；
        # 同一方向的设备继续按各自最短开/停等待、冷热不能直接互切。
        targets: dict[str, EquipmentMode] = {}
        previous_modes: dict[str, EquipmentMode] = {}
        live_modes: set[Mode] = set()
        mode_unsupported: set[Mode] = set()
        for mode in demanded_modes:
            capable = [d for d in self.devices if d.capacity_kw(EquipmentMode(mode.value)) > 0.0]
            if not capable:
                mode_unsupported.add(mode)

        for device in self.devices:
            previous = device.mode
            previous_modes[device.device_id] = previous
            if not any_valid_demand:
                target = EquipmentMode.OFF
            elif not device.available:
                target = EquipmentMode.OFF
            else:
                capable_modes = [
                    mode
                    for mode in demanded_modes
                    if device.capacity_kw(EquipmentMode(mode.value)) > 0.0
                ]
                if not capable_modes:
                    target = EquipmentMode.OFF
                elif previous.running:
                    live_demand = previous.demand()
                    if live_demand in capable_modes:
                        target = previous
                    else:
                        # 当前方向不再被需要，或设备本身没有该方向能力：先回 OFF。
                        target = EquipmentMode.OFF
                else:
                    # OFF 设备优先投入仍缺供给的最高优先级方向。
                    target_mode = next(
                        (mode for mode in demanded_modes if mode not in live_modes and mode in capable_modes),
                        capable_modes[0],
                    )
                    target = EquipmentMode(target_mode.value)
            if device.allow(target, current):
                device.apply(target, current)
            targets[device.device_id] = device.mode
            if device.mode.running and device.available:
                live_modes.add(device.mode.demand())

        # 计划：按每个方向可投入设备的额定能力分别分配，
        # 不考虑等待/故障/限电（计划量 ≠ 实际交付）。
        plan_unmet: list[UnmetDemand] = []
        planned_allocations: list[Allocation] = []
        notes_list: list[str] = []
        for mode in demanded_modes:
            if mode in mode_unsupported:
                for room in sorted(room_states, key=lambda r: r.config.priority):
                    if room.demand is mode:
                        planned_allocations.append(Allocation(room.room_id, mode, room.requested_kw, 0.0))
                        plan_unmet.append(
                            UnmetDemand(room.room_id, mode, room.requested_kw, room.requested_kw, "mode_unsupported")
                        )
                continue
            fleet_capacity = sum(
                device.capacity_kw(EquipmentMode(mode.value))
                for device in self.devices
                if device.capacity_kw(EquipmentMode(mode.value)) > 0.0
            )
            allocations, capacity_unmet = Planner.allocate(room_states, mode, fleet_capacity)
            planned_allocations.extend(allocations)
            plan_unmet.extend(capacity_unmet)
        for mode in demanded_modes:
            if mode in mode_unsupported:
                continue
            capable_ids = {
                d.device_id
                for d in self.devices
                if d.capacity_kw(EquipmentMode(mode.value)) > 0.0
            }
            other_modes = [other for other in demanded_modes if other is not mode]
            committed_away = {
                d.device_id
                for d in self.devices
                if d.device_id in capable_ids
                and targets.get(d.device_id, EquipmentMode.OFF).running
                and targets[d.device_id].demand() in other_modes
            }
            if committed_away == capable_ids and other_modes:
                winner_mode = other_modes[0]
                suppressed = [room.room_id for room in room_states if room.demand is mode]
                notes_list.append(
                    f"模式冲突：{winner_mode.value} 占用全部具备 {mode.value} 能力的设备，"
                    f"房间 {', '.join(suppressed)} 的{mode.value}需求本周期不供给"
                )
        primary_mode = demanded_modes[0] if demanded_modes else Mode.OFF
        desired = (
            EquipmentMode(primary_mode.value)
            if primary_mode in (Mode.HEAT, Mode.COOL)
            else EquipmentMode.OFF
        )
        notes = tuple(notes_list)
        plan = ControlPlan(desired, tuple(planned_allocations), notes, tuple(plan_unmet))

        device_infos: list[DeviceTickInfo] = []
        for device in self.devices:
            device_infos.append(
                DeviceTickInfo(
                    device_id=device.device_id,
                    previous_mode=previous_modes[device.device_id],
                    applied_mode=device.mode,
                    faulted=device.faulted,
                    lockout_remaining_s=device.remaining_lockout_s(current),
                    delivered_kw=0.0,
                )
            )

        power_cap = self.power_schedule.cap_kw(current) if self.power_schedule else float("inf")

        # 实际交付：按方向把设备分组，站点总限额在所有运行设备之间共享，
        # 房间按全局优先级跨方向排队；各方向设备当刻产出先供房间，
        # 富余给对应储能仓充电，缺口由同性质储能仓放电补足。
        demanding_rooms = sorted(
            (room for room in room_states if room.is_known_demand),
            key=lambda room: room.config.priority,
        )
        room_device_kw = {room.room_id: 0.0 for room in demanding_rooms}
        device_output = {device.device_id: 0.0 for device in self.devices}
        charge_kw_by_mode = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        discharge_kw_by_mode = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        active_devices_by_mode: dict[Mode, list[HeatPump]] = {}
        reserve_floor_by_mode: dict[Mode, float] = {}
        for mode in demanded_modes:
            active = [
                device
                for device in self.devices
                if device.available and device.mode.running and device.mode.demand() is mode
            ]
            active_devices_by_mode[mode] = active
            reserve_floor_by_mode[mode] = self._reserve_floor_kwh(mode)

        # 房间按全局优先级跨方向排队：每个房间只从本方向设备取能，
        # 所有取能共享同一个站点功率池，绝不超过单机能力或站点限额。
        device_used = {device.device_id: 0.0 for device in self.devices}
        total_used = 0.0
        for room in demanding_rooms:
            mode = room.demand
            for device in active_devices_by_mode.get(mode, []):
                need = room.requested_kw - room_device_kw[room.room_id]
                if need <= 1e-9:
                    break
                device_headroom = device.capacity_kw(device.mode) - device_used[device.device_id]
                site_headroom = power_cap - total_used
                give = min(need, device_headroom, site_headroom)
                if give <= 1e-9:
                    continue
                room_device_kw[room.room_id] += give
                device_used[device.device_id] += give
                total_used += give

        # 全部房间供完后的富余出力才给同方向储能仓充电（仍受站点限额约束）。
        if self.storage is not None:
            for mode in demanded_modes:
                for device in active_devices_by_mode.get(mode, []):
                    device_headroom = device.capacity_kw(device.mode) - device_used[device.device_id]
                    site_headroom = power_cap - total_used
                    offer = min(device_headroom, site_headroom)
                    if offer <= 1e-9:
                        continue
                    accepted = self.storage.charge(mode, offer, dt_h)
                    charge_kw_by_mode[mode] += accepted
                    device_used[device.device_id] += accepted
                    total_used += accepted
        for device in self.devices:
            device_output[device.device_id] = device_used[device.device_id]

        # 各方向设备产出后的缺口，只由同性质储能仓补足。
        room_storage_kw = {room.room_id: 0.0 for room in demanding_rooms}
        if self.storage is not None:
            for mode in demanded_modes:
                for room in sorted(
                    (room for room in demanding_rooms if room.demand is mode),
                    key=lambda room: room.config.priority,
                ):
                    shortfall = room.requested_kw - room_device_kw[room.room_id]
                    if shortfall <= 1e-9:
                        continue
                    got = self.storage.discharge(
                        mode, shortfall, dt_h, reserve_floor_by_mode.get(mode, 0.0)
                    )
                    room_storage_kw[room.room_id] += got
                    discharge_kw_by_mode[mode] += got

        charge_kw = charge_kw_by_mode[Mode.HEAT] + charge_kw_by_mode[Mode.COOL]
        discharge_kw = discharge_kw_by_mode[Mode.HEAT] + discharge_kw_by_mode[Mode.COOL]

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
            room_states, demanded_modes, targets,
            active_devices_by_mode, mode_unsupported,
            delivered_map, device_output, power_cap, reserve_floor_by_mode,
        )

        running_modes = {info.applied_mode.demand() for info in device_infos if info.applied_mode.running and not info.faulted}
        delivered_mode = (
            EquipmentMode(demanded_modes[0].value)
            if demanded_modes and demanded_modes[0] in running_modes
            else (EquipmentMode(next(iter(running_modes)).value) if running_modes else EquipmentMode.OFF)
        )
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
            storage_info = StorageTickInfo(
                mode=desired,
                charge_kw=charge_kw,
                discharge_kw=discharge_kw,
                reserve_floor_kwh=max(reserve_floor_by_mode.values(), default=0.0),
                heat_kwh=self.storage.heat_kwh,
                cool_kwh=self.storage.cool_kwh,
                heat_charge_kw=charge_kw_by_mode[Mode.HEAT],
                cool_charge_kw=charge_kw_by_mode[Mode.COOL],
                heat_discharge_kw=discharge_kw_by_mode[Mode.HEAT],
                cool_discharge_kw=discharge_kw_by_mode[Mode.COOL],
            )

        # 供能账目累计（kWh）。
        ledger = self._ledger
        devices_kwh = ledger["devices_kwh"]
        for device_id, kw in device_output.items():
            devices_kwh[device_id] = devices_kwh.get(device_id, 0.0) + kw * dt_h
        ledger["storage_charge_kwh"] = ledger["storage_charge_kwh"] + charge_kw * dt_h
        ledger["storage_discharge_kwh"] = ledger["storage_discharge_kwh"] + discharge_kw * dt_h
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
        demanded_modes: list[Mode],
        targets: dict[str, EquipmentMode],
        active_devices_by_mode: dict[Mode, list[HeatPump]],
        mode_unsupported: set[Mode],
        delivered_map: dict[str, float],
        device_output: dict[str, float],
        power_cap_kw: float,
        reserve_floor_by_mode: dict[Mode, float],
    ) -> tuple[UnmetDemand, ...]:
        unmet: list[UnmetDemand] = []
        total_output = sum(device_output.values())
        site_budget_exhausted = total_output >= power_cap_kw - 1e-6
        running_capacity_total = sum(
            d.capacity_kw(d.mode)
            for d in self.devices
            if d.available and d.mode.running
        )
        total_requested = sum(
            room.requested_kw for room in rooms if room.is_known_demand
        )
        for room in sorted(rooms, key=lambda r: r.config.priority):
            if not room.is_known_demand:
                continue
            mode = room.demand
            equip_mode = EquipmentMode(mode.value)
            shortfall = room.requested_kw - delivered_map.get(room.room_id, 0.0)
            if shortfall <= 1e-9:
                continue
            capable_healthy = [
                d for d in self.devices
                if d.capacity_kw(equip_mode) > 0.0 and d.available
            ]
            active_for_mode = active_devices_by_mode.get(mode, [])
            total_active_capacity = sum(d.capacity_kw(equip_mode) for d in active_for_mode)
            power_limited = (
                bool(active_for_mode)
                and site_budget_exhausted
                and (
                    power_cap_kw < running_capacity_total - 1e-9
                    or total_requested > power_cap_kw + 1e-9
                )
            )
            storage_withheld = (
                self.storage is not None
                and reserve_floor_by_mode.get(mode, 0.0) > 0.0
                and self.storage.stored_kwh(mode) > 1e-9
            )
            if mode in mode_unsupported:
                # 机群没有任何设备具备该方向能力（制冷机绝不可能制热）。
                reason = "mode_unsupported"
            elif not capable_healthy:
                if any(d.faulted and d.capacity_kw(equip_mode) > 0.0 for d in self.devices):
                    reason = "equipment_faulted"
                else:
                    reason = "mode_unsupported"
            elif not active_for_mode:
                running_other = [
                    d for d in capable_healthy
                    if targets.get(d.device_id, EquipmentMode.OFF).running
                    and targets[d.device_id].demand() is not mode
                ]
                if len(running_other) == len(capable_healthy) and running_other:
                    # 具备能力的设备正在服务相反方向且被最短开机保护锁住。
                    reason = "mode_conflict"
                else:
                    # 有能力也没被相反方向占用，但最短开/停等待未结束，本周期不能切换。
                    reason = "min_off_not_satisfied"
            elif power_limited:
                reason = "power_limited"
            elif storage_withheld:
                reason = "storage_reserve_held"
            else:
                reason = "capacity_exceeded"
            unmet.append(
                UnmetDemand(room.room_id, mode, room.requested_kw, shortfall, reason)
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
        for device, snapshot in zip(self.devices, snapshots):
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
