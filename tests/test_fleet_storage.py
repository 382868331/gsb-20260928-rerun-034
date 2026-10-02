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
