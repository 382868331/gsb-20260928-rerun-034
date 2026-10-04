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


def stop_at(equipment, ts):
    """把设备置于 ts 时刻开始的停机等待（用于构造最短停机未满足场景）。"""
    equipment.mode = EquipmentMode.OFF
    equipment.state_since = ts


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
    assert result.allocation_for("r2").mode is Mode.COOL
    assert result.allocation_for("r2").allocated_kw == pytest.approx(0.0)
    r2_state = next(room for room in result.rooms if room.room_id == "r2")
    assert r2_state.demand is Mode.COOL
    assert result.unmet_for("r2").reason == "mode_conflict"
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
    assert result.allocation_for("r1").mode is Mode.HEAT
    assert result.allocation_for("r1").allocated_kw == pytest.approx(0.0)
    assert result.unmet_for("r1").reason == "mode_conflict"
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
    # 最短开机未满足：设备保持 HEAT；房间已到设定点，真实交付为 0（不是凭空停机/供能）。
    assert result.delivered_mode is EquipmentMode.HEAT
    assert result.allocation_for("r1") is None

    assert engine.tick(600.0).applied_mode is EquipmentMode.OFF

    engine.ingest(Measurement("r1", 30.0, 601.0))
    result = engine.tick(601.0)
    assert result.plan.desired_mode is EquipmentMode.COOL
    assert result.applied_mode is EquipmentMode.OFF
    assert result.delivered_mode is EquipmentMode.OFF
    assert result.allocation_for("r1").allocated_kw == pytest.approx(0.0)
    unmet = result.unmet_for("r1")
    assert unmet.reason == "min_off_not_satisfied"
    assert unmet.unmet_kw > 0

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
    assert stale.delivered == ()


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


def test_planned_capacity_is_not_delivered_during_min_off_wait():
    """停机等待期间计划量不得变成房间能量。"""
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, equipment = make_engine(make_spec(min_on=0.0, min_off=300.0), rooms)
    stop_at(equipment, -200.0)
    engine.ingest(Measurement("r1", 16.0, 0.0))

    result = engine.tick(0.0)

    assert result.plan.desired_mode is EquipmentMode.HEAT
    assert result.plan.allocated_for("r1").allocated_kw > 0
    assert result.delivered_mode is EquipmentMode.OFF
    assert result.delivered[0].allocated_kw == pytest.approx(0.0)
    unmet = result.unmet_for("r1")
    assert unmet.reason == "min_off_not_satisfied"
    assert unmet.unmet_kw == pytest.approx(unmet.requested_kw)


def test_multi_room_capacity_contention_reports_real_shortfall():
    rooms = [
        RoomConfig("r1", "房间1", priority=1, setpoint_c=20.0, rated_input_kw=3.0),
        RoomConfig("r2", "房间2", priority=2, setpoint_c=20.0, rated_input_kw=3.0),
        RoomConfig("r3", "客厅", priority=3, setpoint_c=20.0, rated_input_kw=3.0),
    ]
    engine, _ = make_engine(make_spec(cool=4.0), rooms)
    for rid, temp in (("r1", 26.0), ("r2", 25.0), ("r3", 24.0)):
        engine.ingest(Measurement(rid, temp, 0.0))

    result = engine.tick(0.0)

    delivered = {a.room_id: a.allocated_kw for a in result.delivered}
    assert delivered["r1"] == pytest.approx(3.0)
    assert delivered["r2"] == pytest.approx(1.0)
    # r3 在能力边界上拿到 0，必须留下原因而不是凭空得到冷量。
    assert delivered["r3"] == pytest.approx(0.0)
    unmet = result.unmet_for("r3")
    assert unmet.reason == "capacity_exceeded"
    assert unmet.unmet_kw > 0
    assert sum(a.allocated_kw for a in result.delivered) <= 4.0 + 1e-9


def test_mode_switch_waits_off_then_serves_other_mode():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, _ = make_engine(make_spec(min_on=600.0, min_off=300.0), rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    assert engine.tick(0.0).delivered_mode is EquipmentMode.HEAT

    engine.ingest(Measurement("r1", 30.0, 600.0))
    switching = engine.tick(600.0)
    # 达到最短开机后先回 OFF；此刻不能直接制冷。
    assert switching.applied_mode is EquipmentMode.OFF
    assert switching.delivered_mode is EquipmentMode.OFF
    assert switching.unmet_for("r1").reason == "min_off_not_satisfied"

    waiting = engine.tick(750.0)
    assert waiting.delivered_mode is EquipmentMode.OFF
    assert waiting.lockout_remaining_s == pytest.approx(150.0)

    engine.ingest(Measurement("r1", 30.0, 900.0))
    ready = engine.tick(900.0)
    assert ready.delivered_mode is EquipmentMode.COOL
    assert ready.allocation_for("r1").allocated_kw > 0


def test_fault_forces_off_and_recovery_respects_min_off():
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=22.0),
    ]
    engine, equipment = make_engine(make_spec(min_on=600.0, min_off=300.0), rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    engine.ingest(Measurement("r2", 16.0, 0.0))
    assert engine.tick(0.0).delivered_mode is EquipmentMode.HEAT

    # 300s 时设备临时故障：急停，任何计划量都不能交付。
    engine.ingest(Measurement("r1", 16.0, 300.0))
    engine.ingest(Measurement("r2", 16.0, 300.0))
    equipment.set_available(False, 300.0)
    faulted = engine.tick(300.0)
    assert faulted.applied_mode is EquipmentMode.OFF
    assert faulted.delivered_mode is EquipmentMode.OFF
    assert {u.reason for u in faulted.unmet} == {"equipment_faulted"}
    assert sum(u.unmet_kw for u in faulted.unmet) > 0

    # 360s 故障恢复：最短停机（从 300s 急停计）未结束，仍不能启动。
    equipment.set_available(True, 360.0)
    engine.ingest(Measurement("r1", 16.0, 360.0))
    engine.ingest(Measurement("r2", 16.0, 360.0))
    recovered_early = engine.tick(360.0)
    assert recovered_early.delivered_mode is EquipmentMode.OFF
    assert recovered_early.lockout_remaining_s == pytest.approx(240.0)

    # 600s 等待结束，需求重新分配给各房间（读数须新鲜）。
    engine.ingest(Measurement("r1", 16.0, 590.0))
    engine.ingest(Measurement("r2", 16.0, 590.0))
    recovered = engine.tick(600.0)
    assert recovered.delivered_mode is EquipmentMode.HEAT
    assert {a.room_id for a in recovered.delivered} == {"r1", "r2"}
    assert all(a.allocated_kw > 0 for a in recovered.delivered)
    assert {
        u.reason for u in recovered.unmet
    }.issubset({"capacity_exceeded"})


def test_energy_accounting_uses_only_delivered_power():
    from building_control.simulation import LocalRoomModel

    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    engine, equipment = make_engine(make_spec(min_off=300.0), rooms)
    stop_at(equipment, -100.0)
    sim = LocalRoomModel("r1", temperature_c=16.0, ambient_c=16.0)
    before = sim.temperature_c

    # 设备处于最短停机等待：计划制热但实际交付 0。
    engine.ingest(Measurement("r1", 16.0, 0.0))
    result = engine.tick(0.0)
    assert result.delivered[0].allocated_kw == pytest.approx(0.0)
    delivered = result.allocation_for("r1")
    heat = delivered.allocated_kw
    sim.step(60.0, heat_input_kw=heat)
    assert sim.temperature_c == pytest.approx(before)


def test_faulted_state_persists_and_wait_cannot_be_bypassed(tmp_path):
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0)]
    spec = make_spec(min_on=0.0, min_off=300.0)
    engine, equipment = make_engine(spec, rooms)
    engine.ingest(Measurement("r1", 16.0, 0.0))
    assert engine.tick(0.0).delivered_mode is EquipmentMode.HEAT
    equipment.set_available(False, 120.0)
    engine.save_state(tmp_path / "state.json")

    new_equipment = HeatPump(spec, now=-10_000.0)
    restored = BuildingEngine.restore(tmp_path / "state.json", rooms, new_equipment)
    assert new_equipment.faulted is True
    assert new_equipment.mode is EquipmentMode.OFF
    assert new_equipment.state_since == pytest.approx(120.0)

    new_equipment.set_available(True, 120.0)
    restored.ingest(Measurement("r1", 16.0, 240.0))
    result = restored.tick(240.0)
    assert result.delivered_mode is EquipmentMode.OFF
    assert result.lockout_remaining_s == pytest.approx(180.0)
