"""端到端演示：两台能力不同的设备 + 冷热储能 + 限电 + 故障恢复 + 双策略对账。

场景（夏季制冷）：
- 三个房间共用 hp1（5kW）与 hp2（3kW）两台冷热设备，各自有最短开/停等待。
- 一座冷热双仓缓冲储能：低需求蓄冷，高需求/限电时放冷，充放均有损耗。
- 600s~1500s 站点限电 4kW，正好压在高需求时段上。
- 300s 时 hp2 临时故障，480s 恢复，但最短停机未满仍要等。
- 同一剧本分别用「舒适优先」与「保留应急储备」两种策略运行，对比账目。
- 中途保存状态并重开，验证储量、等待计时与未满足需求保持连续。
"""

from building_control import (
    BuildingEngine,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    PowerSchedule,
    RoomConfig,
    StoragePolicy,
    StorageSpec,
    ThermalStorage,
)
from building_control.simulation import LocalRoomModel

DT_S = 60.0
STEPS = 30  # 30 分钟
STATE_FILE = "/tmp/building_engine_state.json"


def build(policy: StoragePolicy):
    rooms = [
        RoomConfig(room_id="r1", name="主卧", priority=1, setpoint_c=24.0),
        RoomConfig(room_id="r2", name="客厅", priority=2, setpoint_c=24.0),
        RoomConfig(room_id="r3", name="书房", priority=3, setpoint_c=24.0),
    ]
    devices = [
        HeatPump(HeatPumpSpec("hp1", 5.0, 5.0, min_on_s=300.0, min_off_s=180.0), now=-10_000.0),
        HeatPump(HeatPumpSpec("hp2", 3.0, 3.0, min_on_s=600.0, min_off_s=300.0), now=-10_000.0),
    ]
    storage = ThermalStorage(
        StorageSpec(
            name="缓冲罐",
            heat_capacity_kwh=8.0,
            cool_capacity_kwh=8.0,
            max_charge_kw=4.0,
            max_discharge_kw=4.0,
            charge_efficiency=0.9,
            discharge_efficiency=0.9,
        )
    )
    schedule = PowerSchedule(segments=[(600.0, 4.0), (1500.0, 100.0)])
    engine = BuildingEngine(
        rooms,
        devices,
        sensor_stale_after_s=120.0,
        storage=storage,
        power_schedule=schedule,
        storage_policy=policy,
        reserve_fraction=0.5,
    )
    sims = {
        "r1": LocalRoomModel("r1", temperature_c=24.2, ambient_c=33.0),
        "r2": LocalRoomModel("r2", temperature_c=24.4, ambient_c=33.0),
        "r3": LocalRoomModel("r3", temperature_c=24.3, ambient_c=33.0),
    }
    return rooms, devices, storage, engine, sims


def run(policy: StoragePolicy, label: str) -> None:
    print(f"\n===== 策略：{label} =====")
    rooms, devices, storage, engine, sims = build(policy)
    restored = False
    for step in range(STEPS):
        t = step * DT_S
        if step == 5:  # 300s：hp2 临时故障
            devices[1].set_available(False, t)
        if step == 8:  # 480s：故障恢复（最短停机从急停计，仍须等待）
            devices[1].set_available(True, t)
        if step == 6:  # 360s 起室外热浪，需求冲高
            for sim in sims.values():
                sim.ambient_c = 38.0
        if step == 12 and not restored:  # 720s：保存并重开，状态连续
            engine.save_state(STATE_FILE)
            new_devices = [
                HeatPump(HeatPumpSpec("hp1", 5.0, 5.0, 300.0, 180.0), now=-10_000.0),
                HeatPump(HeatPumpSpec("hp2", 3.0, 3.0, 600.0, 300.0), now=-10_000.0),
            ]
            new_storage = ThermalStorage(storage.spec)
            engine = BuildingEngine.restore(
                STATE_FILE, rooms, new_devices, storage=new_storage
            )
            devices = new_devices
            restored = True
            print(f"  --- t={t:5.0f}s 状态已保存并重开（储量/等待/未满足账目连续） ---")

        for room_id, sim in sims.items():
            engine.ingest(Measurement(room_id, sim.temperature_c, t))
        result = engine.tick(t)

        for room_id, sim in sims.items():
            delivered = result.allocation_for(room_id)
            cool = delivered.allocated_kw if delivered and delivered.mode is Mode.COOL else 0.0
            heat = delivered.allocated_kw if delivered and delivered.mode is Mode.HEAT else 0.0
            sim.step(DT_S, heat_input_kw=heat, cool_input_kw=cool)

        dev_text = " ".join(
            f"{info.device_id}={info.applied_mode.value}:{info.delivered_kw:.1f}kW"
            + (f"(等{info.lockout_remaining_s:.0f}s)" if info.lockout_remaining_s > 0 else "")
            + ("(故障)" if info.faulted else "")
            for info in result.devices
        )
        sto = result.storage
        sto_text = (
            f"蓄{sto.charge_kw:.1f}/放{sto.discharge_kw:.1f}kW 冷仓{sto.cool_kwh:.2f}kWh"
            if sto
            else ""
        )
        cap = f"限电{result.power_cap_kw:.0f}kW" if result.power_cap_kw < 90 else ""
        temps = " ".join(f"{rid}={sim.temperature_c:.1f}℃" for rid, sim in sims.items())
        print(f"t={t:5.0f}s {dev_text} | {sto_text} {cap} | {temps}")
        for item in result.unmet:
            print(
                f"    未满足 {item.room_id}: 缺 {item.unmet_kw:.1f}kW（{item.reason}）"
            )

    ledger = engine.energy_ledger()
    print(f"  设备累计供能: {ledger['devices_kwh']}")
    print(
        f"  储能累计: 充 {ledger['storage_charge_kwh']:.2f} kWh，"
        f"放 {ledger['storage_discharge_kwh']:.2f} kWh，期末冷仓 {storage.cool_kwh:.2f} kWh"
    )
    print(f"  未满足累计: {ledger['unmet_rooms_kwh']}")


def main() -> None:
    run(StoragePolicy.COMFORT_FIRST, "舒适优先（储能可放空）")
    run(StoragePolicy.PRESERVE_RESERVE, "保留应急储备（50% 下限）")


if __name__ == "__main__":
    main()

