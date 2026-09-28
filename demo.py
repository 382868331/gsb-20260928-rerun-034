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
        RoomConfig(room_id="r1", name="主卧", priority=1, setpoint_c=22.0),
        RoomConfig(room_id="r2", name="客厅", priority=2, setpoint_c=22.0),
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
        "r1": LocalRoomModel("r1", temperature_c=16.0, ambient_c=8.0),
        "r2": LocalRoomModel("r2", temperature_c=30.0, ambient_c=34.0),
    }

    dt_s = 60.0
    for step in range(8):
        t = step * dt_s
        for room_id, sim in sims.items():
            engine.ingest(Measurement(room_id, sim.temperature_c, t))
        result = engine.tick(t)

        for room_id, sim in sims.items():
            allocation = result.allocation_for(room_id)
            heat = allocation.allocated_kw if allocation and allocation.mode is Mode.HEAT else 0.0
            cool = allocation.allocated_kw if allocation and allocation.mode is Mode.COOL else 0.0
            sim.step(dt_s, heat_input_kw=heat, cool_input_kw=cool)

        plan_mode = result.plan.desired_mode.value
        actual_mode = result.applied_mode.value
        flag = ""
        if plan_mode != actual_mode:
            flag = f"  计划={plan_mode} 受最短开停约束保持实际={actual_mode}"
        temps = ", ".join(f"{rid}={sim.temperature_c:.2f}℃" for rid, sim in sims.items())
        print(f"t={t:6.0f}s 设备={actual_mode:4s} {temps}{flag}")
        for note in result.plan.notes:
            print(f"         仲裁: {note}")


if __name__ == "__main__":
    main()
