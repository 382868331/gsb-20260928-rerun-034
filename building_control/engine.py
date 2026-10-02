"""控制引擎：汇集测量、生成计划、驱动多台设备与储能，并可保存/恢复状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .model import Mode, RoomConfig, RoomState, SensorStatus
from .planner import Allocation, ControlPlan, Planner, UnmetDemand
from .plant import DeviceDelivery, EquipmentMode, HeatPump, PowerSchedule
from .sensing import Measurement, SensorRegistry
from .storage import StorageFlow, ThermalStorage

STRATEGY_COMFORT = "comfort"
STRATEGY_RESERVE = "reserve"


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
    device_deliveries: tuple[DeviceDelivery, ...] = field(default_factory=tuple)
    storage_flow: StorageFlow | None = None
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

    def delivery_for(self, device_id: str):
        for delivery in self.device_deliveries:
            if delivery.device_id == device_id:
                return delivery
        return None


class BuildingEngine:
    """多设备 + 储能 + 限电预算的建筑冷热控制引擎。

    - equipment 可以是一台 HeatPump，也可以是多台组成的列表；
      每台设备各自保留最短开/停等待与故障状态。
    - power_schedule 给出站点级用电上限，限电时按房间优先级分配真实功率。
    - storage 为冷热双罐储能：低需求蓄能、高需求/限电释放，
      两种策略：comfort（优先保障舒适）/ reserve（保留应急储备）。
    """

    def __init__(
        self,
        rooms: list[RoomConfig],
        equipment: "HeatPump | Iterable[HeatPump]",
        sensor_stale_after_s: float = 30.0,
        time_fn: "Callable[[], float] | None" = None,
        power_schedule: "PowerSchedule | None" = None,
        storage: "ThermalStorage | None" = None,
        strategy: str = STRATEGY_COMFORT,
        reserve_fraction: float = 0.3,
    ) -> None:
        if not rooms:
            raise ValueError("至少需要一个房间")
        ids = [room.room_id for room in rooms]
        if len(ids) != len(set(ids)):
            raise ValueError("房间 ID 必须唯一")
        if isinstance(equipment, HeatPump):
            devices = [equipment]
        else:
            devices = list(equipment)
        if not devices:
            raise ValueError("至少需要一台设备")
        if len({d.device_id for d in devices}) != len(devices):
            raise ValueError("设备 ID 必须唯一")
        if strategy not in (STRATEGY_COMFORT, STRATEGY_RESERVE):
            raise ValueError(f"未知策略：{strategy}")
        if not (0.0 <= reserve_fraction <= 1.0):
            raise ValueError("reserve_fraction 必须在 [0, 1] 区间")
        self.rooms = rooms
        self.devices = devices
        self.equipment = devices[0]
        self.sensors = SensorRegistry(sensor_stale_after_s)
        self.planner = Planner()
        self.time_fn = time_fn
        self.power_schedule = power_schedule or PowerSchedule()
        self.storage = storage
        self.strategy = strategy
        self.reserve_fraction = float(reserve_fraction)

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

    def _reserve_kwh(self) -> float:
        if self.storage is None or self.strategy != STRATEGY_RESERVE:
            return 0.0
        return self.reserve_fraction * self.storage.capacity_kwh

    def tick(self, now: "float | None" = None, dt_s: "float | None" = None) -> TickResult:
        current = self.now() if now is None else now
        if self.storage is not None and (dt_s is None or dt_s <= 0.0):
            raise ValueError("配置储能后 tick 必须提供正的 dt_s 以结算能量")
        room_states = self._room_states(current)
        views = [device.view() for device in self.devices]
        power_cap_kw = self.power_schedule.cap_kw(current)
        plan = self.planner.plan(room_states, views, power_cap_kw)

        previous_mode = self.equipment.mode
        any_valid_demand = any(room.is_known_demand for room in room_states)
        targets = dict(plan.device_targets)
        for device in self.devices:
            target = targets.get(device.device_id, EquipmentMode.OFF)
            if not any_valid_demand:
                target = EquipmentMode.OFF
            elif device.mode.running and target is not device.mode:
                # 冷热不能直接互切：先回到 OFF，等最短停机结束后再起新模式。
                target = EquipmentMode.OFF
            if device.allow(target, current):
                device.apply(target, current)

        delivered_modes = {
            device.device_id: (
                device.mode if device.available and device.mode.running else EquipmentMode.OFF
            )
            for device in self.devices
        }
        delivered, unmet, device_deliveries, storage_flow = self._deliver(
            room_states, plan, delivered_modes, power_cap_kw, current, dt_s
        )

        applied_mode = self.equipment.mode
        return TickResult(
            timestamp=current,
            plan=plan,
            previous_mode=previous_mode,
            applied_mode=applied_mode,
            rooms=tuple(room_states),
            degraded=any(room.sensor_status is not SensorStatus.FRESH for room in room_states),
            conservative_shutdown=not any_valid_demand,
            lockout_remaining_s=max(
                (device.remaining_lockout_s(current) for device in self.devices), default=0.0
            ),
            delivered_mode=delivered_modes[self.equipment.device_id],
            delivered=delivered,
            unmet=unmet,
            device_deliveries=device_deliveries,
            storage_flow=storage_flow,
            power_cap_kw=power_cap_kw,
        )

    def _deliver(
        self,
        rooms: list[RoomState],
        plan: ControlPlan,
        delivered_modes: dict,
        power_cap_kw: float,
        now: float,
        dt_s: "float | None",
    ):
        """按真实运行模式、限电预算与储能状态交付能量，并记录全部账目。"""

        known = sorted(
            (room for room in rooms if room.is_known_demand),
            key=lambda room: room.config.priority,
        )
        device_out = {device.device_id: 0.0 for device in self.devices}
        device_to_storage = {device.device_id: 0.0 for device in self.devices}
        delivered_kw = {room.room_id: 0.0 for room in known}
        budget = power_cap_kw

        # 1) 设备直供：按房间优先级，从同模式设备的真实剩余能力与剩余功率预算中取能。
        for room in known:
            want = room.requested_kw
            for device in self.devices:
                if want <= 1e-9 or budget <= 1e-9:
                    break
                mode = delivered_modes[device.device_id]
                if mode.value != room.demand.value:
                    continue
                spare = device.capacity_kw(mode) - device_out[device.device_id]
                if spare <= 1e-9:
                    continue
                give = min(want, spare, budget)
                device_out[device.device_id] += give
                budget -= give
                delivered_kw[room.room_id] += give
                want -= give

        # 2) 储能放电补缺（高需求或限电时释放；reserve 策略保留应急储备）。
        discharge = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        if self.storage is not None:
            reserve = self._reserve_kwh()
            discharge_left = self.storage.max_discharge_kw
            for room in known:
                shortfall = room.requested_kw - delivered_kw[room.room_id]
                if shortfall <= 1e-9 or discharge_left <= 1e-9:
                    continue
                got = self.storage.discharge(
                    room.demand, min(shortfall, discharge_left), dt_s, reserve
                )
                discharge_left -= got
                discharge[room.demand] += got
                delivered_kw[room.room_id] += got

        # 3) 储能充电（低需求时设备富余能力蓄能，同样受功率预算约束）。
        charge = {Mode.HEAT: 0.0, Mode.COOL: 0.0}
        if self.storage is not None:
            charge_left = self.storage.max_charge_kw
            for device in self.devices:
                mode = delivered_modes[device.device_id]
                if not mode.running or budget <= 1e-9 or charge_left <= 1e-9:
                    continue
                spare = device.capacity_kw(mode) - device_out[device.device_id]
                if spare <= 1e-9:
                    continue
                accepted = self.storage.charge(
                    Mode(mode.value), min(spare, budget, charge_left), dt_s
                )
                charge_left -= accepted
                charge[Mode(mode.value)] += accepted
                device_to_storage[device.device_id] += accepted
                device_out[device.device_id] += accepted
                budget -= accepted

        # 4) 未满足需求对账。
        any_device_output = sum(device_out.values()) > 1e-9
        any_discharge = sum(discharge.values()) > 1e-9
        spare_after = any(
            delivered_modes[device.device_id].running
            and device.capacity_kw(delivered_modes[device.device_id])
            - device_out[device.device_id]
            > 1e-9
            for device in self.devices
        )
        power_limited = budget <= 1e-9 and spare_after
        unmet: list[UnmetDemand] = []
        for room in known:
            shortfall = room.requested_kw - delivered_kw[room.room_id]
            if shortfall <= 1e-9:
                continue
            reason = self._shortfall_reason(
                room, plan, any_device_output, any_discharge, power_limited
            )
            unmet.append(
                UnmetDemand(room.room_id, room.demand, room.requested_kw, shortfall, reason)
            )

        # 5) 汇总设备与储能账目。
        deliveries = tuple(
            DeviceDelivery(
                device_id=device.device_id,
                mode=device.mode,
                delivered_mode=delivered_modes[device.device_id],
                output_kw=device_out[device.device_id],
                to_rooms_kw=device_out[device.device_id] - device_to_storage[device.device_id],
                to_storage_kw=device_to_storage[device.device_id],
                lockout_remaining_s=device.remaining_lockout_s(now),
                faulted=device.faulted,
            )
            for device in self.devices
        )
        storage_flow = None
        if self.storage is not None:
            storage_flow = StorageFlow(
                charge_heat_kw=charge[Mode.HEAT],
                charge_cool_kw=charge[Mode.COOL],
                discharge_heat_kw=discharge[Mode.HEAT],
                discharge_cool_kw=discharge[Mode.COOL],
                heat_stored_kwh=self.storage.heat_kwh,
                cool_stored_kwh=self.storage.cool_kwh,
            )

        delivered = tuple(
            Allocation(
                room_id=room.room_id,
                mode=room.demand,
                requested_kw=room.requested_kw,
                allocated_kw=delivered_kw[room.room_id],
            )
            for room in known
            if any(
                delivered_modes[device.device_id].value == room.demand.value
                for device in self.devices
            )
            or delivered_kw[room.room_id] > 1e-9
        )
        return delivered, tuple(unmet), deliveries, storage_flow

    def _shortfall_reason(
        self,
        room: RoomState,
        plan: ControlPlan,
        any_device_output: bool,
        any_discharge: bool,
        power_limited: bool,
    ) -> str:
        if not any_device_output and not any_discharge:
            if any(device.faulted for device in self.devices):
                return "equipment_faulted"
            if any(target.running for _, target in plan.device_targets):
                return "min_off_not_satisfied"
            return "not_running"
        if any(device.faulted for device in self.devices):
            return "equipment_faulted"
        if power_limited:
            return "power_capped"
        if (
            self.storage is not None
            and self.strategy == STRATEGY_RESERVE
            and self._reserve_kwh() > 0.0
            and self.storage.stored_kwh(room.demand) >= self._reserve_kwh() - 1e-9
        ):
            return "reserve_preserved"
        return "capacity_exceeded"

    def save_state(self, path) -> None:
        payload = {
            "sensor_stale_after_s": self.sensors._stale_after_s,
            "strategy": self.strategy,
            "reserve_fraction": self.reserve_fraction,
            "devices": [device.snapshot() for device in self.devices],
            "power_schedule": self.power_schedule.snapshot(),
            "storage": self.storage.snapshot() if self.storage is not None else None,
            "readings": self.sensors.snapshot(),
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_state(self, path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        snapshots = payload.get("devices") or [payload["equipment"]]
        by_id = {device.device_id: device for device in self.devices}
        for snapshot in snapshots:
            device = by_id.get(str(snapshot.get("device_id")))
            if device is not None:
                device.restore(snapshot)
        if payload.get("storage") is not None and self.storage is not None:
            self.storage.restore(payload["storage"])
        self.sensors.restore(payload.get("readings", {}))

    @classmethod
    def restore(cls, path, rooms, equipment, storage=None, time_fn=None):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        engine = cls(
            rooms=rooms,
            equipment=equipment,
            sensor_stale_after_s=float(payload["sensor_stale_after_s"]),
            time_fn=time_fn,
            power_schedule=PowerSchedule.restore(payload.get("power_schedule")),
            storage=storage,
            strategy=payload.get("strategy", STRATEGY_COMFORT),
            reserve_fraction=float(payload.get("reserve_fraction", 0.3)),
        )
        engine.load_state(path)
        return engine
