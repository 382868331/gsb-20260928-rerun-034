"""智能建筑控制引擎（Python 起步版本）。

当前范围：多个房间共用一台冷暖设备时的温度控制。
照明与能耗策略在后续迭代加入。
"""

from .model import Mode, RoomConfig, RoomState, SensorStatus
from .plant import EquipmentMode, HeatPump, HeatPumpSpec, PowerSchedule
from .planner import Allocation, ControlPlan, Planner, UnmetDemand
from .storage import StorageSpec, ThermalStorage
from .engine import (
    BuildingEngine,
    DeviceTickInfo,
    StoragePolicy,
    StorageTickInfo,
    TickResult,
)
from .sensing import Measurement

__all__ = [
    "Mode",
    "RoomConfig",
    "RoomState",
    "SensorStatus",
    "EquipmentMode",
    "HeatPump",
    "HeatPumpSpec",
    "PowerSchedule",
    "StorageSpec",
    "ThermalStorage",
    "StoragePolicy",
    "DeviceTickInfo",
    "StorageTickInfo",
    "Allocation",
    "UnmetDemand",
    "ControlPlan",
    "Planner",
    "BuildingEngine",
    "TickResult",
    "Measurement",
]
