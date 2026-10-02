"""多设备 + 储能 + 限电 + 策略 + 持久化的验收测试。"""

import pytest

from building_control import (
    STRATEGY_COMFORT,
    STRATEGY_RESERVE,
    BuildingEngine,
    EquipmentMode,
    HeatPump,
    HeatPumpSpec,
    Measurement,
    Mode,
    PowerSchedule,
    RoomConfig,
    ThermalStorage,
)


def make_device(name, heat, cool, min_on=0.0, min_off=0.0, now=-10_000.0):
    spec = HeatPumpSpec(
        name=name,
        heating_capacity_kw=heat,
        cooling_capacity_kw=cool,
        min_on_s=min_on,
        min_off_s=min_off,
    )
    return HeatPump(spec, now=now)


def make_rooms(temps, setpoint=22.0):
    return [
        RoomConfig(f"r{i+1}", f"房间{i+1}", priority=i + 1, setpoint_c=setpoint)
        for i in range(len(temps))
    ]


def feed(engine, rooms, temps, t):
    for room, temp in zip(rooms, temps):
        engine.ingest(Measurement(room.room_id, temp, t))


def test_two_devices_jointly_serve_and_accounted_separately():
    rooms = make_rooms([30.0, 29.0, 28.0])
    d1 = make_device("hp1", 5.0, 5.0)
    d2 = make_device("hp2", 3.0, 3.0)
    engine = BuildingEngine(rooms, [d1, d2])
    feed(engine, rooms, [30.0, 29.0, 28.0], 0.0)

    result = engine.tick(0.0)

    # 两台设备都真实供冷，各自记账，不是“描述两台却只按一台供”。
    d1_delivery = result.delivery_for("hp1")
    d2_delivery = result.delivery_for("hp2")
    assert d1_delivery.delivered_mode is EquipmentMode.COOL
    assert d2_delivery.delivered_mode is EquipmentMode.COOL
    assert d1_delivery.output_kw == pytest.approx(5.0)
    assert d2_delivery.output_kw > 0.0
    total = sum(a.allocated_kw for a in result.delivered)
    assert total == pytest.approx(d1_delivery.to_rooms_kw + d2_delivery.to_rooms_kw)
    assert 5.0 < total <= 8.0 + 1e-9


def test_two_devices_serve_opposite_modes_simultaneously():
    rooms = make_rooms([16.0, 30.0])  # r1 制热优先，r2 制冷
    d1 = make_device("hp1", 5.0, 5.0)
    d2 = make_device("hp2", 3.0, 3.0)
    engine = BuildingEngine(rooms, [d1, d2])
    feed(engine, rooms, [16.0, 30.0], 0.0)

    result = engine.tick(0.0)

    assert result.delivery_for("hp1").delivered_mode is EquipmentMode.HEAT
    assert result.delivery_for("hp2").delivered_mode is EquipmentMode.COOL
    assert result.allocation_for("r1").mode is Mode.HEAT
    assert result.allocation_for("r1").allocated_kw > 0
    assert result.allocation_for("r2").mode is Mode.COOL
    assert result.allocation_for("r2").allocated_kw > 0
    assert result.unmet == ()


def test_power_cap_limits_real_delivery_and_reports_unmet():
    rooms = make_rooms([30.0, 29.0])
    d1 = make_device("hp1", 5.0, 5.0)
    d2 = make_device("hp2", 5.0, 5.0)
    schedule = PowerSchedule(segments=[(0.0, 4.0)])
    engine = BuildingEngine(rooms, [d1, d2], power_schedule=schedule)
    feed(engine, rooms, [30.0, 29.0], 0.0)

    result = engine.tick(0.0)

    delivered_total = sum(a.allocated_kw for a in result.delivered)
    assert delivered_total == pytest.approx(4.0)
    assert result.power_cap_kw == pytest.approx(4.0)
    unmet = result.unmet_for("r2")
    assert unmet.reason == "power_capped"
    assert unmet.unmet_kw > 0


def test_single_device_fault_leaves_other_serving():
    rooms = make_rooms([30.0, 29.0, 28.0])
    d1 = make_device("hp1", 5.0, 5.0)
    d2 = make_device("hp2", 3.0, 3.0)
    engine = BuildingEngine(rooms, [d1, d2])
    feed(engine, rooms, [30.0, 29.0, 28.0], 0.0)
    engine.tick(0.0)

    d1.set_available(False, 60.0)
    feed(engine, rooms, [30.0, 29.0, 28.0], 60.0)
    result = engine.tick(60.0)

    assert result.delivery_for("hp1").faulted is True
    assert result.delivery_for("hp1").output_kw == pytest.approx(0.0)
    assert result.delivery_for("hp2").delivered_mode is EquipmentMode.COOL
    assert result.delivery_for("hp2").output_kw == pytest.approx(3.0)
    total = sum(a.allocated_kw for a in result.delivered)
    assert total == pytest.approx(3.0)
    assert any(u.reason == "equipment_faulted" for u in result.unmet)
    assert sum(u.unmet_kw for u in result.unmet) > 0


def test_storage_charges_at_low_demand_with_real_losses():
    rooms = make_rooms([23.0])  # 需求很小（约 2kW）
    d1 = make_device("hp1", 6.0, 6.0)
    storage = ThermalStorage(
        capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=4.0,
        charge_efficiency=0.8, discharge_efficiency=0.8,
    )
    engine = BuildingEngine(rooms, [d1], storage=storage)
    feed(engine, rooms, [23.0], 0.0)

    result = engine.tick(0.0, dt_s=3600.0)

    flow = result.storage_flow
    assert flow.charge_cool_kw > 0.0
    # 设备当刻产出 = 房间获得 + 储能充入，三方对账。
    delivery = result.delivery_for("hp1")
    assert delivery.output_kw == pytest.approx(delivery.to_rooms_kw + delivery.to_storage_kw)
    assert result.allocation_for("r1").allocated_kw == pytest.approx(delivery.to_rooms_kw)
    # 损耗真实存在：罐内增量 < 充入功率 * 时长。
    charged_kwh = flow.charge_cool_kw * 1.0
    assert storage.cool_kwh == pytest.approx(charged_kwh * 0.8)
    assert storage.cool_kwh < charged_kwh


def test_storage_discharges_under_power_cap():
    rooms = make_rooms([30.0, 29.0])
    d1 = make_device("hp1", 6.0, 6.0)
    schedule = PowerSchedule(segments=[(0.0, 3.0)])  # 限电 3kW
    storage = ThermalStorage(
        capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=4.0,
        charge_efficiency=1.0, discharge_efficiency=0.5, cool_kwh=4.0,
    )
    engine = BuildingEngine(rooms, [d1], power_schedule=schedule, storage=storage)
    feed(engine, rooms, [30.0, 29.0], 0.0)

    result = engine.tick(0.0, dt_s=1800.0)

    flow = result.storage_flow
    assert flow.discharge_cool_kw > 0.0
    device_kw = result.delivery_for("hp1").to_rooms_kw
    assert device_kw <= 3.0 + 1e-9
    # 房间实际获得 = 设备直供 + 储能释放。
    total = sum(a.allocated_kw for a in result.delivered)
    assert total == pytest.approx(device_kw + flow.discharge_cool_kw)
    # 损耗不能凭空补足：罐内减少量 = 放出的能量 / 效率 > 放出的能量。
    removed = 4.0 - storage.cool_kwh
    delivered_kwh = flow.discharge_cool_kw * 0.5
    assert removed == pytest.approx(delivered_kwh / 0.5)
    assert removed > delivered_kwh


def test_reserve_strategy_preserves_emergency_stock():
    rooms = make_rooms([30.0])
    temps = [30.0]

    def run(strategy):
        d1 = make_device("hp1", 0.5, 0.5)
        storage = ThermalStorage(
            capacity_kwh=4.0, max_charge_kw=4.0, max_discharge_kw=4.0,
            charge_efficiency=1.0, discharge_efficiency=1.0, cool_kwh=4.0,
        )
        engine = BuildingEngine(
            rooms, [d1], storage=storage, strategy=strategy, reserve_fraction=0.5
        )
        feed(engine, rooms, temps, 0.0)
        result = engine.tick(0.0, dt_s=3600.0)
        return result, storage

    comfort_result, comfort_storage = run(STRATEGY_COMFORT)
    reserve_result, reserve_storage = run(STRATEGY_RESERVE)

    # 舒适优先：储能尽量放完（缺口 2.5kW 全补）；保留储备：至少留下 50% 应急储量。
    assert comfort_storage.cool_kwh == pytest.approx(1.5)
    assert reserve_storage.cool_kwh == pytest.approx(2.0)
    comfort_got = comfort_result.allocation_for("r1").allocated_kw
    reserve_got = reserve_result.allocation_for("r1").allocated_kw
    assert comfort_got > reserve_got
    unmet = reserve_result.unmet_for("r1")
    assert unmet.reason == "reserve_preserved"
    assert unmet.unmet_kw > 0


def test_mode_switch_does_not_net_heat_and_cool_storage():
    rooms = make_rooms([16.0])  # 先制热
    d1 = make_device("hp1", 6.0, 6.0, min_on=0.0, min_off=0.0)
    storage = ThermalStorage(
        capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=4.0,
        charge_efficiency=1.0, discharge_efficiency=1.0,
    )
    engine = BuildingEngine(rooms, [d1], storage=storage)
    feed(engine, rooms, [16.0], 0.0)
    engine.tick(0.0, dt_s=3600.0)
    heat_after_charge = storage.heat_kwh
    assert heat_after_charge > 0.0
    assert storage.cool_kwh == pytest.approx(0.0)

    # 切换到制冷用途（先回 OFF 再启动）：热量储量不能被冷量抵销，两罐各自独立。
    feed(engine, rooms, [30.0], 60.0)
    assert engine.tick(60.0, dt_s=60.0).delivery_for("hp1").delivered_mode is EquipmentMode.OFF
    feed(engine, rooms, [30.0], 120.0)
    result = engine.tick(120.0, dt_s=3600.0)
    assert result.delivery_for("hp1").delivered_mode is EquipmentMode.COOL
    assert storage.heat_kwh == pytest.approx(heat_after_charge)
    assert storage.cool_kwh > 0.0


def test_save_restore_keeps_storage_waits_and_unmet_continuous(tmp_path):
    rooms = make_rooms([30.0, 29.0])
    d1 = make_device("hp1", 6.0, 6.0, min_on=0.0, min_off=300.0)
    d2 = make_device("hp2", 3.0, 3.0, min_on=0.0, min_off=0.0)
    schedule = PowerSchedule(segments=[(0.0, 4.0)])
    storage = ThermalStorage(
        capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=2.0,
        charge_efficiency=1.0, discharge_efficiency=1.0, cool_kwh=0.5,
    )
    engine = BuildingEngine(rooms, [d1, d2], power_schedule=schedule, storage=storage)
    feed(engine, rooms, [30.0, 29.0], 0.0)
    engine.tick(0.0, dt_s=60.0)

    # 故障急停 d1，随后保存：等待计时、储量、未满足需求都要连续。
    d1.set_available(False, 120.0)
    feed(engine, rooms, [30.0, 29.0], 120.0)
    before = engine.tick(120.0, dt_s=60.0)
    assert before.delivery_for("hp1").faulted is True
    state_file = tmp_path / "state.json"
    engine.save_state(state_file)
    stored_before = storage.cool_kwh

    # 模拟进程重启：全新设备与储能对象，从存档恢复。
    new_d1 = make_device("hp1", 6.0, 6.0, min_on=0.0, min_off=300.0)
    new_d2 = make_device("hp2", 3.0, 3.0, min_on=0.0, min_off=0.0)
    new_storage = ThermalStorage(
        capacity_kwh=10.0, max_charge_kw=4.0, max_discharge_kw=4.0
    )
    restored = BuildingEngine.restore(
        state_file, rooms, [new_d1, new_d2], storage=new_storage
    )
    assert new_d1.faulted is True
    assert new_d1.state_since == pytest.approx(120.0)
    assert new_storage.cool_kwh == pytest.approx(stored_before)
    assert restored.power_schedule.cap_kw(200.0) == pytest.approx(4.0)

    # 恢复后继续对账：d1 故障恢复但最短停机未结束，仍不能启动。
    new_d1.set_available(True, 180.0)
    feed(restored, rooms, [30.0, 29.0], 240.0)
    waiting = restored.tick(240.0, dt_s=60.0)
    assert waiting.delivery_for("hp1").delivered_mode is EquipmentMode.OFF
    assert waiting.delivery_for("hp1").lockout_remaining_s == pytest.approx(180.0)
    assert waiting.unmet  # 高需求 + 限电下仍有未满足需求，账目连续。

    # 等待结束后 d1 重新参与供能，储量只被真实充放改变。
    feed(restored, rooms, [30.0, 29.0], 430.0)
    resumed = restored.tick(430.0, dt_s=60.0)
    assert resumed.delivery_for("hp1").delivered_mode is EquipmentMode.COOL
    assert resumed.delivery_for("hp1").output_kw > 0.0
