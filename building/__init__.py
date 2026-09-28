"""Simplified local-room building control engine.

The engine is split into independent models:

* :mod:`building.rooms`        - local thermal room models and setpoints
* :mod:`building.equipment`    - shared heating/cooling plant capabilities
* :mod:`building.measurements` - sensor readings with freshness tracking
* :mod:`building.planner`      - priority based capacity allocation
* :mod:`building.executor`     - enforces minimum on/off times
* :mod:`building.engine`       - ties measurement, planning and execution together

Lighting and energy strategies intentionally live outside this first step.
"""

from .equipment import Equipment
from .measurements import MeasurementBus, Reading
from .planner import Allocation, Demand, Plan, Planner
from .rooms import Room, RoomModel

__all__ = [
    "Allocation",
    "Demand",
    "Equipment",
    "MeasurementBus",
    "Plan",
    "Planner",
    "Reading",
    "Room",
    "RoomModel",
]
