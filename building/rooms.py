"""Room configuration and a deliberately simple local thermal model."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Room:
    """Static description of a room served by shared equipment.

    ``priority`` is a positive integer; smaller numbers win when rooms ask
    for conflicting use of a limited heating or cooling capacity.
    """

    room_id: str
    priority: int
    setpoint: float
    deadband: float = 0.5
    gain: float = 1.0
    loss: float = 0.05
    ambient: float = 20.0

    def demand(self, temperature: float | None) -> str:
        """Return ``heat``, ``cool`` or ``off`` for a temperature."""

        if temperature is None:
            return "off"
        if temperature < self.setpoint - self.deadband:
            return "heat"
        if temperature > self.setpoint + self.deadband:
            return "cool"
        return "off"


class RoomModel:
    """First-order local room model used for plan-vs-actual checks.

    ``gain`` maps delivered capacity to temperature rise per unit time,
    ``loss`` drives relaxation towards the ambient temperature. The model
    is intentionally local: there is no inter-room heat transfer.
    """

    def __init__(self, room: Room, initial_temperature: float):
        self.room = room
        self.temperature = float(initial_temperature)

    def step(self, delivered: float, dt: float) -> float:
        """Advance ``dt`` seconds; ``delivered`` is positive for heating."""

        drift = (self.room.ambient - self.temperature) * self.room.loss
        self.temperature += (drift + delivered * self.room.gain) * dt
        return self.temperature
