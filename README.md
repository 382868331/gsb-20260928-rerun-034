# 智能建筑控制引擎（Python 起步）

当前迭代只实现**多房间共用一台冷暖设备时的温度控制**，使用简化的本地房间模型。
照明与能耗策略是后续迭代，分层结构已为其预留扩展点。

## 架构

| 模块 | 职责 |
| --- | --- |
| `building_control/model.py` | 房间配置（`RoomConfig`）、房间状态（`RoomState`）、需求模式（`Mode`）与传感器状态（`SensorStatus`） |
| `building_control/plant.py` | 共用冷暖设备 `HeatPump`：冷热能力、最短开机/停机时间、禁止冷热直接互切 |
| `building_control/sensing.py` | `Measurement` 测量值与 `SensorRegistry`：超时断流判定 |
| `building_control/planner.py` | 控制计划 `ControlPlan`：冷热冲突仲裁 + 有限能力按优先级分配 + 未满足需求 `UnmetDemand` 账目 |
| `building_control/engine.py` | `BuildingEngine`：测量汇集、计划生成、**实际能量交付**（计划 vs 实际）、未满足原因、状态保存/恢复 |
| `building_control/simulation.py` | 简化本地集总参数房间热模型 `LocalRoomModel` |
| `demo.py` | 冷热需求相反的两房间端到端演示 |
| `tests/test_control.py` | 全部验收点测试 |

## 核心策略

- **冲突仲裁**：有冷热需求时，设备只能运行一种模式；`priority` 数值最小的房间决定模式，
  相反需求房间本周期不供给，仲裁原因写入 `ControlPlan.notes`。
- **有限能力分配**：同模式需求房间按优先级排序，逐房间分配换热功率，
  超出设备能力的低优先级房间拿到 0。
- **计划量 ≠ 实际输出**：`ControlPlan.allocations` 只是“想供多少”；
  `TickResult.delivered` 才是设备按**当前真实运行模式**交付的能量，房间热模型只能消费它。
  最短开/停等待、模式切换、故障期间 `delivered` 为空（或为 0），绝不把计划量记成房间能量。
- **最短开/停时间**：`HeatPump.allow()` 是唯一的切换闸口。未满足最短开机时间不能停机，
  未满足最短停机时间不能启动；制热/制冷不能直接互切，引擎先把设备置回 `OFF`，
  等最短停机结束后再起新模式。等待期间需求保留，但不产生任何冷热能量。
- **临时故障与重新分配**：`HeatPump.set_available(False, t)` 表示设备故障急停，
  本周期所有需求进入 `UnmetDemand(reason="equipment_faulted")`；故障恢复后仍须满足
  最短停机（从急停时刻计），等待结束才重新按优先级分配能力给各房间。
- **未满足需求账目**：`TickResult.unmet` 逐条记录 `room_id / mode / requested_kw / unmet_kw / reason`，
  原因包括 `mode_conflict`（冷热冲突）、`min_off_not_satisfied`（停机/切换等待）、
  `capacity_exceeded`（能力不足）、`equipment_faulted`（设备故障），缺多少功率就报多少。
- **传感器断流保守状态**：读数超过 `sensor_stale_after_s` 未更新即 `STALE`，从未上报为 `NEVER`；
  两种状态下温度显式为 `None`、需求为 `Mode.UNKNOWN`，绝不复用旧读数，设备不被未知需求驱动。
- **状态持久化**：`save_state/restore` 保存设备模式、`state_since`、故障标志 `available`
  （以及最近测量时间戳），进程重启后等待时间与故障状态不重置、不会被跳过。

## 运行

```bash
.venv/bin/python demo.py
.venv/bin/python -m pytest tests/ -q
```

## 后续扩展

- 照明：新增独立的 `LightingPlan`/执行器，与温度计划在引擎层并列。
- 能耗策略：在 `Planner` 与设备能力之间增加预算约束与削峰策略。
- 更真实的房间/气象模型：替换 `simulation.LocalRoomModel`，引擎接口不变。
