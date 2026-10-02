"""端到端演示：两台能力不同的冷热设备 + 冷热双罐储能 + 限电，共同服务三个房间。

场景（夏季制冷，步长 60s，共 90 分钟）：
- 0~20min   低需求：设备富余能力给储能罐蓄冷；
- 20~50min  站点限电 4kW 且室外升温（高需求）：储能释放补缺；
- 50min     主设备 hp1 临时故障，70min 恢复（最短停机等待后再上线）；
- 30min     模拟进程重启：存档后换新设备/储能对象恢复，账目保持连续；
- 最后对比“舒适优先”与“保留应急储备”两种策略的累计未满足能量。
"""

from building_control import (
    STRATEGY_COMFORT,
    STRATEGY_RESERVE,
    BuildingEngine,
    EquipmentMode,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    PowerSchedule,
    RoomConfig,
    ThermalStorage,
)
from building_control.simulation import LocalRoomModel

DT_S = 60.0
STEPS = 90
STATE_FILE = "/tmp/building_engine_state.json"


def build(strategy):
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=24.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=24.0),
        RoomConfig("r3", "书房", priority=3, setpoint_c=24.0),
    ]
    devices = [
        HeatPump(HeatPumpSpec("hp1-大", 6.0, 6.0, min_on_s=300.0, min_off_s=180.0), now=-10_000.0),
        HeatPump(HeatPumpSpec("hp2-小", 3.0, 3.0, min_on_s=240.0, min_off_s=120.0), now=-10_000.0),
    ]
    schedule = PowerSchedule(segments=[(20 * 60.0, 4.0), (50 * 60.0, 1e9)])
    storage = ThermalStorage(
        capacity_kwh=6.0,
        max_charge_kw=3.0,
        max_discharge_kw=3.0,
        charge_efficiency=0.9,
        discharge_efficiency=0.9,
        cool_kwh=4.0,
    )
    engine = BuildingEngine(
        rooms,
        devices,
        sensor_stale_after_s=180.0,
        power_schedule=schedule,
        storage=storage,
        strategy=strategy,
        reserve_fraction=0.5,
    )
    sims = {
        "r1": LocalRoomModel("r1", temperature_c=26.0, ambient_c=33.0),
        "r2": LocalRoomModel("r2", temperature_c=26.5, ambient_c=33.0),
        "r3": LocalRoomModel("r3", temperature_c=25.0, ambient_c=33.0),
    }
    return engine, sims


def run(strategy):
    engine, sims = build(strategy)
    unmet_kwh = 0.0
    print(f"\n===== 策略：{strategy} =====")
    for step in range(STEPS):
        t = step * DT_S
        if step == 20:
            for sim in sims.values():  # 高需求时段开始：室外升温
                sim.ambient_c = 38.0
        if step == 50:
            engine.devices[0].set_available(False, t)
            print(f"t={t/60:5.1f}min  *** hp1 临时故障，急停 ***")
        if step == 70:
            engine.devices[0].set_available(True, t)
            print(f"t={t/60:5.1f}min  *** hp1 故障恢复（仍需最短停机等待） ***")
        if step == 30:
            engine.save_state(STATE_FILE)
            fresh, _ = build(strategy)
            engine = BuildingEngine.restore(
                STATE_FILE, fresh.rooms, fresh.devices, storage=fresh.storage
            )
            print(f"t={t/60:5.1f}min  *** 进程重启：储量/等待计时/未满足账目从存档恢复 ***")

        for room_id, sim in sims.items():
            engine.ingest(Measurement(room_id, sim.temperature_c, t))
        result = engine.tick(t, dt_s=DT_S)

        for room_id, sim in sims.items():
            delivered = result.allocation_for(room_id)
            cool = delivered.allocated_kw if delivered and delivered.mode is Mode.COOL else 0.0
            sim.step(DT_S, cool_input_kw=cool)
        unmet_kwh += sum(item.unmet_kw for item in result.unmet) * DT_S / 3600.0

        if step % 5 == 0 or step in (49, 50, 69, 70, 71):
            devs = " ".join(
                f"{d.device_id}={d.delivered_mode.value}:{d.output_kw:.1f}kW"
                + (f"(储{d.to_storage_kw:.1f})" if d.to_storage_kw > 1e-9 else "")
                + ("(故障)" if d.faulted else "")
                for d in result.device_deliveries
            )
            flow = result.storage_flow
            store = (
                f"罐[热{flow.heat_stored_kwh:.2f}/冷{flow.cool_stored_kwh:.2f}kWh"
                f" 充{flow.charge_cool_kw:.1f}/放{flow.discharge_cool_kw:.1f}kW]"
                if flow
                else ""
            )
            temps = " ".join(f"{rid}={sim.temperature_c:.1f}℃" for rid, sim in sims.items())
            cap = result.power_cap_kw
            cap_text = f"限电{cap:.0f}kW" if cap < 1e8 else "不限电"
            print(f"t={t/60:5.1f}min {cap_text} {temps} | {devs} | {store}")
            for item in result.unmet:
                print(
                    f"          未满足: {item.room_id} 缺 {item.unmet_kw:.1f}kW（{item.reason}）"
                )
    print(f"累计未满足能量：{unmet_kwh:.2f} kWh")
    return unmet_kwh


def main() -> None:
    comfort_unmet = run(STRATEGY_COMFORT)
    reserve_unmet = run(STRATEGY_RESERVE)
    print("\n===== 策略对比 =====")
    print(f"舒适优先   累计未满足 {comfort_unmet:.2f} kWh（储能尽量放出保舒适）")
    print(f"保留储备   累计未满足 {reserve_unmet:.2f} kWh（始终保留 50% 应急冷量）")


if __name__ == "__main__":
    main()
