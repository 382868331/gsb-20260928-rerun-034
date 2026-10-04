"""混合冷热需求：机群分方向协同、单台故障、总量上限与状态恢复的一致性。"""

import pytest

from building_control import (
    BuildingEngine,
    EquipmentMode,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    PowerSchedule,
    RoomConfig,
)


def make_device(device_id, heat, cool, min_on=0.0, min_off=0.0, now=-10_000.0):
    return HeatPump(
        HeatPumpSpec(
            name=device_id,
            heating_capacity_kw=heat,
            cooling_capacity_kw=cool,
            min_on_s=min_on,
            min_off_s=min_off,
        ),
        now=now,
    )


def mixed_rooms(rated=3.0):
    return [
        RoomConfig("cold", "北卧", priority=1, setpoint_c=22.0, rated_input_kw=rated),
        RoomConfig("hot", "南厅", priority=2, setpoint_c=22.0, rated_input_kw=rated),
    ]


def ingest(engine, t, cold_temp=15.0, hot_temp=31.0):
    engine.ingest(Measurement("cold", cold_temp, t))
    engine.ingest(Measurement("hot", hot_temp, t))


def demanding_room_ids(result):
    return {room.room_id for room in result.rooms if room.demand in (Mode.HEAT, Mode.COOL)}


def recorded_room_ids(result):
    return {a.room_id for a in result.delivered} | {u.room_id for u in result.unmet}


def test_fleet_covers_both_directions_with_45_and_35():
    """15℃ 与 31℃、目标 22℃：4.5kW 与 3.5kW 各管一个方向，两分区都有服务记录。"""
    devices = [make_device("hp1", 4.5, 4.5), make_device("hp2", 3.5, 3.5)]
    engine = BuildingEngine(mixed_rooms(), devices)
    ingest(engine, 0.0)

    result = engine.tick(0.0)

    # 方向数量：一台制热、一台制冷，而不是只返回一种全局模式。
    heat_devices = [d for d in result.devices if d.applied_mode is EquipmentMode.HEAT]
    cool_devices = [d for d in result.devices if d.applied_mode is EquipmentMode.COOL]
    assert len(heat_devices) == 1
    assert len(cool_devices) == 1
    # 高优先级制热需求先分到能力大的 hp1（4.5kW），制冷由 hp2（3.5kW）承担。
    assert result.device_info("hp1").applied_mode is EquipmentMode.HEAT
    assert result.device_info("hp2").applied_mode is EquipmentMode.COOL

    # 两个分区都拿到真实交付，任何分区都没有被删掉。
    cold = result.allocation_for("cold")
    hot = result.allocation_for("hot")
    assert cold is not None and cold.mode is Mode.HEAT
    assert hot is not None and hot.mode is Mode.COOL
    assert cold.allocated_kw == pytest.approx(3.0)
    assert hot.allocated_kw == pytest.approx(3.0)
    assert result.unmet == ()
    assert recorded_room_ids(result) == demanding_room_ids(result)

    # 各方向出力不越界：单设备不超自身能力，总量不超机群能力。
    assert result.device_info("hp1").delivered_kw <= 4.5 + 1e-9
    assert result.device_info("hp2").delivered_kw <= 3.5 + 1e-9
    assert sum(a.allocated_kw for a in result.delivered) <= 4.5 + 3.5 + 1e-9


def test_single_device_conflict_keeps_both_zone_records():
    """单设备只能跑一个方向：另一分区保留 mode_conflict 记录，不被删除。"""
    devices = [make_device("hp1", 4.5, 4.5)]
    engine = BuildingEngine(mixed_rooms(), devices)
    ingest(engine, 0.0)

    result = engine.tick(0.0)

    assert result.plan.desired_mode is EquipmentMode.HEAT
    served = result.allocation_for("cold")
    assert served is not None and served.allocated_kw > 0
    assert result.allocation_for("hot") is None
    unmet = result.unmet_for("hot")
    assert unmet is not None
    assert unmet.mode is Mode.COOL
    assert unmet.reason == "mode_conflict"
    assert unmet.unmet_kw == pytest.approx(unmet.requested_kw)
    assert "模式冲突" in result.plan.notes[0]
    # 两个分区都有记录：一个被服务，一个带原因未满足。
    assert recorded_room_ids(result) == {"cold", "hot"}


def test_single_fault_explained_by_remaining_real_capacity():
    """制冷设备故障后：制热照常，制冷分区按剩余真实能力记 equipment_faulted。"""
    devices = [make_device("hp1", 4.5, 4.5), make_device("hp2", 3.5, 3.5)]
    engine = BuildingEngine(mixed_rooms(), devices)
    ingest(engine, 0.0)
    devices[1].set_available(False, 0.0)

    result = engine.tick(0.0)

    assert result.device_info("hp2").faulted is True
    assert result.device_info("hp2").delivered_kw == pytest.approx(0.0)
    # hp1 继续制热，制冷方向只剩故障设备，按真实剩余能力（0）解释。
    cold = result.allocation_for("cold")
    assert cold is not None and cold.allocated_kw == pytest.approx(3.0)
    unmet = result.unmet_for("hot")
    assert unmet is not None
    assert unmet.reason == "equipment_faulted"
    assert unmet.unmet_kw == pytest.approx(unmet.requested_kw)
    assert recorded_room_ids(result) == {"cold", "hot"}
    # 总输出不超过剩余真实能力 4.5kW。
    assert sum(a.allocated_kw for a in result.delivered) <= 4.5 + 1e-9


def test_mixed_mode_total_output_respects_power_cap():
    """冷热同时供给时，站点限电约束的是机群总出力。"""
    rooms = mixed_rooms(rated=5.0)
    devices = [make_device("hp1", 4.5, 4.5), make_device("hp2", 3.5, 3.5)]
    schedule = PowerSchedule(segments=[(0.0, 5.0)])
    engine = BuildingEngine(rooms, devices, power_schedule=schedule)
    ingest(engine, 0.0)

    result = engine.tick(0.0)

    assert result.power_cap_kw == pytest.approx(5.0)
    total = sum(a.allocated_kw for a in result.delivered)
    assert total <= 5.0 + 1e-9
    assert total == pytest.approx(5.0)
    # hp1（4.5）制热用掉 4.5kW，hp2（3.5）只剩 0.5kW 制冷预算。
    assert result.device_info("hp1").delivered_kw == pytest.approx(4.5)
    assert result.device_info("hp2").delivered_kw == pytest.approx(0.5)
    assert result.unmet_for("cold").reason == "power_limited"
    assert result.unmet_for("cold").unmet_kw == pytest.approx(0.5)
    assert result.unmet_for("hot").reason == "power_limited"
    assert result.unmet_for("hot").unmet_kw == pytest.approx(4.5)
    assert recorded_room_ids(result) == {"cold", "hot"}


def test_save_restore_keeps_mixed_allocation_and_reasons(tmp_path):
    """保存/恢复后：分方向分配、未满足原因与设备目标保持一致。"""
    rooms = mixed_rooms(rated=5.0)

    def build():
        devices = [make_device("hp1", 4.5, 4.5), make_device("hp2", 3.5, 3.5)]
        schedule = PowerSchedule(segments=[(0.0, 5.0)])
        return devices, BuildingEngine(rooms, devices, power_schedule=schedule)

    devices, engine = build()
    ingest(engine, 0.0)
    engine.tick(0.0)
    state_file = tmp_path / "state.json"
    engine.save_state(state_file)

    new_devices, restored = build()
    restored = BuildingEngine.restore(state_file, rooms, new_devices)
    assert [d.mode for d in new_devices] == [d.mode for d in devices]

    ingest(engine, 60.0)
    ingest(restored, 60.0)
    before = engine.tick(60.0)
    after = restored.tick(60.0)

    assert {a.room_id: a.allocated_kw for a in after.delivered} == {
        a.room_id: a.allocated_kw for a in before.delivered
    }
    assert [(u.room_id, u.reason, u.unmet_kw) for u in after.unmet] == [
        (u.room_id, u.reason, u.unmet_kw) for u in before.unmet
    ]
    assert [(d.device_id, d.target_mode, d.applied_mode) for d in after.devices] == [
        (d.device_id, d.target_mode, d.applied_mode) for d in before.devices
    ]
    assert recorded_room_ids(after) == {"cold", "hot"}
    assert sum(a.allocated_kw for a in after.delivered) <= 5.0 + 1e-9
