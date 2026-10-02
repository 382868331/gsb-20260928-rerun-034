# 智能建筑控制引擎

多台能力不同的冷热设备 + 冷热双罐储能 + 站点限电预算，共同服务多个房间的温度控制。
每台设备各自保留最短开/停等待与供能账目；储能、限电、故障恢复全程按真实能量对账。

## 架构

| 模块 | 职责 |
| --- | --- |
| `building_control/model.py` | 房间配置（`RoomConfig`）、房间状态（`RoomState`）、需求模式（`Mode`）与传感器状态（`SensorStatus`） |
| `building_control/plant.py` | 冷暖设备 `HeatPump`（冷热能力、最短开/停、禁止冷热直接互切）、`DeviceView`/`DeviceDelivery` 设备快照与供能账目、`PowerSchedule` 分时段限电上限 |
| `building_control/storage.py` | `ThermalStorage` 冷热双罐储能：独立储量、充放效率损耗、充放功率上限；`StorageFlow` 单周期充放对账 |
| `building_control/sensing.py` | `Measurement` 测量值与 `SensorRegistry`：超时断流判定 |
| `building_control/planner.py` | 控制计划 `ControlPlan`：多设备模式仲裁 + 有限能力按优先级分配 + 未满足需求 `UnmetDemand` 账目 |
| `building_control/engine.py` | `BuildingEngine`：测量汇集、计划生成、**实际能量交付**（设备直供 + 储能充放 + 限电预算）、未满足原因、状态保存/恢复 |
| `building_control/simulation.py` | 简化本地集总参数房间热模型 `LocalRoomModel` |
| `demo.py` | 两设备 + 储能 + 限电跨高需求 + 故障恢复 + 双策略对比的端到端演示 |
| `tests/` | 全部验收点测试（`test_control.py` 单设备行为，`test_multi_asset.py` 多设备/储能/限电/持久化） |

## 核心策略

- **多设备共同服务**：`BuildingEngine` 接受设备列表。第一台可用设备服务最高优先级房间的
  需求模式，后续设备服务剩余需求最大的模式——可以一台制热一台制冷并行。
  每台设备各自执行最短开/停等待与故障急停，`TickResult.device_deliveries` 逐台记录
  当刻产出（`output_kw = to_rooms_kw + to_storage_kw`），绝不是“描述两台却按一台供”。
- **有限能力分配**：同模式需求房间按优先级排序分配，超出设备总能力的低优先级房间拿到 0，
  并记录 `capacity_exceeded` 未满足账目。
- **限电预算**：`PowerSchedule` 给出分时段站点功率上限。限电时设备总出力被真实压缩，
  按房间优先级分配剩余预算，缺口记 `power_capped`。
- **冷热双罐储能**：热量与冷量是不同性质的储量，分别存放、互不抵销——设备切换冷热用途
  时两罐各自保持。低需求时设备富余能力蓄能（`to_storage_kw`），高需求或限电时释放补缺。
  充放各有效率损耗：罐内增量 < 充入能量、罐内减量 > 放出能量，损耗不能凭空补足。
  `StorageFlow` 明确区分设备当刻产出、储能变化与房间实际获得。
- **两种储能策略**：`comfort`（优先保障舒适，储能可放到空）与 `reserve`
  （保留 `reserve_fraction` 应急储备，被保留部分导致的缺口记 `reserve_preserved`）。
- **计划量 ≠ 实际输出**：`ControlPlan.allocations` 只是“想供多少”；
  `TickResult.delivered` 才是房间真实获得的能量（设备直供 + 储能释放）。
  最短开/停等待、模式切换、故障期间交付为 0，绝不把计划量记成房间能量。
- **临时故障与重新分配**：`set_available(False, t)` 急停单台设备（最短停机从急停时刻计），
  其余设备继续按真实能力供能，缺口记 `equipment_faulted`；恢复后仍须满足最短停机才上线。
- **未满足需求账目**：`TickResult.unmet` 逐条记录 `room_id / mode / requested_kw / unmet_kw / reason`，
  原因包括 `mode_conflict`、`min_off_not_satisfied`、`capacity_exceeded`、
  `equipment_faulted`、`power_capped`、`reserve_preserved`。
- **传感器断流保守状态**：读数超时为 `STALE`、从未上报为 `NEVER`，温度显式为 `None`、
  需求为 `Mode.UNKNOWN`，绝不复用旧读数。
- **状态持久化**：`save_state/restore` 保存每台设备的模式、`state_since`、故障标志，
  以及储能双罐储量、限电时刻表、策略与最近测量时间戳。进程重启后储量、各自等待计时
  与尚未满足的房间需求保持连续，不会被重置或跳过。

## 运行

```bash
.venv/bin/python demo.py
.venv/bin/python -m pytest tests/ -q
```

## 后续扩展

- 照明：新增独立的 `LightingPlan`/执行器，与温度计划在引擎层并列。
- 更真实的房间/气象模型：替换 `simulation.LocalRoomModel`，引擎接口不变。

