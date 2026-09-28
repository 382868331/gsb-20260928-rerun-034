"""Execute plans while honouring equipment minimum on/off times."""

from __future__ import annotations

from dataclasses import dataclass

from .equipment import Equipment
from .planner import Plan


@dataclass
class Command:
    mode: str  # "heat", "cool" or "off"
    capacity: float
    reason: str = ""


class Executor:
    """State machine for one shared heating/cooling unit.

    A mode is latched for ``min_on_seconds``; once off, a restart (in either
    direction) waits for ``min_off_seconds``. Switching directly between
    heat and cool also passes through the off-state timer because the
    physical plant must stop before reversing.
    """

    def __init__(self, equipment: Equipment, now: float = 0.0):
        self.equipment = equipment
        # None means the unit has never run, so the first start is not
        # blocked by a minimum-off timer.
        self.mode: str | None = None
        self._changed_at = float(now)

    def execute(self, plan: Plan, now: float) -> Command:
        elapsed = now - self._changed_at
        if self.mode not in (None, "off"):
            if elapsed < self.equipment.min_on_seconds:
                return Command(self.mode, plan.capacity if plan.mode == self.mode else 0.0,
                               "min_on_timer")
            if plan.requested_on():
                if plan.mode == self.mode:
                    return Command(self.mode, plan.capacity, "running")
                # Reverse direction requires the minimum-off pause first.
                self.mode = "off"
                self._changed_at = now
                return Command("off", 0.0, "mode_change_cooldown")
            self.mode = "off"
            self._changed_at = now
            return Command("off", 0.0, "demand_cleared")

        if plan.requested_on():
            if self.mode == "off" and elapsed < self.equipment.min_off_seconds:
                return Command("off", 0.0, "min_off_timer")
            self.mode = plan.mode
            self._changed_at = now
            return Command(plan.mode, plan.capacity, "started")
        return Command("off", 0.0, "idle")

    def time_until_available(self, now: float) -> float:
        """Remaining seconds before the next start is allowed."""

        if self.mode not in (None, "off"):
            return 0.0
        if self.mode is None:
            return 0.0
        return max(self.equipment.min_off_seconds - (now - self._changed_at), 0.0)

    def to_state(self) -> dict[str, object]:
        return {"mode": self.mode or "off", "changed_at": self._changed_at,
                "never_run": self.mode is None}

    @classmethod
    def from_state(cls, equipment: Equipment, state: dict[str, object], now: float) -> "Executor":
        executor = cls(equipment, now=now)
        executor.mode = None if state.get("never_run") else str(state.get("mode", "off"))
        executor._changed_at = float(state.get("changed_at", now))
        return executor
