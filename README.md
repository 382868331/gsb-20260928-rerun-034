# 智能建筑控制引擎（第一步：多房间共用冷暖设备温控）

纯 Python 标准库实现，分层建模，后续可在同一结构上扩展照明与能耗策略。

## 模型划分

- `building/rooms.py`：`Room`（优先级、设定值、死区、热工参数）与简化本地房间模型 `RoomModel`，房间之间无耦合。
- `building/equipment.py`：`Equipment` 描述共用冷暖设备的供热/供冷能力与最短开、最短停时间。
- `building/measurements.py`：`MeasurementBus` 保存传感器读数；超过 `timeout_seconds` 后 `latest()` 返回 `None`，绝不把旧读数当最新值。
- `building/planner.py`：`Planner` 计算需求。冷热需求冲突时由最高优先级（priority 最小）房间决定模式；同模式房间再按优先级瓜分有限能力，未满足房间进入 `unsatisfied`，传感器断流房间进入 `degraded_rooms`。
- `building/executor.py`：`Executor` 执行计划，遵守 `min_on_seconds` / `min_off_seconds`；冷热反向切换必须先停机并走完最短停时间。
- `building/engine.py`：`ControlEngine` 串联测量 → 计划 → 实际执行，`delivered()` 给出实际分配，`save_state()` / `load_state()` 用绝对时间戳保存执行器与传感器状态，重启后不会重置设备等待计时。

## 保守策略

传感器缺失或断流时：该房间不分配能力、被标记为 degraded；若无其它有效需求，设备保持/进入关闭。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试覆盖：

1. 冷热需求相反的房间：计划按优先级选热，低优先级供冷房间不执行；仿真验证实际温度变化。
2. 有限能力在同模式房间间按优先级分配。
3. 传感器断流后旧读数不再被使用，进入明确保守状态。
4. 最短开/停时间约束；保存状态后重启，未走完的最短停等待仍然生效。

## 后续扩展

照明与能耗策略将分别新增模块，复用 `MeasurementBus`、优先级分配与时间戳驱动的执行器框架，不改变本地房间温控语义。
