# 智能建筑控制引擎（Python）

本迭代支持多个房间共用**多台能力不同的冷热设备**，并处理冷热模式冲突、逐设备最短开停机、
逐设备故障、按时段变化的总功率上限、实际供能账目以及保存/恢复。

## 架构

| 模块 | 职责 |
| --- | --- |
| `building_control/model.py` | 房间配置、房间状态、需求方向与传感器状态 |
| `building_control/plant.py` | `HeatPumpSpec/HeatPump` 和 `EquipmentFleet`：单机能力、故障、逐设备最短开/停，设备组禁止同时冷热 |
| `building_control/schedule.py` | `PowerLimit/PowerSchedule`：按时段查询制热/制冷总功率上限 |
| `building_control/planner.py` | 冷热冲突仲裁、优先级分配、计划分配与未满足功率 |
| `building_control/accounting.py` | `EnergyLedger/RoomEnergy`：只累计真实交付功率形成的能量账目 |
| `building_control/engine.py` | 执行设备轮换、交付真实功率、解释未满足原因、持久化设备/传感器/账目状态 |
| `building_control/sensing.py` | 最近测量与断流判定 |
| `building_control/simulation.py` | 简化本地房间热模型 |
| `demo.py` | 双机正常、限电、单机故障恢复和冷热切换演示 |
| `tests/test_control.py` | 验收测试 |

## 核心策略

- **双机/多机不反向同供**：`EquipmentFleet` 在任一时刻只允许统一为制热、制冷或停机；新模式启动前，相反模式设备必须先停机。
- **逐设备最短约束**：每台设备单独保存 `mode/state_since/available`，设备轮换或故障急停不会重置其他设备的等待计时。
- **按时段限电**：`PowerSchedule.limit_kw(mode, t)` 取得当前总功率上限；计划和真实交付都取设备能力与外部功率上限的较小值，限电缺口记为 `power_limit_exceeded`。
- **故障后重新分配**：单台设备故障立即急停，其他可运行设备继续服务并按房间优先级重新分配；故障设备恢复后仍要完成自己的最短停机等待。
- **计划不等于交付**：最短开/停机、冷热切换、设备故障、限电等情况下，只有 `TickResult.delivered` 中存在的功率可进入房间热模型和 `EnergyLedger`。
- **未满足原因**：包括 `mode_conflict`、`min_on_not_satisfied`、`min_off_not_satisfied`、`capacity_exceeded`、`power_limit_exceeded`、`equipment_faulted`。
- **重启一致性**：状态文件保存每台设备状态、最近测量、时段计划、上次 tick、上次实际交付功率和累计能量；恢复后等待计时与继续积分结果与连续运行一致。

## 运行

```bash
.venv/bin/python demo.py
.venv/bin/python -m pytest tests/ -q
```
