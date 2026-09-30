import pytest

from building_control import (
    BuildingEngine,
    EquipmentMode,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    RoomConfig,
    SensorStatus,
)
from building_control.simulation import LocalRoomModel


def make_spec(min_on=600.0, min_off=300.0, heat=5.0, cool=5.0):
    return HeatPumpSpec(
        name="hp1",
        heating_capacity_kw=heat,
        cooling_capacity_kw=cool,
        min_on_s=min_on,
        min_off_s=min_off,
    )


def make_engine(spec, rooms, stale_after=30.0, equipment_now=-10_000.0):
    equipment = HeatPump(spec, now=equipment_now)
    return BuildingEngine(rooms, equipment, sensor_stale_after_s=stale_after), equipment


def ingest_all(engine, temperatures, timestamp):
    for room_id, temperature in temperatures.items():
        engine.ingest(Measurement(room_id, temperature, timestamp))


def test_conflicting_heat_cool_priority_decides_mode():
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=22.0),
    ]
    engine, equipment = make_engine(make_spec(), rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    engine.ingest(Measurement("r2", 30.0, 0.0))

    result = engine.tick(0.0)

    assert result.plan.desired_mode is EquipmentMode.HEAT
    assert result.applied_mode is EquipmentMode.HEAT
    assert result.allocation_for("r1").allocated_kw > 0
    assert result.allocation_for("r2") is None
    r2_state = next(room for room in result.rooms if room.room_id == "r2")
    assert r2_state.demand is Mode.COOL
    assert "模式冲突" in result.plan.notes[0]


def test_lower_priority_room_can_win_when_it_ranks_first():
    rooms = [
        RoomConfig("r1", "主卧", priority=2, setpoint_c=22.0),
        RoomConfig("r2", "客厅", priority=1, setpoint_c=22.0),
    ]
    engine, _ = make_engine(make_spec(), rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    engine.ingest(Measurement("r2", 30.0, 0.0))

    result = engine.tick(0.0)
    assert result.plan.desired_mode is EquipmentMode.COOL
    assert result.allocation_for("r1") is None
    assert result.allocation_for("r2").allocated_kw > 0


def test_limited_capacity_allocated_by_priority():
    rooms = [
        RoomConfig("r1", "房间1", priority=1, setpoint_c=20.0),
        RoomConfig("r2", "房间2", priority=2, setpoint_c=20.0),
        RoomConfig("r3", "房间3", priority=3, setpoint_c=20.0),
    ]
    engine, _ = make_engine(make_spec(cool=4.0), rooms)
    engine.ingest(Measurement("r1", 26.0, 0.0))
    engine.ingest(Measurement("r2", 25.0, 0.0))
    engine.ingest(Measurement("r3", 24.0, 0.0))

    result = engine.tick(0.0)

    assert result.allocation_for("r1").allocated_kw == pytest.approx(3.0)
    assert result.allocation_for("r2").allocated_kw == pytest.approx(1.0)
    assert result.allocation_for("r3").allocated_kw == pytest.approx(0.0)
    total = sum(a.allocated_kw for a in result.plan.allocations)
    assert total <= 4.0 + 1e-9
    deliveries = {delivery.room_id: delivery for delivery in result.deliveries}
    assert deliveries["r1"].delivered_kw == pytest.approx(3.0)
    assert deliveries["r2"].delivered_kw == pytest.approx(1.0)
    assert deliveries["r3"].delivered_kw == pytest.approx(0.0)
    assert any("能力不足" in reason for reason in deliveries["r3"].reasons)


def test_min_off_wait_does_not_deliver_planned_cooling():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, equipment = make_engine(
        make_spec(min_on=600.0, min_off=300.0), rooms, equipment_now=0.0
    )
    ingest_all(engine, {"r1": 30.0}, 0.0)

    result = engine.tick(0.0)

    assert result.plan.desired_mode is EquipmentMode.COOL
    assert result.allocation_for("r1").allocated_kw > 0.0
    assert result.applied_mode is EquipmentMode.OFF
    delivery = result.delivery_for("r1")
    assert delivery.planned_kw > 0.0
    assert delivery.delivered_kw == pytest.approx(0.0)
    assert "最短停机等待" in delivery.reasons[0]
    assert equipment.mode is EquipmentMode.OFF


def test_mode_switch_wait_keeps_opposite_room_energy_at_zero():
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=22.0),
    ]
    engine, _ = make_engine(make_spec(min_on=600.0, min_off=300.0), rooms)
    ingest_all(engine, {"r1": 16.0, "r2": 30.0}, 0.0)
    first = engine.tick(0.0)

    assert first.delivery_for("r1").delivered_kw > 0.0
    assert first.delivery_for("r2").delivered_kw == pytest.approx(0.0)

    ingest_all(engine, {"r1": 22.0, "r2": 30.0}, 600.0)
    switched = engine.tick(600.0)
    assert switched.plan.desired_mode is EquipmentMode.COOL
    assert switched.applied_mode is EquipmentMode.OFF
    assert switched.delivery_for("r2").planned_kw > 0.0
    assert switched.delivery_for("r2").delivered_kw == pytest.approx(0.0)

    ingest_all(engine, {"r1": 22.0, "r2": 30.0}, 900.0)
    recovered = engine.tick(900.0)
    assert recovered.applied_mode is EquipmentMode.COOL
    assert recovered.delivery_for("r2").delivered_kw > 0.0


def test_min_on_time_blocks_early_shutdown_and_mode_flip():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, equipment = make_engine(make_spec(min_on=600.0, min_off=300.0), rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    assert engine.tick(0.0).applied_mode is EquipmentMode.HEAT

    engine.ingest(Measurement("r1", 22.0, 300.0))
    result = engine.tick(300.0)
    assert result.plan.desired_mode is EquipmentMode.OFF
    assert result.applied_mode is EquipmentMode.HEAT
    assert result.lockout_remaining_s == pytest.approx(300.0)

    assert engine.tick(600.0).applied_mode is EquipmentMode.OFF

    engine.ingest(Measurement("r1", 30.0, 601.0))
    result = engine.tick(601.0)
    assert result.plan.desired_mode is EquipmentMode.COOL
    assert result.applied_mode is EquipmentMode.OFF

    assert equipment.allow(EquipmentMode.COOL, 0.0) is False


def test_fault_outputs_zero_and_reallocates_after_min_off_wait():
    rooms = [
        RoomConfig("r1", "房间1", priority=1, setpoint_c=20.0),
        RoomConfig("r2", "房间2", priority=2, setpoint_c=20.0),
    ]
    engine, equipment = make_engine(
        make_spec(min_on=600.0, min_off=300.0, heat=4.0), rooms
    )
    ingest_all(engine, {"r1": 16.0, "r2": 15.0}, 0.0)
    assert engine.tick(0.0).delivery_for("r1").delivered_kw > 0.0

    equipment.report_fault(until=240.0, now=120.0)
    ingest_all(engine, {"r1": 16.0, "r2": 15.0}, 120.0)
    faulty = engine.tick(120.0)
    assert faulty.plan.desired_mode is EquipmentMode.HEAT
    assert faulty.applied_mode is EquipmentMode.OFF
    assert faulty.equipment_available is False
    assert faulty.delivery_for("r1").planned_kw > 0.0
    assert faulty.delivery_for("r1").delivered_kw == pytest.approx(0.0)
    assert "故障" in faulty.delivery_for("r1").reasons[0]

    ingest_all(engine, {"r1": 16.0, "r2": 15.0}, 240.0)
    recovered = engine.tick(240.0)
    assert recovered.equipment_available is True
    assert recovered.applied_mode is EquipmentMode.OFF
    assert recovered.delivery_for("r1").delivered_kw == pytest.approx(0.0)
    assert "最短停机等待" in recovered.delivery_for("r1").reasons[0]

    ingest_all(engine, {"r1": 16.0, "r2": 15.0}, 420.0)
    reallocated = engine.tick(420.0)
    assert reallocated.applied_mode is EquipmentMode.HEAT
    assert reallocated.delivery_for("r1").delivered_kw == pytest.approx(3.0)
    assert reallocated.delivery_for("r2").delivered_kw == pytest.approx(1.0)


def test_restore_active_fault_keeps_unavailability_and_wait(tmp_path):
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    spec = make_spec(min_on=600.0, min_off=300.0)
    engine, equipment = make_engine(spec, rooms)
    ingest_all(engine, {"r1": 16.0}, 0.0)
    engine.tick(0.0)
    equipment.report_fault(until=240.0, now=120.0)
    engine.tick(120.0)
    state_file = tmp_path / "state.json"
    engine.save_state(state_file)

    restored_equipment = HeatPump(spec, now=-10_000.0)
    restored = BuildingEngine.restore(state_file, rooms, restored_equipment)
    ingest_all(restored, {"r1": 16.0}, 180.0)
    faulty = restored.tick(180.0)
    assert faulty.equipment_available is False
    assert faulty.applied_mode is EquipmentMode.OFF
    assert faulty.delivery_for("r1").delivered_kw == pytest.approx(0.0)

    ingest_all(restored, {"r1": 16.0}, 240.0)
    waiting = restored.tick(240.0)
    assert waiting.equipment_available is True
    assert waiting.applied_mode is EquipmentMode.OFF
    assert waiting.delivery_for("r1").delivered_kw == pytest.approx(0.0)
    assert "最短停机等待" in waiting.delivery_for("r1").reasons[0]

    ingest_all(restored, {"r1": 16.0}, 420.0)
    assert restored.tick(420.0).applied_mode is EquipmentMode.HEAT


def test_end_to_end_temperature_uses_only_delivered_energy():
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0, rated_input_kw=3.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig("r3", "书房", priority=3, setpoint_c=22.0, rated_input_kw=3.0),
    ]
    spec = make_spec(min_on=600.0, min_off=300.0, heat=4.0, cool=5.0)
    engine, equipment = make_engine(spec, rooms)
    sims = {
        "r1": LocalRoomModel("r1", 16.0, ambient_c=22.0),
        "r2": LocalRoomModel("r2", 30.0, ambient_c=30.0),
        "r3": LocalRoomModel("r3", 15.0, ambient_c=22.0),
    }

    def step(timestamp, temperatures=None):
        values = temperatures or {
            room_id: sim.temperature_c for room_id, sim in sims.items()
        }
        ingest_all(engine, values, timestamp)
        result = engine.tick(timestamp)
        for room_id, sim in sims.items():
            delivery = result.delivery_for(room_id)
            heat = delivery.delivered_kw if delivery and delivery.mode is Mode.HEAT else 0.0
            cool = delivery.delivered_kw if delivery and delivery.mode is Mode.COOL else 0.0
            sim.step(60.0, heat, cool)
        return result

    first = step(0.0)
    assert first.applied_mode is EquipmentMode.HEAT
    assert first.delivery_for("r1").delivered_kw == pytest.approx(3.0)
    assert first.delivery_for("r3").delivered_kw == pytest.approx(1.0)
    assert sims["r3"].heating_energy_kwh > 0.0
    assert sims["r2"].cooling_energy_kwh == pytest.approx(0.0)

    for timestamp in range(60, 600, 60):
        step(float(timestamp))

    waiting_switch = step(600.0, {"r1": 22.0, "r2": 30.0, "r3": 22.0})
    assert waiting_switch.applied_mode is EquipmentMode.OFF
    assert waiting_switch.delivery_for("r2").delivered_kw == pytest.approx(0.0)
    r2_before_wait = sims["r2"].temperature_c

    step(660.0, {"r1": 22.0, "r2": 30.0, "r3": 22.0})
    assert sims["r2"].temperature_c == pytest.approx(r2_before_wait)
    cooling = step(900.0, {"r1": 22.0, "r2": 30.0, "r3": 22.0})
    assert cooling.delivery_for("r2").delivered_kw == pytest.approx(5.0)
    cooled_temperature = sims["r2"].temperature_c
    assert cooled_temperature < 30.0

    equipment.report_fault(until=1080.0, now=960.0)
    faulty = step(960.0, {"r1": 22.0, "r2": 30.0, "r3": 22.0})
    energy_at_fault = sims["r2"].cooling_energy_kwh
    assert faulty.delivery_for("r2").delivered_kw == pytest.approx(0.0)
    assert "故障" in faulty.delivery_for("r2").reasons[0]

    recovered_waiting = step(1080.0, {"r1": 22.0, "r2": 30.0, "r3": 22.0})
    assert sims["r2"].cooling_energy_kwh == pytest.approx(energy_at_fault)
    assert "最短停机等待" in recovered_waiting.delivery_for("r2").reasons[0]

    restarted = step(1260.0, {"r1": 22.0, "r2": 30.0, "r3": 22.0})
    assert restarted.applied_mode is EquipmentMode.COOL
    assert restarted.delivery_for("r2").delivered_kw == pytest.approx(5.0)
    assert sims["r2"].cooling_energy_kwh > energy_at_fault


def test_stale_sensor_enters_conservative_state():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, _ = make_engine(make_spec(min_on=0.0, min_off=0.0), rooms, stale_after=30.0)
    engine.ingest(Measurement("r1", 30.0, 0.0))
    fresh = engine.tick(0.0)
    assert fresh.applied_mode is EquipmentMode.COOL

    stale = engine.tick(31.0)
    room = stale.rooms[0]
    assert room.sensor_status is SensorStatus.STALE
    assert room.temperature_c is None
    assert room.demand is Mode.UNKNOWN
    assert stale.degraded is True
    assert stale.conservative_shutdown is True
    assert stale.plan.desired_mode is EquipmentMode.OFF
    assert stale.applied_mode is EquipmentMode.OFF


def test_never_seen_sensor_is_conservative_unknown():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, _ = make_engine(make_spec(min_on=0.0, min_off=0.0), rooms)
    result = engine.tick(5.0)
    room = result.rooms[0]
    assert room.sensor_status is SensorStatus.NEVER
    assert room.temperature_c is None
    assert room.demand is Mode.UNKNOWN
    assert result.applied_mode is EquipmentMode.OFF


def test_save_and_restore_does_not_skip_wait_time(tmp_path):
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    spec = make_spec(min_on=600.0, min_off=300.0)
    engine, _ = make_engine(spec, rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    assert engine.tick(0.0).applied_mode is EquipmentMode.HEAT
    state_file = tmp_path / "state.json"
    engine.save_state(state_file)

    new_equipment = HeatPump(spec, now=-10_000.0)
    restored = BuildingEngine.restore(state_file, rooms, new_equipment)
    assert new_equipment.mode is EquipmentMode.HEAT
    assert new_equipment.state_since == pytest.approx(0.0)

    restored.ingest(Measurement("r1", 30.0, 300.0))
    result = restored.tick(300.0)
    assert result.plan.desired_mode is EquipmentMode.COOL
    assert result.applied_mode is EquipmentMode.HEAT
    assert result.lockout_remaining_s == pytest.approx(300.0)

    assert restored.tick(600.0).applied_mode is EquipmentMode.OFF
    assert restored.tick(700.0).applied_mode is EquipmentMode.OFF
    restored.ingest(Measurement("r1", 30.0, 890.0))
    assert restored.tick(900.0).applied_mode is EquipmentMode.COOL

    # 恢复后断流计时同样保留：910s 读数新鲜，930s 断流。
    assert restored.tick(910.0).rooms[0].sensor_status is SensorStatus.FRESH
    assert restored.tick(930.0).rooms[0].sensor_status is SensorStatus.STALE
