"""Shared heating/cooling equipment capability model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Equipment:
    """Plant capabilities and timing interlocks.

    The same physical unit cannot heat and cool simultaneously. Capacity
    is shared between all rooms and is allocated by priority.
    """

    equipment_id: str
    heat_capacity: float
    cool_capacity: float
    min_on_seconds: float = 0.0
    min_off_seconds: float = 0.0
