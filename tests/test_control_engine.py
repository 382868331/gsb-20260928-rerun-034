import unittest

from building import (
    Equipment,
    MeasurementBus,
    Planner,
    Room,
    RoomModel,
)
from building.engine import ControlEngine


class PriorityPlanningTests(unittest.TestCase):
    def setUp(self):
        self.hot = Room("hot-room", priority=2, setpoint=22.0, gain=1.0)
        self.cold = Room("cold-room", priority=1, setpoint=22.0, gain=1.0)
        self.equipment = Equipment("hvac-1", heat_capacity=1.5, cool_capacity=1.5)

    def test_conflicting_heat_and_cool_follows_priority(self):
        planner = Planner(self.equipment)
        plan = planner.plan(
            [self.hot, self.cold],
            {"hot-room": 26.0, "cold-room": 21.0},
        )
        self.assertEqual(plan.mode, "heat")
        self.assertEqual([a.room_id for a in plan.allocations], ["cold-room"])
        self.assertEqual(plan.unsatisfied, ("hot-room",))

    def test_limited_capacity_splits_by_priority(self):
        other_cold = Room("other-cold", priority=2, setpoint=22.0)
        planner = Planner(self.equipment)
        plan = planner.plan(
            [self.cold, other_cold],
            {"cold-room": 21.4, "other-cold": 21.0},
        )
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(
            [a.room_id for a in plan.allocations], ["cold-room", "other-cold"]
        )
        self.assertEqual(plan.capacity, 1.5)
        self.assertAlmostEqual(plan.allocations[0].capacity, 1.1)
        self.assertAlmostEqual(plan.allocations[1].capacity, 0.4)
        self.assertEqual(plan.unsatisfied, ("other-cold",))


class StaleSensorTests(unittest.TestCase):
    def test_old_reading_is_never_used_as_current_value(self):
        room = Room("room-a", priority=1, setpoint=22.0)
        bus = MeasurementBus(timeout_seconds=30.0)
        engine = ControlEngine([room], Equipment("hvac", 2.0, 2.0), bus, now=0.0)
        bus.publish(engine.sensor_id("room-a"), 10.0, timestamp=0.0)

        plan, command = engine.step(now=10.0)
        self.assertEqual(plan.mode, "heat")
        self.assertEqual(command.mode, "heat")

        # Stream stops: at exactly the timeout the reading is still fresh,
        # afterwards it must be treated as missing rather than as 10 C.
        self.assertIsNone(bus.latest(engine.sensor_id("room-a"), now=31.0))
        plan, command = engine.step(now=31.0)
        self.assertEqual(plan.degraded_rooms, frozenset({"room-a"}))
        self.assertEqual(plan.mode, "off")
        self.assertEqual(command.mode, "off")
        self.assertEqual(command.reason, "demand_cleared")


class MinimumTimerTests(unittest.TestCase):
    def _engine(self):
        cold = Room("cold", priority=1, setpoint=22.0, gain=1.0)
        hot = Room("hot", priority=2, setpoint=22.0, gain=1.0)
        equipment = Equipment(
            "hvac", heat_capacity=2.0, cool_capacity=2.0,
            min_on_seconds=60.0, min_off_seconds=120.0,
        )
        return ControlEngine([cold, hot], equipment, MeasurementBus(300.0), now=0.0)

    def test_minimum_on_and_off_times_are_held(self):
        engine = self._engine()
        engine.bus.publish(engine.sensor_id("cold"), 10.0, 0.0)
        plan, command = engine.step(0.0)
        self.assertEqual((plan.mode, command.mode, command.reason),
                         ("heat", "heat", "started"))

        # Demand disappears after 10 s: unit must keep running to 60 s.
        engine.bus.publish(engine.sensor_id("cold"), 22.0, 10.0)
        _, command = engine.step(10.0)
        self.assertEqual((command.mode, command.reason), ("heat", "min_on_timer"))
        _, command = engine.step(59.9)
        self.assertEqual((command.mode, command.reason), ("heat", "min_on_timer"))
        _, command = engine.step(60.0)
        self.assertEqual((command.mode, command.reason), ("off", "demand_cleared"))

        # Cooling demand immediately appears, but the off-timer blocks start.
        for t in (60.0, 100.0, 179.9, 180.0):
            engine.bus.publish(engine.sensor_id("hot"), 30.0, t)
        _, command = engine.step(60.0)
        self.assertEqual((command.mode, command.reason), ("off", "min_off_timer"))
        _, command = engine.step(179.9)
        self.assertEqual((command.mode, command.reason), ("off", "min_off_timer"))
        _, command = engine.step(180.0)
        self.assertEqual((command.mode, command.reason), ("cool", "started"))

    def test_saved_state_keeps_waiting_after_restart(self):
        import tempfile
        from pathlib import Path

        engine = self._engine()
        engine.bus.publish(engine.sensor_id("cold"), 10.0, 0.0)
        engine.step(0.0)
        engine.bus.publish(engine.sensor_id("cold"), 22.0, 60.0)
        engine.step(60.0)  # turns off at t=60, min_off ends at t=180

        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.json"
            engine.save_state(state_path)

            restarted = self._engine()
            restarted.load_state(state_path)
            for t in (100.0, 179.9, 180.0):
                restarted.bus.publish(restarted.sensor_id("hot"), 30.0, t)

            _, command = restarted.step(100.0)
            self.assertEqual((command.mode, command.reason),
                             ("off", "min_off_timer"))
            _, command = restarted.step(179.9)
            self.assertEqual(command.mode, "off")
            _, command = restarted.step(180.0)
            self.assertEqual((command.mode, command.reason), ("cool", "started"))


class PlanVsActualSimulationTests(unittest.TestCase):
    def test_opposite_demands_actual_delivery_matches_executed_plan(self):
        cold_room = Room("cold", priority=1, setpoint=22.0, gain=0.1,
                         loss=0.0, ambient=22.0)
        hot_room = Room("hot", priority=2, setpoint=22.0, gain=0.1,
                        loss=0.0, ambient=22.0)
        equipment = Equipment("hvac", heat_capacity=2.0, cool_capacity=2.0)
        engine = ControlEngine([cold_room, hot_room], equipment,
                               MeasurementBus(timeout_seconds=300.0))
        sims = {"cold": RoomModel(cold_room, 20.0),
                "hot": RoomModel(hot_room, 24.0)}

        for t in range(0, 10):
            for room_id, model in sims.items():
                engine.bus.publish(engine.sensor_id(room_id), model.temperature, float(t))
            plan, command = engine.step(float(t))
            delivered = engine.delivered()
            for room_id, model in sims.items():
                model.step(delivered[room_id], dt=1.0)

        self.assertEqual(plan.mode, "heat")
        self.assertEqual(command.mode, "heat")
        # The high-priority cold room warms; the opposite-demand room gets
        # nothing instead of being actively heated by the shared unit.
        self.assertGreater(sims["cold"].temperature, 20.0)
        self.assertEqual(sims["hot"].temperature, 24.0)
        self.assertEqual(delivered["hot"], 0.0)
        self.assertIn("hot", plan.unsatisfied)


if __name__ == "__main__":
    unittest.main()
