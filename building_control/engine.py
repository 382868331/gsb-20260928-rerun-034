"""控制引擎：汇集测量、生成计划、驱动共用设备，并可保存/恢复状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import Mode, RoomConfig, RoomState, SensorStatus
from .planner import Allocation, ControlPlan, Planner, UnmetDemand
from .plant import EquipmentMode, HeatPump
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
        equipment: HeatPump,
        sensor_stale_after_s: float = 30.0,
        time_fn: Callable[[], float] | None = None,
    ) -> None:
        if not rooms:
            raise ValueError("至少需要一个房间")
        ids = [room.room_id for room in rooms]
        if len(ids) != len(set(ids)):
            raise ValueError("房间 ID 必须唯一")
        self.rooms = rooms
        self.equipment = equipment
        self.sensors = SensorRegistry(sensor_stale_after_s)
        self.planner = Planner(equipment)
        self.time_fn = time_fn

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

    def tick(self, now: float | None = None) -> TickResult:
        current = self.now() if now is None else now
        room_states = self._room_states(current)
        plan = self.planner.plan(room_states)
        previous_mode = self.equipment.mode

        degraded = any(room.sensor_status is not SensorStatus.FRESH for room in room_states)
        any_valid_demand = any(room.is_known_demand for room in room_states)
        if not any_valid_demand:
            target = EquipmentMode.OFF
        elif previous_mode.running and plan.desired_mode is not previous_mode:
            # 冷热不能直接互切：先回到 OFF，等最短停机结束后再起新模式。
            target = EquipmentMode.OFF
        else:
            target = plan.desired_mode

        if self.equipment.allow(target, current):
            self.equipment.apply(target, current)
        applied_mode = self.equipment.mode

        delivered_mode = (
            applied_mode
            if self.equipment.available and applied_mode.running
            else EquipmentMode.OFF
        )
        delivered, unmet = self._deliver(room_states, plan, delivered_mode, current)

        return TickResult(
            timestamp=current,
            plan=plan,
            previous_mode=previous_mode,
            applied_mode=applied_mode,
            rooms=tuple(room_states),
            degraded=degraded,
            conservative_shutdown=not any_valid_demand,
            lockout_remaining_s=self.equipment.remaining_lockout_s(current),
            delivered_mode=delivered_mode,
            delivered=delivered,
            unmet=unmet,
        )

    def _deliver(
        self,
        rooms: list[RoomState],
        plan: ControlPlan,
        delivered_mode: EquipmentMode,
        now: float,
    ) -> tuple[tuple[Allocation, ...], tuple[UnmetDemand, ...]]:
        """只按设备当前真实运行的模式交付能量，并为所有缺供给出原因。"""

        if not delivered_mode.running:
            return (), tuple(self._unmet_without_delivery(rooms, plan, now))

        winner = Mode(delivered_mode.value)
        capacity = self.equipment.capacity_kw(delivered_mode)
        delivered, _ = self.planner.allocate(rooms, winner, capacity)

        unmet: list[UnmetDemand] = []
        delivered_map = {a.room_id: a.allocated_kw for a in delivered}
        for room in sorted(rooms, key=lambda r: r.config.priority):
            if not room.is_known_demand:
                continue
            if room.demand is winner:
                shortfall = room.requested_kw - delivered_map.get(room.room_id, 0.0)
                if shortfall > 1e-9:
                    unmet.append(
                        UnmetDemand(
                            room.room_id,
                            room.demand,
                            room.requested_kw,
                            shortfall,
                            "capacity_exceeded",
                        )
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
        return delivered, tuple(unmet)

    def _unmet_without_delivery(
        self, rooms: list[RoomState], plan: ControlPlan, now: float
    ) -> list[UnmetDemand]:
        lockout = self.equipment.remaining_lockout_s(now)
        if self.equipment.faulted:
            reason = "equipment_faulted"
        elif plan.desired_mode.running:
            reason = "min_off_not_satisfied"
        else:
            reason = "not_running"
        items: list[UnmetDemand] = []
        for room in sorted(rooms, key=lambda r: r.config.priority):
            if room.is_known_demand:
                items.append(
                    UnmetDemand(
                        room.room_id,
                        room.demand,
                        room.requested_kw,
                        room.requested_kw,
                        reason,
                    )
                )
        return items

    def save_state(self, path: str | Path) -> None:
        payload = {
            "sensor_stale_after_s": self.sensors._stale_after_s,
            "equipment": self.equipment.snapshot(),
            "readings": self.sensors.snapshot(),
        }
        Path(path).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def load_state(self, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.equipment.restore(payload["equipment"])
        self.sensors.restore(payload.get("readings", {}))

    @classmethod
    def restore(cls, path, rooms, equipment, time_fn=None):
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        engine = cls(
            rooms=rooms,
            equipment=equipment,
            sensor_stale_after_s=float(payload["sensor_stale_after_s"]),
            time_fn=time_fn,
        )
        engine.load_state(path)
        return engine
