"""端到端演示：冷热需求相反的两个房间共用一台冷暖设备。"""

from building_control import (
    BuildingEngine,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    RoomConfig,
)
from building_control.simulation import LocalRoomModel


def main() -> None:
    rooms = [
        RoomConfig(room_id="r1", name="主卧", priority=1, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig(room_id="r2", name="客厅", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
    ]
    spec = HeatPumpSpec(
        name="屋顶热泵-1",
        heating_capacity_kw=5.0,
        cooling_capacity_kw=5.0,
        min_on_s=600.0,
        min_off_s=300.0,
    )
    equipment = HeatPump(spec, now=-10_000.0)
    engine = BuildingEngine(rooms, equipment, sensor_stale_after_s=120.0)

    sims = {
        "r1": LocalRoomModel(
            "r1", temperature_c=16.0, ambient_c=20.0, thermal_capacity_kj_per_c=500.0
        ),
        "r2": LocalRoomModel(
            "r2", temperature_c=30.0, ambient_c=34.0, thermal_capacity_kj_per_c=500.0
        ),
    }

    dt_s = 60.0
    for step in range(22):
        t = step * dt_s
        if t == 960.0:
            equipment.report_fault(until=1080.0, now=t)

        for index, (room_id, sim) in enumerate(sims.items()):
            temperature = 16.0 if room_id == "r1" and t < 600.0 else (
                22.0 if room_id == "r1" else 30.0
            )
            engine.ingest(Measurement(room_id, temperature, t))
        result = engine.tick(t)

        for room_id, sim in sims.items():
            delivery = result.delivery_for(room_id)
            heat = delivery.delivered_kw if delivery and delivery.mode is Mode.HEAT else 0.0
            cool = delivery.delivered_kw if delivery and delivery.mode is Mode.COOL else 0.0
            sim.step(dt_s, heat_input_kw=heat, cool_input_kw=cool)

        plan_mode = result.plan.desired_mode.value
        actual_mode = result.applied_mode.value
        flag = ""
        if plan_mode != actual_mode:
            flag = f"  计划={plan_mode} 受最短开停约束保持实际={actual_mode}"
        if not result.equipment_available:
            flag += f"  故障剩余={result.unavailable_remaining_s:.0f}s，计划供能不落地"
        temps = ", ".join(f"{rid}={sim.temperature_c:.2f}℃" for rid, sim in sims.items())
        print(f"t={t:6.0f}s 设备={actual_mode:4s} {temps}{flag}")
        for note in result.plan.notes:
            print(f"         仲裁: {note}")
        for reason in result.unmet_reasons:
            print(f"         未满足: {reason}")

    print(
        "能量账目: "
        f"r1 制热={sims['r1'].heating_energy_kwh:.2f}kWh, "
        f"r2 制冷={sims['r2'].cooling_energy_kwh:.2f}kWh"
    )


if __name__ == "__main__":
    main()
