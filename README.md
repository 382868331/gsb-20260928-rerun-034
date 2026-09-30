# 智能建筑控制引擎（Python 起步）

当前迭代只实现**多房间共用一台冷暖设备时的温度控制**，使用简化的本地房间模型。
照明与能耗策略是后续迭代，分层结构已为其预留扩展点。

## 架构

| 模块 | 职责 |
| --- | --- |
| `building_control/model.py` | 房间配置（`RoomConfig`）、房间状态（`RoomState`）、需求模式（`Mode`）与传感器状态（`SensorStatus`） |
| `building_control/plant.py` | 共用冷暖设备 `HeatPump`：冷热能力、最短开机/停机时间、禁止冷热直接互切 |
| `building_control/sensing.py` | `Measurement` 测量值与 `SensorRegistry`：超时断流判定 |
| `building_control/planner.py` | 控制计划 `ControlPlan`：冷热冲突仲裁 + 有限能力按优先级分配 |
| `building_control/engine.py` | `BuildingEngine`：测量汇集、计划生成、实际执行/交付（计划 vs 实际）、状态保存/恢复 |
| `building_control/simulation.py` | 简化本地集总参数房间热模型 `LocalRoomModel` |
| `demo.py` | 冷热需求相反的两房间端到端演示 |
| `tests/test_control.py` | 全部验收点测试 |

## 核心策略

- **冲突仲裁**：有冷热需求时，设备只能运行一种模式；`priority` 数值最小的房间决定模式，
  相反需求房间本周期不供给，仲裁原因写入 `ControlPlan.notes`。
- **有限能力分配**：同模式需求房间按优先级排序，逐房间分配换热功率，
  超出设备能力的低优先级房间拿到 0。
- **计划/实际分离**：`ControlPlan.allocations` 只是计划量，`TickResult.deliveries`
  才是房间本周期真实获得的功率；等待、模式冲突、能力不足和设备故障都会留下原因。
- **临时故障与重新分配**：`HeatPump.report_fault(until, now)` 将设备安全置为 `OFF`，
  故障期间计划仍可计算，但交付量强制为 0；恢复后重新仲裁并按优先级分配。
- **最短开/停时间**：`HeatPump.allow()` 是唯一的切换闸口。未满足最短开机时间不能停机，
  未满足最短停机时间不能启动，制热/制冷必须先回 `OFF` 再切换。
  计划要求与约束冲突时，引擎保留“计划模式”但“实际模式”保持不变，并报告剩余等待秒数。
- **传感器断流保守状态**：读数超过 `sensor_stale_after_s` 未更新即 `STALE`，从未上报为 `NEVER`；
  两种状态下温度显式为 `None`、需求为 `Mode.UNKNOWN`，绝不复用旧读数，设备不被未知需求驱动。
- **状态持久化**：`save_state/restore` 保存设备模式、`state_since`、故障截止时间
  （以及最近测量时间戳），进程重启后等待和故障窗口不重置、不会被跳过。

## 运行

```bash
.venv/bin/python demo.py
.venv/bin/python -m pytest tests/ -q
```

## 后续扩展

- 照明：新增独立的 `LightingPlan`/执行器，与温度计划在引擎层并列。
- 能耗策略：在 `Planner` 与设备能力之间增加预算约束与削峰策略。
- 更真实的房间/气象模型：替换 `simulation.LocalRoomModel`，引擎接口不变。
