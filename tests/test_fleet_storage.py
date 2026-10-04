"""多设备协同、冷热储能、限电与储能策略的验收测试。"""

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
    StoragePolicy,
    StorageSpec,
    ThermalStorage,
)


def make_device(device_id, heat=5.0, cool=5.0, min_on=0.0, min_off=0.0, now=-10_000.0):
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


def cooling_rooms():
    return [
        RoomConfig("r1", "房间1", priority=1, setpoint_c=20.0, rated_input_kw=3.0),
        RoomConfig("r2", "房间2", priority=2, setpoint_c=20.0, rated_input_kw=3.0),
        RoomConfig("r3", "房间3", priority=3, setpoint_c=20.0, rated_input_kw=3.0),
    ]


def ingest_all(engine, temps, t):
    for room_id, temp in temps.items():
        engine.ingest(Measurement(room_id, temp, t))


def test_two_devices_jointly_serve_and_keep_separate_ledgers():
    rooms = cooling_rooms()
    hp1, hp2 = make_device("hp1", cool=4.0), make_device("hp2", cool=2.0)
    engine = BuildingEngine(rooms, [hp1, hp2])
    ingest_all(engine, {"r1": 26.0, "r2": 25.0, "r3": 24.0}, 0.0)

    result = engine.tick(0.0)

    # 两台设备真实共同出力：4kW + 2kW，各自记账。
    assert result.device_info("hp1").delivered_kw == pytest.approx(4.0)
    assert result.device_info("hp2").delivered_kw == pytest.approx(2.0)
    delivered = {a.room_id: a.allocated_kw for a in result.delivered}
    assert delivered["r1"] == pytest.approx(3.0)
    assert delivered["r2"] == pytest.approx(3.0)
    assert delivered["r3"] == pytest.approx(0.0)
    unmet = result.unmet_for("r3")
    assert unmet.reason == "capacity_exceeded"
    assert unmet.unmet_kw == pytest.approx(3.0)

    # 第二 tick 累计 60s 供能账目：两台设备分开累计。
    ingest_all(engine, {"r1": 26.0, "r2": 25.0, "r3": 24.0}, 60.0)
    engine.tick(60.0)
    ledger = engine.energy_ledger()
    assert ledger["devices_kwh"]["hp1"] == pytest.approx(4.0 * 60 / 3600)
    assert ledger["devices_kwh"]["hp2"] == pytest.approx(2.0 * 60 / 3600)
    assert ledger["unmet_rooms_kwh"]["r3"] == pytest.approx(3.0 * 60 / 3600)


def test_power_limit_caps_real_delivery_and_reports_shortfall():
    rooms = cooling_rooms()
    devices = [make_device("hp1", cool=4.0), make_device("hp2", cool=2.0)]
    schedule = PowerSchedule(segments=[(0.0, 3.0)])
    engine = BuildingEngine(rooms, devices, power_schedule=schedule)
    ingest_all(engine, {"r1": 26.0, "r2": 25.0, "r3": 24.0}, 0.0)

    result = engine.tick(0.0)

    assert result.power_cap_kw == pytest.approx(3.0)
    total = sum(a.allocated_kw for a in result.delivered)
    assert total == pytest.approx(3.0)
    assert result.device_info("hp1").delivered_kw == pytest.approx(3.0)
    assert result.device_info("hp2").delivered_kw == pytest.approx(0.0)
    assert result.unmet_for("r2").reason == "power_limited"
    assert result.unmet_for("r3").reason == "power_limited"


def test_single_fault_leaves_other_device_serving():
    rooms = cooling_rooms()
    hp1, hp2 = make_device("hp1", cool=4.0), make_device("hp2", cool=2.0)
    engine = BuildingEngine(rooms, [hp1, hp2])
    ingest_all(engine, {"r1": 26.0, "r2": 25.0, "r3": 24.0}, 0.0)
    hp1.set_available(False, 0.0)

    result = engine.tick(0.0)

    assert result.device_info("hp1").faulted is True
    assert result.device_info("hp1").delivered_kw == pytest.approx(0.0)
    assert result.device_info("hp2").delivered_kw == pytest.approx(2.0)
    delivered = {a.room_id: a.allocated_kw for a in result.delivered}
    assert delivered["r1"] == pytest.approx(2.0)
    # 能力因单台故障真实缩水，未满足量按剩余能力记账。
    assert result.unmet_for("r1").unmet_kw == pytest.approx(1.0)
    assert result.unmet_for("r1").reason == "capacity_exceeded"
    assert result.unmet_for("r3").unmet_kw == pytest.approx(3.0)


def test_opposite_demands_keep_service_and_reason_for_every_partition():
    rooms = [
        RoomConfig("cold", "冷房", priority=1, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig("hot", "热房", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
    ]
    device = make_device("hp1", heat=4.5, cool=3.5)
    engine = BuildingEngine(rooms, device)
    ingest_all(engine, {"cold": 15.0, "hot": 31.0}, 0.0)

    result = engine.tick(0.0)

    assert {a.room_id for a in result.plan.allocations} == {"cold", "hot"}
    assert {a.room_id for a in result.delivered} == {"cold", "hot"}
    cold = result.allocation_for("cold")
    hot = result.allocation_for("hot")
    assert cold.mode is Mode.HEAT
    assert cold.allocated_kw == pytest.approx(4.5)
    assert hot.mode is Mode.COOL
    assert hot.allocated_kw == pytest.approx(0.0)
    assert result.unmet_for("hot").mode is Mode.COOL
    assert result.unmet_for("hot").reason == "mode_conflict"
    assert result.unmet_for("hot").unmet_kw == pytest.approx(5.0)
    assert sum(a.allocated_kw for a in result.delivered) == pytest.approx(4.5)

    cooling_first = [
        RoomConfig("hot", "热房", priority=1, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig("cold", "冷房", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
    ]
    cooling_engine = BuildingEngine(
        cooling_first,
        make_device("hp1", heat=4.5, cool=3.5),
    )
    ingest_all(cooling_engine, {"cold": 15.0, "hot": 31.0}, 0.0)
    cooling_result = cooling_engine.tick(0.0)
    assert cooling_result.allocation_for("hot").allocated_kw == pytest.approx(3.5)
    assert cooling_result.allocation_for("cold").allocated_kw == pytest.approx(0.0)
    assert cooling_result.unmet_for("cold").reason == "mode_conflict"


def test_direction_specific_capacity_fault_and_total_cap_are_separate():
    rooms = [
        RoomConfig("cool-a", "冷房A", priority=1, setpoint_c=22.0, rated_input_kw=4.0),
        RoomConfig("cool-b", "冷房B", priority=2, setpoint_c=22.0, rated_input_kw=4.0),
    ]

    devices = [
        make_device("hp1", heat=4.5, cool=3.5),
        make_device("hp2", heat=4.5, cool=3.5),
    ]
    engine = BuildingEngine(rooms, devices)
    ingest_all(engine, {"cool-a": 31.0, "cool-b": 31.0}, 0.0)
    healthy = engine.tick(0.0)
    assert sum(a.allocated_kw for a in healthy.delivered) == pytest.approx(7.0)
    assert healthy.device_info("hp1").delivered_kw == pytest.approx(3.5)
    assert healthy.device_info("hp2").delivered_kw == pytest.approx(3.5)
    assert healthy.unmet_for("cool-b").reason == "capacity_exceeded"

    devices[0].set_available(False, 60.0)
    ingest_all(engine, {"cool-a": 31.0, "cool-b": 31.0}, 60.0)
    faulted = engine.tick(60.0)
    assert faulted.device_info("hp1").faulted is True
    assert faulted.device_info("hp1").delivered_kw == pytest.approx(0.0)
    assert faulted.device_info("hp2").delivered_kw == pytest.approx(3.5)
    assert {a.room_id for a in faulted.delivered} == {"cool-a", "cool-b"}
    assert faulted.allocation_for("cool-a").allocated_kw == pytest.approx(3.5)
    assert faulted.allocation_for("cool-b").allocated_kw == pytest.approx(0.0)
    assert faulted.unmet_for("cool-a").unmet_kw == pytest.approx(0.5)
    assert faulted.unmet_for("cool-a").reason == "capacity_exceeded"
    assert faulted.unmet_for("cool-b").unmet_kw == pytest.approx(4.0)
    assert sum(a.allocated_kw for a in faulted.delivered) <= 3.5 + 1e-9
    capped_engine = BuildingEngine(
        rooms,
        [
            make_device("hp1", heat=4.5, cool=3.5),
            make_device("hp2", heat=4.5, cool=3.5),
        ],
        power_schedule=PowerSchedule(segments=[(0.0, 4.0)]),
    )
    ingest_all(capped_engine, {"cool-a": 31.0, "cool-b": 31.0}, 120.0)
    capped = capped_engine.tick(120.0)
    assert sum(info.delivered_kw for info in capped.devices) == pytest.approx(4.0)
    assert sum(a.allocated_kw for a in capped.delivered) == pytest.approx(4.0)
    assert capped.unmet_for("cool-b").reason == "power_limited"

    heating_devices = [
        make_device("hp1", heat=4.5, cool=3.5),
        make_device("hp2", heat=4.5, cool=3.5),
    ]
    heating_rooms = [
        RoomConfig("heat-a", "冷房A", priority=1, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig("heat-b", "冷房B", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
    ]
    heating_engine = BuildingEngine(heating_rooms, heating_devices)
    ingest_all(heating_engine, {"heat-a": 15.0, "heat-b": 15.0}, 0.0)
    heating = heating_engine.tick(0.0)
    assert heating.device_info("hp1").delivered_kw == pytest.approx(4.5)
    assert heating.device_info("hp2").delivered_kw == pytest.approx(4.5)
    assert heating.allocation_for("heat-a").allocated_kw == pytest.approx(5.0)
    assert heating.allocation_for("heat-b").allocated_kw == pytest.approx(4.0)
    assert heating.unmet_for("heat-b").reason == "capacity_exceeded"


def test_restore_preserves_faulted_capacity_and_conflict_basis(tmp_path):
    rooms = [
        RoomConfig("hot", "热房", priority=1, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig("cold", "冷房", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
    ]
    devices = [
        make_device("hp1", heat=4.5, cool=3.5),
        make_device("hp2", heat=4.5, cool=3.5),
    ]
    devices[0].set_available(False, 0.0)
    engine = BuildingEngine(rooms, devices)
    ingest_all(engine, {"cold": 15.0, "hot": 31.0}, 0.0)
    before = engine.tick(0.0)
    engine.save_state(tmp_path / "state.json")

    restored_devices = [
        make_device("hp1", heat=4.5, cool=3.5),
        make_device("hp2", heat=4.5, cool=3.5),
    ]
    restored = BuildingEngine.restore(tmp_path / "state.json", rooms, restored_devices)
    ingest_all(restored, {"cold": 15.0, "hot": 31.0}, 60.0)
    after = restored.tick(60.0)

    expected = [
        (a.room_id, a.mode, a.allocated_kw) for a in before.delivered
    ]
    actual = [
        (a.room_id, a.mode, a.allocated_kw) for a in after.delivered
    ]
    assert actual == expected
    assert after.allocation_for("hot").allocated_kw == pytest.approx(3.5)
    assert after.allocation_for("cold").allocated_kw == pytest.approx(0.0)
    assert after.unmet_for("hot").reason == "capacity_exceeded"
    assert after.unmet_for("hot").unmet_kw == pytest.approx(1.5)
    assert after.unmet_for("cold").reason == "mode_conflict"
    assert after.device_info("hp1").faulted is True
    assert after.device_info("hp2").delivered_kw == pytest.approx(3.5)
    assert sum(a.allocated_kw for a in after.delivered) <= 3.5 + 1e-9


def make_storage(heat_kwh=0.0, cool_kwh=0.0, capacity=10.0, eff=0.9):
    return ThermalStorage(
        StorageSpec(
            name="buffer",
            heat_capacity_kwh=capacity,
            cool_capacity_kwh=capacity,
            max_charge_kw=10.0,
            max_discharge_kw=10.0,
            charge_efficiency=eff,
            discharge_efficiency=eff,
        ),
        heat_kwh=heat_kwh,
        cool_kwh=cool_kwh,
    )


def direction_rooms():
    return [
        RoomConfig("cold", "冷房", priority=1, setpoint_c=22.0, rated_input_kw=5.0),
        RoomConfig("hot", "热房", priority=2, setpoint_c=22.0, rated_input_kw=5.0),
    ]


def test_direction_specific_devices_serve_both_rooms_independently():
    """仅能制冷 4.5kW 与仅能制热 3.5kW 的两台设备分别服务对应房间。"""
    rooms = direction_rooms()
    cooler = make_device("cooler", heat=0.0, cool=4.5)
    heater = make_device("heater", heat=3.5, cool=0.0)
    engine = BuildingEngine(rooms, [cooler, heater])
    ingest_all(engine, {"cold": 15.0, "hot": 31.0}, 0.0)

    result = engine.tick(0.0)

    assert result.device_info("cooler").applied_mode is EquipmentMode.COOL
    assert result.device_info("heater").applied_mode is EquipmentMode.HEAT
    assert result.device_info("cooler").delivered_kw == pytest.approx(4.5)
    assert result.device_info("heater").delivered_kw == pytest.approx(3.5)
    cold = result.allocation_for("cold")
    hot = result.allocation_for("hot")
    assert cold.mode is Mode.HEAT
    assert cold.allocated_kw == pytest.approx(3.5)
    assert hot.mode is Mode.COOL
    assert hot.allocated_kw == pytest.approx(4.5)
    # 两个房间都被真实服务，不再只有模式冲突解释。
    assert result.unmet_for("cold").unmet_kw == pytest.approx(1.5)
    assert result.unmet_for("cold").reason == "capacity_exceeded"
    assert result.unmet_for("hot").unmet_kw == pytest.approx(0.5)
    assert result.unmet_for("hot").reason == "capacity_exceeded"
    assert all(item.reason != "mode_conflict" for item in result.unmet)
    # 不得超过真实能力：制冷机不出热量，制热机不出冷量。
    assert sum(a.allocated_kw for a in result.delivered) == pytest.approx(8.0)


def test_direction_allocation_is_order_independent_and_respects_site_cap():
    rooms = direction_rooms()

    def build(order, cap=None):
        devices = {
            "cooler": make_device("cooler", heat=0.0, cool=4.5),
            "heater": make_device("heater", heat=3.5, cool=0.0),
        }
        schedule = PowerSchedule(segments=[(0.0, cap)]) if cap is not None else None
        return BuildingEngine(rooms, [devices[key] for key in order], power_schedule=schedule)

    delivered_by_order = {}
    for order in (("cooler", "heater"), ("heater", "cooler")):
        engine = build(order)
        ingest_all(engine, {"cold": 15.0, "hot": 31.0}, 0.0)
        result = engine.tick(0.0)
        delivered_by_order[order] = {
            a.room_id: a.allocated_kw for a in result.delivered
        }
    assert delivered_by_order[("cooler", "heater")] == delivered_by_order[("heater", "cooler")]

    # 站点限额 5kW：总交付不得超限，缺口记为 power_limited。
    capped = build(("cooler", "heater"), cap=5.0)
    ingest_all(capped, {"cold": 15.0, "hot": 31.0}, 0.0)
    result = capped.tick(0.0)
    assert sum(a.allocated_kw for a in result.delivered) <= 5.0 + 1e-9
    assert sum(info.delivered_kw for info in result.devices) <= 5.0 + 1e-9
    assert any(item.reason == "power_limited" for item in result.unmet)


def test_direction_fault_lockout_and_budget_reasons_are_distinct():
    """故障、启停保护与预算不足在同一场景下分别记账。"""
    rooms = [
        RoomConfig("cool-a", "冷房A", priority=1, setpoint_c=22.0, rated_input_kw=4.0),
        RoomConfig("cool-b", "冷房B", priority=2, setpoint_c=22.0, rated_input_kw=4.0),
        RoomConfig("heat-a", "热房", priority=3, setpoint_c=22.0, rated_input_kw=4.0),
    ]
    cooler = make_device("cooler", heat=0.0, cool=4.0)
    heater = make_device("heater", heat=4.0, cool=0.0, min_off=300.0)
    schedule = PowerSchedule(segments=[(0.0, 6.0)])
    engine = BuildingEngine(rooms, [cooler, heater], power_schedule=schedule)
    # heater 处于最短停机等待中。
    heater.mode = EquipmentMode.OFF
    heater.state_since = -100.0
    cooler.set_available(False, -50.0)
    ingest_all(engine, {"cool-a": 31.0, "cool-b": 31.0, "heat-a": 15.0}, 0.0)

    result = engine.tick(0.0)

    assert result.device_info("cooler").faulted is True
    assert result.device_info("heater").applied_mode is EquipmentMode.OFF
    assert result.unmet_for("cool-a").reason == "equipment_faulted"
    assert result.unmet_for("cool-b").reason == "equipment_faulted"
    assert result.unmet_for("heat-a").reason == "min_off_not_satisfied"
    assert sum(a.allocated_kw for a in result.delivered) == pytest.approx(0.0)

    # 故障恢复、停机等待结束：限电 6kW 下制冷 4kW + 制热 2kW，热房缺口记预算不足。
    cooler.set_available(True, 60.0)
    ingest_all(engine, {"cool-a": 31.0, "cool-b": 31.0, "heat-a": 15.0}, 400.0)
    recovered = engine.tick(400.0)
    assert recovered.device_info("cooler").delivered_kw == pytest.approx(4.0)
    assert recovered.device_info("heater").delivered_kw == pytest.approx(2.0)
    assert recovered.allocation_for("heat-a").allocated_kw == pytest.approx(2.0)
    assert recovered.unmet_for("heat-a").reason == "power_limited"
    assert sum(a.allocated_kw for a in recovered.delivered) <= 6.0 + 1e-9


def test_direction_allocation_consistent_across_save_restore(tmp_path):
    rooms = direction_rooms()

    def build():
        return [
            make_device("cooler", heat=0.0, cool=4.5),
            make_device("heater", heat=3.5, cool=0.0),
        ]

    devices = build()
    engine = BuildingEngine(rooms, devices)
    ingest_all(engine, {"cold": 15.0, "hot": 31.0}, 0.0)
    engine.tick(0.0)
    state_file = tmp_path / "state.json"
    engine.save_state(state_file)

    restored_devices = build()
    restored = BuildingEngine.restore(state_file, rooms, restored_devices)
    ingest_all(restored, {"cold": 15.0, "hot": 31.0}, 60.0)
    after = restored.tick(60.0)

    continuous = BuildingEngine(rooms, build())
    ingest_all(continuous, {"cold": 15.0, "hot": 31.0}, 0.0)
    continuous.tick(0.0)
    ingest_all(continuous, {"cold": 15.0, "hot": 31.0}, 60.0)
    expected = continuous.tick(60.0)

    actual_map = {a.room_id: (a.mode, a.allocated_kw) for a in after.delivered}
    expected_map = {a.room_id: (a.mode, a.allocated_kw) for a in expected.delivered}
    assert actual_map == expected_map
    assert after.device_info("cooler").applied_mode is EquipmentMode.COOL
    assert after.device_info("heater").applied_mode is EquipmentMode.HEAT


def test_storage_charges_at_low_demand_and_discharges_at_high():
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0, rated_input_kw=3.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=22.0, rated_input_kw=3.0),
    ]
    device = make_device("hp1", heat=5.0)
    storage = make_storage()
    engine = BuildingEngine(rooms, device, storage=storage)

    # 低需求：r1 需 1.2kW，设备富余 3.8kW 充入储能，1 小时入库 3.8*0.9 kWh。
    engine.ingest(Measurement("r1", 21.4, 0.0))
    engine.ingest(Measurement("r2", 22.0, 0.0))
    first = engine.tick(0.0)
    assert first.storage is not None
    engine.ingest(Measurement("r1", 21.4, 3600.0))
    engine.ingest(Measurement("r2", 22.0, 3600.0))
    charged = engine.tick(3600.0)
    assert charged.storage.charge_kw == pytest.approx(3.8)
    assert charged.storage.heat_kwh == pytest.approx(3.8 * 0.9)
    assert charged.device_info("hp1").delivered_kw == pytest.approx(5.0)

    # 高需求：两房间共需 6kW > 设备 5kW，储能放出 1kW 补足。
    engine.ingest(Measurement("r1", 16.0, 7200.0))
    engine.ingest(Measurement("r2", 16.0, 7200.0))
    peak = engine.tick(7200.0)
    assert peak.storage.discharge_kw == pytest.approx(1.0)
    delivered = {a.room_id: a.allocated_kw for a in peak.delivered}
    assert delivered["r1"] == pytest.approx(3.0)
    assert delivered["r2"] == pytest.approx(3.0)
    assert peak.unmet == ()
    # 损耗不凭空补足：高峰时设备无富余（不充电），放出 1kW 需出库 1/0.9 kWh。
    expected = 3.8 * 0.9 - 1.0 / 0.9
    assert peak.storage.heat_kwh == pytest.approx(expected)


def test_preserve_reserve_policy_holds_emergency_stock():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0, rated_input_kw=3.0)]
    device = make_device("hp1", heat=1.0)
    storage = make_storage(heat_kwh=4.0)
    engine = BuildingEngine(
        rooms,
        device,
        storage=storage,
        storage_policy=StoragePolicy.PRESERVE_RESERVE,
        reserve_fraction=0.5,
    )
    engine.ingest(Measurement("r1", 16.0, 0.0))
    engine.tick(0.0)
    engine.ingest(Measurement("r1", 16.0, 3600.0))

    result = engine.tick(3600.0)

    # 储备下限 5kWh > 当前 4kWh：储能不放，缺口记为 storage_reserve_held。
    assert result.storage.discharge_kw == pytest.approx(0.0)
    assert result.storage.heat_kwh == pytest.approx(4.0)
    unmet = result.unmet_for("r1")
    assert unmet.reason == "storage_reserve_held"
    assert unmet.unmet_kw == pytest.approx(2.0)


def test_comfort_first_policy_drains_storage_for_demand():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0, rated_input_kw=3.0)]
    device = make_device("hp1", heat=1.0)
    storage = make_storage(heat_kwh=4.0)
    engine = BuildingEngine(
        rooms, device, storage=storage, storage_policy=StoragePolicy.COMFORT_FIRST
    )
    engine.ingest(Measurement("r1", 16.0, 0.0))
    engine.tick(0.0)
    engine.ingest(Measurement("r1", 16.0, 3600.0))

    result = engine.tick(3600.0)

    assert result.storage.discharge_kw == pytest.approx(2.0)
    assert result.allocation_for("r1").allocated_kw == pytest.approx(3.0)
    assert result.unmet == ()
    assert result.storage.heat_kwh == pytest.approx(4.0 - 2.0 / 0.9)


def test_heat_and_cool_storage_never_offset_each_other():
    rooms = [RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0, rated_input_kw=3.0)]
    device = make_device("hp1", heat=5.0, cool=5.0)
    storage = make_storage(heat_kwh=5.0, cool_kwh=0.0)
    engine = BuildingEngine(rooms, device, storage=storage)

    engine.ingest(Measurement("r1", 16.0, 0.0))
    assert engine.tick(0.0).delivered_mode is EquipmentMode.HEAT

    # 切换到制冷：热量储量不能抵作冷量，也保持不变。
    engine.ingest(Measurement("r1", 30.0, 60.0))
    engine.tick(60.0)  # HEAT -> OFF
    engine.ingest(Measurement("r1", 30.0, 120.0))
    cooling = engine.tick(120.0)  # OFF -> COOL

    assert cooling.delivered_mode is EquipmentMode.COOL
    # 热量储量原样保留，不被冷量需求消耗；冷仓只由制冷富余充入。
    assert cooling.storage.heat_kwh == pytest.approx(5.0)
    assert cooling.storage.cool_kwh == pytest.approx(2.0 * 0.9 / 60.0)
    assert cooling.storage.discharge_kw == pytest.approx(0.0)
    assert cooling.allocation_for("r1").allocated_kw == pytest.approx(3.0)


def test_save_restore_keeps_storage_waits_and_unmet_continuity(tmp_path):
    rooms = [
        RoomConfig("r1", "主卧", priority=1, setpoint_c=22.0, rated_input_kw=3.0),
        RoomConfig("r2", "客厅", priority=2, setpoint_c=22.0, rated_input_kw=3.0),
    ]

    def build():
        devices = [
            make_device("hp1", heat=4.0, min_off=300.0),
            make_device("hp2", heat=2.0, min_off=300.0),
        ]
        # 冷仓有储量但当前是制热需求：不同性质储量不能相抵，原样保留。
        storage = make_storage(heat_kwh=0.0, cool_kwh=1.5)
        schedule = PowerSchedule(segments=[(0.0, 5.0)])
        return devices, storage, BuildingEngine(
            rooms, devices, storage=storage, power_schedule=schedule
        )

    devices, storage, engine = build()
    engine.ingest(Measurement("r1", 16.0, 0.0))
    engine.ingest(Measurement("r2", 16.0, 0.0))
    engine.tick(0.0)

    # 60s：hp1 故障急停，限电 5kW 下只剩 hp2 的 2kW，缺口开始累计。
    devices[0].set_available(False, 60.0)
    engine.ingest(Measurement("r1", 16.0, 60.0))
    engine.ingest(Measurement("r2", 16.0, 60.0))
    engine.tick(60.0)
    state_file = tmp_path / "state.json"
    engine.save_state(state_file)
    ledger_before = engine.energy_ledger()

    # 重开：新设备、新储能对象从存档恢复，储量/等待计时/未满足账目保持连续。
    new_devices, new_storage, _ = build()
    restored = BuildingEngine.restore(state_file, rooms, new_devices, storage=new_storage)
    assert new_storage.cool_kwh == pytest.approx(1.5)
    assert new_storage.heat_kwh == pytest.approx(storage.heat_kwh)
    assert new_devices[0].faulted is True
    assert new_devices[0].state_since == pytest.approx(60.0)
    assert restored.energy_ledger() == ledger_before

    # 120s 故障恢复，但 hp1 最短停机未到（360s 才满），仍只有 hp2 供能。
    new_devices[0].set_available(True, 120.0)
    restored.ingest(Measurement("r1", 16.0, 120.0))
    restored.ingest(Measurement("r2", 16.0, 120.0))
    waiting = restored.tick(120.0)
    assert waiting.device_info("hp1").applied_mode is EquipmentMode.OFF
    assert waiting.device_info("hp1").lockout_remaining_s == pytest.approx(240.0)
    assert waiting.device_info("hp2").delivered_kw == pytest.approx(2.0)

    # 360s 等待结束，两台设备重新共同供能；未满足账目在恢复后继续累计。
    restored.ingest(Measurement("r1", 16.0, 360.0))
    restored.ingest(Measurement("r2", 16.0, 360.0))
    recovered = restored.tick(360.0)
    assert recovered.device_info("hp1").delivered_kw > 0
    assert recovered.device_info("hp2").delivered_kw > 0
    ledger_after = restored.energy_ledger()
    assert ledger_after["unmet_rooms_kwh"]["r2"] > ledger_before["unmet_rooms_kwh"]["r2"]
