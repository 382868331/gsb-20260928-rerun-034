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
        min_on_s=300.0,
        min_off_s=180.0,
    )
    # 设备一开始处于临时故障，演示故障恢复与重新分配。
    equipment = HeatPump(spec, now=-10_000.0)
    equipment.set_available(False, now=-10_000.0)
    engine = BuildingEngine(rooms, equipment, sensor_stale_after_s=120.0)

    sims = {
        "r1": LocalRoomModel("r1", temperature_c=16.0, ambient_c=8.0),
        "r2": LocalRoomModel("r2", temperature_c=30.0, ambient_c=34.0),
    }

    dt_s = 60.0
    energy_kwh = {"r1": 0.0, "r2": 0.0}
    for step in range(16):
        t = step * dt_s
        if step == 2:  # 120s 故障排除
            equipment.set_available(True, t)
        if step == 9:  # 540s 起主卧也要求制冷，进入 HEAT→OFF 等待→COOL
            sims["r1"].temperature_c = 30.0
            sims["r1"].ambient_c = 34.0
        for room_id, sim in sims.items():
            engine.ingest(Measurement(room_id, sim.temperature_c, t))
        result = engine.tick(t)

        for room_id, sim in sims.items():
            # 只按设备“实际交付”的能量记账，计划量在等待/故障期间为 0。
            delivered = result.allocation_for(room_id)
            heat = delivered.allocated_kw if delivered and delivered.mode is Mode.HEAT else 0.0
            cool = delivered.allocated_kw if delivered and delivered.mode is Mode.COOL else 0.0
            energy_kwh[room_id] += (heat - cool) * dt_s / 3600.0
            sim.step(dt_s, heat_input_kw=heat, cool_input_kw=cool)

        plan_mode = result.plan.desired_mode.value
        actual_mode = result.delivered_mode.value
        flag = ""
        if result.delivered_mode is not result.plan.desired_mode:
            wait = f"，剩余等待 {result.lockout_remaining_s:.0f}s" if result.lockout_remaining_s else ""
            flag = f"  计划={plan_mode} 但实际交付=off（{_reason_text(result)}{wait}）"
        temps = ", ".join(
            f"{rid}={sim.temperature_c:.2f}℃/净能量{energy_kwh[rid]:+.2f}kWh"
            for rid, sim in sims.items()
        )
        print(f"t={t:6.0f}s 设备={actual_mode:4s} {temps}{flag}")
        for item in result.unmet:
            print(
                f"         未满足: {item.room_id} 需{item.mode.value} "
                f"{item.requested_kw:.1f}kW，缺 {item.unmet_kw:.1f}kW（{item.reason}）"
            )


def _reason_text(result) -> str:
    reasons = {item.reason for item in result.unmet}
    if "equipment_faulted" in reasons:
        return "设备临时故障"
    if "min_off_not_satisfied" in reasons:
        return "最短停机/切换等待未结束"
    return "模式冲突或未运行"


if __name__ == "__main__":
    main()
