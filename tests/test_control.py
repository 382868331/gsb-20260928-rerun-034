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
