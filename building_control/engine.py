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
    target_mode: EquipmentMode = EquipmentMode.OFF


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

    def _assign_device_modes(
        self, room_states: list[RoomState], include_faulted: bool = False
    ) -> dict[str, EquipmentMode]:
        """给每台可用设备分配本周期目标模式（机群可同时覆盖冷热两个方向）。

        已在某需求方向运行的设备保持原方向，减少不必要切换；其余设备按
        各方向未覆盖缺口从大到小贪心补位，并列时优先级数值小的方向先、
        再按制热优先，保证结果确定。故障设备不参与分配（保持 OFF）；
        include_faulted=True 用于推演"若无故障"的假设分配，解释未满足原因。
        """
        targets = {device.device_id: EquipmentMode.OFF for device in self.devices}
        demands: dict[Mode, dict[str, float]] = {}
        for mode in (Mode.HEAT, Mode.COOL):
            rooms_in_mode = [room for room in room_states if room.demand is mode]
            if rooms_in_mode:
                demands[mode] = {
                    "requested": sum(room.requested_kw for room in rooms_in_mode),
                    "priority": min(room.config.priority for room in rooms_in_mode),
                }
        if not demands:
            return targets
        remaining = {mode: info["requested"] for mode, info in demands.items()}
        available = [
            device for device in self.devices if include_faulted or device.available
        ]
        for device in available:
            if device.mode.running:
                mode = device.mode.demand()
                if mode in remaining and remaining[mode] > 1e-9:
                    targets[device.device_id] = device.mode
                    remaining[mode] -= device.capacity_kw(device.mode)
        for device in available:
            if targets[device.device_id].running:
                continue
            candidates = [
                mode for mode in (Mode.HEAT, Mode.COOL) if remaining.get(mode, 0.0) > 1e-9
            ]
            if not candidates:
                break
            mode = max(
                candidates,
                key=lambda m: (remaining[m], -demands[m]["priority"], m is Mode.HEAT),
            )
            equipment_mode = EquipmentMode(mode.value)
            targets[device.device_id] = equipment_mode
            remaining[mode] -= device.capacity_kw(equipment_mode)
        return targets

    def tick(self, now: float | None = None) -> TickResult:
        current = self.now() if now is None else now
        dt_h = 0.0
        if self._last_tick is not None and current > self._last_tick:
            dt_h = (current - self._last_tick) / 3600.0

        room_states = self._room_states(current)
        winner, _ = Planner.arbitrate(room_states)
        any_valid_demand = any(room.is_known_demand for room in room_states)
        desired = (
            EquipmentMode(winner.value) if winner in (Mode.HEAT, Mode.COOL) else EquipmentMode.OFF
        )

        # 设备模式分配：每台设备一个目标方向，机群可同时覆盖冷与热。
        targets = self._assign_device_modes(room_states)
        if not any_valid_demand:
            targets = {device_id: EquipmentMode.OFF for device_id in targets}

        demanding = sorted(
            (room for room in room_states if room.is_known_demand),
            key=lambda room: room.config.priority,
        )
        unserved = [
            room
            for room in demanding
            if not any(
                target is EquipmentMode(room.demand.value) for target in targets.values()
            )
        ]
        notes: tuple[str, ...] = ()
        if unserved and desired.running:
            notes = (
                f"模式冲突：{winner.value} 方向优先（最高优先级房间 {demanding[0].room_id}），"
                f"房间 {', '.join(room.room_id for room in unserved)} 的相反需求本周期无设备供给",
            )

        # 计划：冷热两方向分别按所分配设备的额定能力分配，不考虑等待/故障/限电
        # （计划量 ≠ 实际交付）；没有任何设备覆盖的方向记 mode_conflict，不删分区。
        plan_unmet: list[UnmetDemand] = []
        planned_allocations: list[Allocation] = []
        for mode in (Mode.HEAT, Mode.COOL):
            equipment_mode = EquipmentMode(mode.value)
            rooms_in_mode = [room for room in room_states if room.demand is mode]
            if not rooms_in_mode:
                continue
            mode_capacity = sum(
                device.capacity_kw(equipment_mode)
                for device in self.devices
                if targets[device.device_id] is equipment_mode
            )
            if mode_capacity <= 1e-9:
                for room in sorted(rooms_in_mode, key=lambda r: r.config.priority):
                    plan_unmet.append(
                        UnmetDemand(
                            room.room_id, mode, room.requested_kw, room.requested_kw,
                            "mode_conflict",
                        )
                    )
                continue
            allocations, capacity_unmet = Planner.allocate(room_states, mode, mode_capacity)
            planned_allocations.extend(allocations)
            plan_unmet.extend(capacity_unmet)
        plan = ControlPlan(desired, tuple(planned_allocations), notes, tuple(plan_unmet))

        # 逐台设备执行模式切换：各自保留最短开/停等待，冷热不能直接互切。
        device_infos: list[DeviceTickInfo] = []
        for device in self.devices:
            previous = device.mode
            target = targets[device.device_id]
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
                    target_mode=targets[device.device_id],
                )
            )

        power_cap = self.power_schedule.cap_kw(current) if self.power_schedule else float("inf")

        # 实际交付：设备当刻产出先供本方向房间，富余能力给对应储能仓充电，
        # 缺口由对应储能仓放电补足；冷热两方向分别记账，站点限电约束总出力。
        requests_by_mode = {
            mode: sorted(
                (room for room in room_states if room.demand is mode),
                key=lambda room: room.config.priority,
            )
            for mode in (Mode.HEAT, Mode.COOL)
        }
        room_device_kw = {
            room.room_id: 0.0 for rooms in requests_by_mode.values() for room in rooms
        }
        device_output = {device.device_id: 0.0 for device in self.devices}
        charge_by_mode = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        discharge_by_mode = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        active_by_mode = {Mode.HEAT: [], Mode.COOL: []}
        for device in self.devices:
            if device.available and device.mode.running:
                active_by_mode[device.mode.demand()].append(device)
        remaining_power = power_cap
        for device in self.devices:
            if not (device.available and device.mode.running):
                continue
            mode = device.mode.demand()
            budget = max(0.0, min(device.capacity_kw(device.mode), remaining_power))
            remaining_power -= budget
            used = 0.0
            for room in requests_by_mode[mode]:
                need = room.requested_kw - room_device_kw[room.room_id]
                if need <= 1e-9:
                    continue
                give = min(need, budget - used)
                if give <= 0.0:
                    break
                room_device_kw[room.room_id] += give
                used += give
            if self.storage is not None and budget - used > 1e-9:
                accepted = self.storage.charge(mode, budget - used, dt_h)
                charge_by_mode[mode] += accepted
                used += accepted
            device_output[device.device_id] = used

        room_storage_kw = dict(room_device_kw)
        for room_id in room_storage_kw:
            room_storage_kw[room_id] = 0.0
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
                mode=mode,
                requested_kw=room.requested_kw,
                allocated_kw=room_device_kw[room.room_id] + room_storage_kw[room.room_id],
            )
            for mode in (Mode.HEAT, Mode.COOL)
            if active_by_mode[mode]
            for room in requests_by_mode[mode]
        )
        delivered_map = {a.room_id: a.allocated_kw for a in delivered}

        unmet = self._unmet(
            room_states, targets, delivered_map, active_by_mode,
            device_output, power_cap, reserve_floors,
        )

        running_modes = [
            device.mode
            for device in self.devices
            if device.mode.running and not device.faulted
        ]
        if desired in running_modes:
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
                target_mode=info.target_mode,
            )
            for info in device_infos
        ]
        storage_info = None
        if self.storage is not None:
            storage_info = StorageTickInfo(
                mode=desired,
                charge_kw=charge_by_mode[Mode.HEAT] + charge_by_mode[Mode.COOL],
                discharge_kw=discharge_by_mode[Mode.HEAT] + discharge_by_mode[Mode.COOL],
                reserve_floor_kwh=reserve_floors[winner] if winner in (Mode.HEAT, Mode.COOL) else 0.0,
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
        total_charge_kw = charge_by_mode[Mode.HEAT] + charge_by_mode[Mode.COOL]
        total_discharge_kw = discharge_by_mode[Mode.HEAT] + discharge_by_mode[Mode.COOL]
        ledger["storage_charge_kwh"] = ledger["storage_charge_kwh"] + total_charge_kw * dt_h
        ledger["storage_discharge_kwh"] = ledger["storage_discharge_kwh"] + total_discharge_kw * dt_h
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
        targets: dict[str, EquipmentMode],
        delivered_map: dict[str, float],
        active_by_mode: dict[Mode, list[HeatPump]],
        device_output: dict[str, float],
        power_cap_kw: float,
        reserve_floors: dict[Mode, float],
    ) -> tuple[UnmetDemand, ...]:
        unmet: list[UnmetDemand] = []
        active_devices = [
            device for devices in active_by_mode.values() for device in devices
        ]
        total_active_capacity = sum(
            device.capacity_kw(device.mode) for device in active_devices
        )
        power_limited = (
            bool(active_devices)
            and power_cap_kw < total_active_capacity - 1e-9
            and abs(sum(device_output.values()) - power_cap_kw) < 1e-6
        )
        for room in sorted(rooms, key=lambda r: r.config.priority):
            if not room.is_known_demand:
                continue
            mode = room.demand
            equipment_mode = EquipmentMode(mode.value)
            shortfall = room.requested_kw - delivered_map.get(room.room_id, 0.0)
            if not active_by_mode[mode]:
                # 该方向本周期无设备实际运行：区分故障、未分配与等待。
                available = [device for device in self.devices if device.available]
                if not available:
                    reason = "equipment_faulted"
                elif any(
                    targets[device.device_id] is equipment_mode for device in available
                ):
                    reason = "min_off_not_satisfied"
                else:
                    # 若无故障的假设分配会覆盖该方向，则缺口由故障造成。
                    hypothetical = self._assign_device_modes(rooms, include_faulted=True)
                    if any(
                        hypothetical[device.device_id] is equipment_mode
                        for device in self.devices
                        if device.faulted
                    ):
                        reason = "equipment_faulted"
                    else:
                        reason = "mode_conflict"
                unmet.append(
                    UnmetDemand(room.room_id, mode, room.requested_kw, room.requested_kw, reason)
                )
                continue
            if shortfall <= 1e-9:
                continue
            storage_withheld = (
                self.storage is not None
                and reserve_floors[mode] > 0.0
                and self.storage.stored_kwh(mode) > 1e-9
            )
            if power_limited:
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
