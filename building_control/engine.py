"""控制引擎：汇集测量、生成计划、驱动共用设备，并可保存/恢复状态。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .model import Mode, RoomConfig, RoomState, SensorStatus
from .planner import ControlPlan, Delivery, Planner
from .plant import EquipmentMode, HeatPump
from .sensing import Measurement, SensorRegistry


@dataclass(frozen=True)
class TickResult:
    timestamp: float
    plan: ControlPlan
    previous_mode: EquipmentMode
    applied_mode: EquipmentMode
    rooms: tuple[RoomState, ...]
    deliveries: tuple[Delivery, ...]
    degraded: bool
    conservative_shutdown: bool
    equipment_available: bool
    unavailable_remaining_s: float
    lockout_remaining_s: float

    def allocation_for(self, room_id: str):
        return self.plan.allocated_for(room_id)

    def delivery_for(self, room_id: str):
        for delivery in self.deliveries:
            if delivery.room_id == room_id:
                return delivery
        return None

    @property
    def unmet_reasons(self) -> tuple[str, ...]:
        return tuple(
            f"{delivery.room_id}: {reason}"
            for delivery in self.deliveries
            for reason in delivery.reasons
        )


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
        target = plan.desired_mode if any_valid_demand else EquipmentMode.OFF
        transition_target = target
        if (
            any_valid_demand
            and target.running
            and self.equipment.mode.running
            and target is not self.equipment.mode
            and self.equipment.can_turn_off(current)
        ):
            transition_target = EquipmentMode.OFF

        if self.equipment.allow(transition_target, current):
            self.equipment.apply(transition_target, current)
        applied_mode = self.equipment.mode

        deliveries = self._deliver(room_states, plan, applied_mode, current)

        return TickResult(
            timestamp=current,
            plan=plan,
            previous_mode=previous_mode,
            applied_mode=applied_mode,
            rooms=tuple(room_states),
            deliveries=tuple(deliveries),
            degraded=degraded,
            conservative_shutdown=not any_valid_demand,
            equipment_available=self.equipment.is_available(current),
            unavailable_remaining_s=self.equipment.unavailable_remaining_s(current),
            lockout_remaining_s=self.equipment.remaining_lockout_s(current),
        )

    def _deliver(
        self,
        room_states: list[RoomState],
        plan: ControlPlan,
        applied_mode: EquipmentMode,
        now: float,
    ) -> list[Delivery]:
        deliveries: list[Delivery] = []
        available = self.equipment.is_available(now)

        for room in room_states:
            if not room.is_known_demand:
                continue

            allocation = plan.allocated_for(room.room_id)
            planned_kw = allocation.allocated_kw if allocation is not None else 0.0
            reasons: list[str] = []

            if not available:
                reasons.append("设备临时故障/不可用，本周期无实际输出")
            elif room.demand.value != plan.desired_mode.value:
                if plan.desired_mode.running:
                    reasons.append(
                        f"模式冲突：本周期执行 {plan.desired_mode.value}，{room.demand.value} 需求不供给"
                    )
                else:
                    reasons.append("本周期计划停机，需求不供给")
            elif room.demand.value != applied_mode.value:
                if applied_mode is EquipmentMode.OFF:
                    reasons.append("设备仍处于最短停机等待，计划供能尚未成为实际输出")
                else:
                    reasons.append(
                        f"设备仍保持 {applied_mode.value}，模式切换/最短开机等待未结束"
                    )

            delivered_kw = planned_kw if not reasons else 0.0
            if room.requested_kw > delivered_kw + 1e-9 and available:
                capacity_shortfall = room.requested_kw - max(planned_kw, delivered_kw)
                if capacity_shortfall > 1e-9 and not any(
                    "模式" in reason or "等待" in reason for reason in reasons
                ):
                    reasons.append(
                        f"设备能力不足，尚有 {capacity_shortfall:.3g}kW 需求未满足"
                    )

            deliveries.append(
                Delivery(
                    room_id=room.room_id,
                    mode=room.demand,
                    requested_kw=room.requested_kw,
                    planned_kw=planned_kw,
                    delivered_kw=delivered_kw,
                    reasons=tuple(reasons),
                )
            )
        return deliveries

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
