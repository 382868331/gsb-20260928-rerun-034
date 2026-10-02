"""智能建筑控制引擎。

当前范围：多台能力不同的冷热设备 + 冷热双罐储能 + 站点限电预算，
共同服务多个房间的温度控制，含故障/恢复与状态持久化。
"""

from .model import Mode, RoomConfig, RoomState, SensorStatus
from .plant import (
    DeviceDelivery,
    DeviceView,
    EquipmentMode,
    HeatPump,
    HeatPumpSpec,
    PowerSchedule,
)
from .planner import Allocation, ControlPlan, Planner, UnmetDemand
from .engine import (
    STRATEGY_COMFORT,
    STRATEGY_RESERVE,
    BuildingEngine,
    TickResult,
)
from .sensing import Measurement
from .storage import StorageFlow, ThermalStorage

__all__ = [
    "Mode",
    "RoomConfig",
    "RoomState",
    "SensorStatus",
    "EquipmentMode",
    "HeatPump",
    "HeatPumpSpec",
    "DeviceView",
    "DeviceDelivery",
    "PowerSchedule",
    "Allocation",
    "UnmetDemand",
    "ControlPlan",
    "Planner",
    "BuildingEngine",
    "TickResult",
    "Measurement",
    "ThermalStorage",
    "StorageFlow",
    "STRATEGY_COMFORT",
    "STRATEGY_RESERVE",
]
