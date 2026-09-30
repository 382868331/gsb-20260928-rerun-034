"""端到端演示：两台不同能力的设备共同服务多个房间。"""

from building_control import (
    BuildingEngine,
    EquipmentFleet,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    PowerLimit,
    PowerSchedule,
    RoomConfig,
)
from building_control.simulation import LocalRoomModel


def main() -> None:
    rooms = [
        RoomConfig(room_id="r1", name="主卧", priority=1, setpoint_c=22.0),
        RoomConfig(room_id="r2", name="客厅", priority=2, setpoint_c=22.0),
    ]
    specs = [
        HeatPumpSpec(
            device_id="hp-small",
            name="小热泵",
            heating_capacity_kw=2.0,
            cooling_capacity_kw=2.0,
            min_on_s=300.0,
            min_off_s=180.0,
        ),
        HeatPumpSpec(
            device_id="hp-large",
            name="大热泵",
            heating_capacity_kw=4.0,
            cooling_capacity_kw=4.0,
            min_on_s=300.0,
            min_off_s=180.0,
        ),
    ]
    fleet = EquipmentFleet(
        [HeatPump(specs[0], now=-10_000.0), HeatPump(specs[1], now=-20_000.0)]
    )
    schedule = PowerSchedule([PowerLimit(300.0, 600.0, heating_kw=3.0, cooling_kw=3.0)])
    engine = BuildingEngine(rooms, fleet, sensor_stale_after_s=120.0, power_schedule=schedule)

    sims = {
        "r1": LocalRoomModel("r1", temperature_c=16.0, ambient_c=8.0),
        "r2": LocalRoomModel("r2", temperature_c=30.0, ambient_c=34.0),
    }

    dt_s = 60.0
    for step in range(16):
        t = step * dt_s
        if step == 3:
            fleet.set_available(False, t, "hp-large")
        if step == 6:
            fleet.set_available(True, t, "hp-large")
        if step == 9:
            sims["r1"].temperature_c = 30.0
            sims["r1"].ambient_c = 34.0
        for room_id, sim in sims.items():
            engine.ingest(Measurement(room_id, sim.temperature_c, t))
        result = engine.tick(t)

        for room_id, sim in sims.items():
            delivered = result.allocation_for(room_id)
            heat = delivered.allocated_kw if delivered and delivered.mode is Mode.HEAT else 0.0
            cool = delivered.allocated_kw if delivered and delivered.mode is Mode.COOL else 0.0
            sim.step(dt_s, heat_input_kw=heat, cool_input_kw=cool)

        device_text = ", ".join(f"{id_}={mode.value}" for id_, mode in result.device_modes)
        limit_text = f"/{result.total_power_limit_kw:.0f}kW" if result.delivered_mode.running else ""
        accounts = dict(result.energy)
        temps = ", ".join(
            f"{rid}={sim.temperature_c:.2f}℃/累计{accounts[rid].total_kwh:+.2f}kWh"
            if rid in accounts
            else f"{rid}={sim.temperature_c:.2f}℃/累计+0.00kWh"
            for rid, sim in sims.items()
        )
        print(
            f"t={t:6.0f}s 交付={result.delivered_mode.value:4s}{limit_text} "
            f"[{device_text}] {temps}"
        )
        for item in result.unmet:
            print(
                f"         未满足: {item.room_id} 需{item.mode.value} "
                f"{item.requested_kw:.1f}kW，缺 {item.unmet_kw:.1f}kW（{item.reason}）"
            )


if __name__ == "__main__":
    main()
