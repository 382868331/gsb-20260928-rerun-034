# 智能建筑控制引擎（Python 起步）

当前迭代实现**多个房间共用多台冷热设备 + 冷热缓冲储能**的温度控制，
使用简化的本地房间模型。照明与能耗策略是后续迭代，分层结构已为其预留扩展点。

## 架构

| 模块 | 职责 |
| --- | --- |
| `building_control/model.py` | 房间配置（`RoomConfig`）、房间状态（`RoomState`）、需求模式（`Mode`）与传感器状态（`SensorStatus`） |
| `building_control/plant.py` | 共用冷暖设备 `HeatPump`：冷热能力、最短开机/停机时间、禁止冷热直接互切；`PowerSchedule` 分时段站点限电 |
| `building_control/storage.py` | 冷热双仓缓冲储能 `ThermalStorage`：热量/冷量分开记账，充放效率损耗 |
| `building_control/sensing.py` | `Measurement` 测量值与 `SensorRegistry`：超时断流判定 |
| `building_control/planner.py` | 控制计划 `ControlPlan`：冷热冲突仲裁 + 有限能力按优先级分配 + 未满足需求 `UnmetDemand` 账目 |
| `building_control/engine.py` | `BuildingEngine`：多设备协同、储能充放调度、限电执行、**实际能量交付**（计划 vs 实际）、未满足原因、供能账目、状态保存/恢复 |
| `building_control/simulation.py` | 简化本地集总参数房间热模型 `LocalRoomModel` |
| `demo.py` | 两设备 + 储能 + 限电 + 故障恢复 + 双策略对比的端到端演示 |
| `tests/` | 全部验收点测试 |

## 核心策略

- **多设备协同**：`BuildingEngine` 接受设备列表，每台设备保留自己的最短开/停等待、
  故障标志与供能账目（`TickResult.devices` / `energy_ledger()`）。
  需求按优先级先由第一台设备的当刻产出满足，不足部分依次由后续设备补足；
  正常、限电与单台故障场景都按真实剩余能力分配，而不是只描述设备却按一台供给。
- **冲突仲裁**：有冷热需求时，机群只能运行一种模式；`priority` 数值最小的房间决定模式，
  相反需求房间仍保留一条 `0.0 kW` 服务记录，并以 `mode_conflict` 写入未满足原因，
  仲裁依据同时写入 `ControlPlan.notes`。
- **冷热缓冲储能**：`ThermalStorage` 分热仓/冷仓。设备供完房间后的富余出力给储能充电
  （低需求蓄能）；设备能力不足或限电时储能放电补足（高需求放能）。
  `TickResult.storage` 明确区分设备当刻产出、储能充/放功率与期末两仓储量；
  充电按充电效率入库、放电按放电效率出库，损耗凭空消失、不被补足。
- **两种储能策略**：`StoragePolicy.COMFORT_FIRST`（舒适优先，储能可放空）与
  `StoragePolicy.PRESERVE_RESERVE`（保留应急储备，放到 `reserve_fraction` 下限为止，
  被保留的缺口记为 `storage_reserve_held`）。同一剧本可对比两种安排的账目。
- **不同性质储量不相抵**：热仓与冷仓物理分开，设备从制热切换到制冷时，
  热仓储量原样保留，绝不当冷量使用，反之亦然。
- **站点限电**：`PowerSchedule` 按时段给出总功率上限，设备总出力（含给储能充电）
  不得超限；因限电产生的缺口记为 `power_limited`。
- **计划量 ≠ 实际输出**：`ControlPlan.allocations` 只是“想供多少”；
  `TickResult.delivered` 为每个有明确冷热需求的分区保留服务账，房间热模型只能消费其中非零交付。
  最短开/停等待、模式切换、故障、限电期间对应记录为 `0.0 kW`，绝不把计划量记成房间能量。
- **最短开/停时间**：`HeatPump.allow()` 是每台设备唯一的切换闸口，冷热不能直接互切，
  必须先回 `OFF` 等最短停机结束。等待期间需求保留，但不产生任何冷热能量。
- **临时故障与重新分配**：`HeatPump.set_available(False, t)` 急停回 `OFF`，
  其余设备继续按真实能力供给；故障恢复后仍须满足最短停机（从急停时刻计）。
- **未满足需求账目**：`TickResult.unmet` 逐条记录 `room_id / mode / requested_kw / unmet_kw / reason`，
  原因包括 `mode_conflict`、`min_off_not_satisfied`、`capacity_exceeded`、
  `equipment_faulted`、`power_limited`、`storage_reserve_held`；
  `energy_ledger()` 把各设备供能、储能充放与各房间未满足能量按 tick 间隔累计成 kWh。
- **传感器断流保守状态**：读数超过 `sensor_stale_after_s` 未更新即 `STALE`，从未上报为 `NEVER`；
  两种状态下温度显式为 `None`、需求为 `Mode.UNKNOWN`，绝不复用旧读数。
- **状态持久化**：`save_state/restore` 保存全部设备的模式、`state_since`、故障标志、
  储能两仓储量、限电计划、储能策略与累计账目（以及最近测量时间戳），
  进程重启后储量、各自等待计时与未满足需求账目保持连续。

## 运行

```bash
.venv/bin/python demo.py
.venv/bin/python -m pytest tests/ -q
```

## 后续扩展

- 照明：新增独立的 `LightingPlan`/执行器，与温度计划在引擎层并列。
- 能耗策略：在 `Planner` 与设备能力之间增加预算约束与削峰策略。
- 更真实的房间/气象模型：替换 `simulation.LocalRoomModel`，引擎接口不变。
