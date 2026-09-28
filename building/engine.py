"""Measurement -> plan -> execution loop with persistent timers."""

from __future__ import annotations

import json
from pathlib import Path

from .equipment import Equipment
from .executor import Command, Executor
from .measurements import MeasurementBus, Reading
from .planner import Plan
from .planner import Planner
from .rooms import Room


class ControlEngine:
    """Bind rooms, a measurement bus, the planner and the executor.

    Timestamps are supplied by the caller (simulation or wall clock), so
    minimum on/off timing and sensor timeouts behave identically in tests
    and in deployment.
    """

    def __init__(
        self,
        rooms: list[Room],
        equipment: Equipment,
        bus: MeasurementBus | None = None,
        now: float = 0.0,
    ):
        if not rooms:
            raise ValueError("at least one room is required")
        self.rooms = rooms
        self.equipment = equipment
        self.bus = bus or MeasurementBus()
        self.planner = Planner(equipment)
        self.executor = Executor(equipment, now=now)
        self.last_plan: Plan | None = None
        self.last_command: Command | None = None

    def sensor_id(self, room_id: str) -> str:
        return f"temp:{room_id}"

    def read_temperatures(self, now: float) -> dict[str, float | None]:
        return {
            room.room_id: self.bus.latest(self.sensor_id(room.room_id), now)
            for room in self.rooms
        }

    def step(self, now: float) -> tuple[Plan, Command]:
        temperatures = self.read_temperatures(now)
        plan = self.planner.plan(self.rooms, temperatures)
        command = self.executor.execute(plan, now)
        self.last_plan = plan
        self.last_command = command
        return plan, command

    def delivered(self) -> dict[str, float]:
        """Actual per-room delivery from the latest executed command.

        Positive values heat the room, negative values cool it. A timer
        hold returns zero even if the plan still asks for capacity.
        """

        if self.last_plan is None or self.last_command is None:
            return {room.room_id: 0.0 for room in self.rooms}
        if self.last_command.mode == "off" or self.last_command.capacity <= 0.0:
            return {room.room_id: 0.0 for room in self.rooms}

        requested_total = sum(
            allocation.capacity
            for allocation in self.last_plan.allocations
            if allocation.mode == self.last_command.mode
        )
        scale = self.last_command.capacity / requested_total if requested_total else 0.0
        sign = 1.0 if self.last_command.mode == "heat" else -1.0
        result = {room.room_id: 0.0 for room in self.rooms}
        for allocation in self.last_plan.allocations:
            if allocation.mode == self.last_command.mode:
                result[allocation.room_id] = sign * allocation.capacity * scale
        return result

    def save_state(self, path: str | Path) -> None:
        """Persist executor timers and buffered sensor readings.

        Absolute timestamps are stored, so a restart cannot reset a pending
        minimum on/off wait.
        """

        payload = {
            "equipment_id": self.equipment.equipment_id,
            "executor": self.executor.to_state(),
            "readings": {
                sensor_id: {"value": reading.value, "timestamp": reading.timestamp}
                for sensor_id, reading in self.bus._readings.items()
            },
        }
        Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def load_state(self, path: str | Path) -> None:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        self.executor = Executor.from_state(
            self.equipment, payload["executor"], now=0.0
        )
        self.bus._readings = {
            sensor_id: Reading(float(item["value"]), float(item["timestamp"]))
            for sensor_id, item in payload.get("readings", {}).items()
        }
