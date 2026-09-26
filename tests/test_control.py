"""Focused tests for the time plan and slow valve response."""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest


SOURCE = Path(__file__).parents[1] / "custom_components" / "room_thermostat" / "control.py"
SPEC = importlib.util.spec_from_file_location("room_control", SOURCE)
control = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = control
SPEC.loader.exec_module(control)


class ScheduleTests(unittest.TestCase):
    def test_preheat_starts_before_an_upward_switch(self):
        schedule = [[{"minute": 0, "temperature": 20}] for _ in range(7)]
        schedule[0].append({"minute": 540, "temperature": 22})
        early = datetime(2026, 9, 21, 4, tzinfo=timezone.utc)
        self.assertFalse(control.preheat_plan(schedule, early, 20, 0, 0.5)["active"])
        later = early + timedelta(hours=1)
        plan = control.preheat_plan(schedule, later, 20, 0, 0.5)
        self.assertTrue(plan["active"])
        self.assertEqual(plan["target"], 22)
        self.assertEqual(plan["lead_hours"], 4.75)
        self.assertFalse(control.preheat_plan(schedule, later, 22, 0, 0.5)["active"])
        self.assertTrue(control.preheat_plan(schedule, later, 22, 0, 0.5,
            {"time": plan["time"], "lead_hours": plan["lead_hours"]})["active"])

    def test_heating_rate_from_sustained_rise(self):
        start = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        samples = [
            (start + timedelta(minutes=minute), 20 + 0.2 * minute / 60, 60, 30)
            for minute in range(0, 186, 5)
        ]
        observed = control.observed_heating_rate(samples, start + timedelta(minutes=185))
        self.assertAlmostEqual(observed, 0.2)
        learned = control.learn_heating_rate(0.25, observed)
        self.assertGreaterEqual(learned, 0.21)
        self.assertLess(learned, 0.25)
        self.assertIsNone(control.observed_heating_rate([(t, temp, 30, base) for t, temp, _, base in samples], start + timedelta(minutes=185)))

    def test_previous_day_and_minute_boundary(self):
        schedule = [[{"minute": 0, "temperature": 20}] for _ in range(7)]
        schedule[0] = [{"minute": 360, "temperature": 18}, {"minute": 1080, "temperature": 21}]
        schedule[1] = [{"minute": 420, "temperature": 19}]
        self.assertEqual(control.schedule_target(schedule, datetime(2026, 9, 22, 6, 59)), 21)
        self.assertEqual(control.schedule_target(schedule, datetime(2026, 9, 22, 7, 0)), 19)

    def test_outdoor_offset_is_bounded(self):
        self.assertEqual(control.outdoor_offset(-20), 1)
        self.assertEqual(control.outdoor_offset(15), -1)
        self.assertEqual(control.outdoor_offset(None), 0)
        self.assertEqual(control.outdoor_offset(-15, 1.5, -0.5), 1.5)
        self.assertEqual(control.outdoor_offset(15, 1.5, -0.5), -0.5)

    def test_rolling_mean_and_next_switch(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        samples = [
            (now - timedelta(hours=25), -100),
            (now - timedelta(hours=1), -5),
            (now, 5),
        ]
        self.assertEqual(control.rolling_mean(samples, now), (0.0, 1.0))
        schedule = [[{"minute": 360, "temperature": 20}] for _ in range(7)]
        schedule[4].append({"minute": 780, "temperature": 22})
        switches = control.next_switches(schedule, now)
        self.assertEqual(switches[0]["temperature"], 22)
        self.assertEqual(switches[0]["time"], "2026-09-25T13:00:00+00:00")


class ControllerTests(unittest.TestCase):
    def test_minimum_valve_is_zero_or_at_least_configured_value(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state = control.ControlState(filtered=18, integral=0, valve=0, last_update=now)
        _, opened = control.update_control(state, 18, 20, now + timedelta(minutes=5), 30, 15, 25)
        self.assertEqual(opened, 25)
        _, rising = control.update_control(state, 18, 20, now + timedelta(minutes=10), 30, 15, 25)
        self.assertGreaterEqual(rising, 25)
        self.assertLessEqual(rising - opened, 1.7)
        state.integral = -90
        _, closing = control.update_control(state, 22, 20, now + timedelta(minutes=15), 30, 15, 25)
        self.assertEqual(closing, 25)
        _, closed = control.update_control(state, 22, 20, now + timedelta(minutes=20), 30, 15, 25)
        self.assertEqual(closed, 0)

    def test_minimum_valve_uses_hysteresis_near_on_off_boundary(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state = control.ControlState(filtered=20, integral=-17, valve=0, last_update=now)
        _, closed = control.update_control(state, 20, 20, now + timedelta(minutes=5), 30, 0, 25)
        self.assertEqual(closed, 0)
        state.integral = -10
        _, opened = control.update_control(state, 20, 20, now + timedelta(minutes=10), 30, 0, 25)
        self.assertEqual(opened, 25)
        state.integral = -17
        _, still_open = control.update_control(state, 20, 20, now + timedelta(minutes=15), 30, 0, 25)
        self.assertEqual(still_open, 25)
        state.integral = -25
        _, held = control.update_control(state, 20, 20, now + timedelta(minutes=20), 30, 0, 25)
        self.assertEqual(held, 25)
        _, closed = control.update_control(state, 20, 20, now + timedelta(minutes=45), 30, 0, 25)
        self.assertEqual(closed, 0)

    def test_minimum_valve_stays_off_briefly_unless_room_is_too_cold(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state = control.ControlState(filtered=20, integral=-10, valve=0,
                                     last_update=now, last_switch_at=now, last_target=20)
        _, held = control.update_control(state, 20, 20, now + timedelta(minutes=5), 30, 0, 25)
        self.assertEqual(held, 0)
        _, opened = control.update_control(state, 19.5, 20, now + timedelta(minutes=10), 30, 0, 25)
        self.assertEqual(opened, 25)

    def test_minimum_valve_can_close_early_when_room_is_too_warm(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state = control.ControlState(filtered=20.6, integral=-30, valve=25,
                                     last_update=now, last_switch_at=now, last_target=20)
        _, closed = control.update_control(state, 20.6, 20, now + timedelta(minutes=5), 30, 0, 25)
        self.assertEqual(closed, 0)

    def test_hold_valve_learns_only_after_stable_period(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        model = list(control.DEFAULT_HOLD_VALVES)
        self.assertEqual(control.hold_valve(model, 15), 28)
        state = control.ControlState(integral=2, valve=30)
        for minute in range(0, 186, 5):
            control.update_control(state, 20, 20, now + timedelta(minutes=minute), 28, 15)
        self.assertTrue(control.stable_for_learning(state, now + timedelta(minutes=185)))
        control.learn_hold_valve(model, 15, state.valve)
        self.assertGreater(model[1], 28)
        self.assertLessEqual(model[1] - 28, 0.4)
        self.assertEqual(model[0], 8)

    def test_equal_temperature_opens_from_zero_toward_hold(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state = control.ControlState(valve=0)
        control.update_control(state, 20, 20, now, 28, 15)
        _, valve = control.update_control(state, 20, 20, now + timedelta(minutes=5), 28, 15)
        self.assertGreater(valve, 0)
        self.assertLessEqual(valve, 1.7)

    def test_sustained_cooling_opens_before_target_is_reached(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        samples = [(now + timedelta(minutes=minute), 21.4 - 0.2 * minute / 45, 21.0)
                   for minute in range(0, 46, 5)]
        state = control.ControlState(filtered=21.3, integral=-30, valve=0,
                                     last_update=now + timedelta(minutes=45), cooling_samples=samples)
        _, valve = control.update_control(state, 21.18, 21.0, now + timedelta(minutes=50), 15, 15, 30)
        self.assertEqual(valve, 30)

    def test_flat_or_sudden_readings_do_not_trigger_early_opening(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        flat = [(now + timedelta(minutes=minute), 21.2, 21.0) for minute in range(0, 46, 5)]
        jump = [(stamp, 21.5 if index < 9 else 21.2, target)
                for index, (stamp, _, target) in enumerate(flat)]
        single_step = [(stamp, 21.2 if index < 7 else 21.1, target)
                       for index, (stamp, _, target) in enumerate(flat)]
        for kind, samples, current in (("flat", flat, 21.2), ("jump", jump, 21.2),
                                       ("single_step", single_step, 21.1)):
            with self.subTest(kind=kind):
                state = control.ControlState(filtered=21.3, integral=-30, valve=0,
                                             last_update=now + timedelta(minutes=45), cooling_samples=samples)
                _, valve = control.update_control(state, current, 21.0, now + timedelta(minutes=50), 15, 15, 30)
                self.assertEqual(valve, 0)

    def test_new_target_does_not_reuse_old_cooling_trend(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        samples = [(now + timedelta(minutes=minute), 21.4 - 0.2 * minute / 45, 21.0)
                   for minute in range(0, 46, 5)]
        state = control.ControlState(filtered=21.3, integral=-30, valve=0,
                                     last_update=now + timedelta(minutes=45), cooling_samples=samples)
        _, valve = control.update_control(state, 21.18, 21.1, now + timedelta(minutes=50), 15, 15, 30)
        self.assertEqual(valve, 0)
        self.assertEqual(len(state.cooling_samples), 1)

    def test_window_hint_needs_cold_air_and_does_not_hold_valve(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        state = control.ControlState(integral=30, valve=30)
        observation = control.ControlState()
        self.assertFalse(control.detect_open_window(observation, 20, 0, now)[0])
        control.update_control(state, 20, 23, now)
        now += timedelta(minutes=5)
        _, valve = control.update_control(state, 20, 23, now)
        self.assertLessEqual(valve, 31.7)
        now += timedelta(minutes=5)
        self.assertTrue(control.detect_open_window(observation, 19.4, 0, now)[0])
        _, after_drop = control.update_control(state, 19.4, 23, now)
        self.assertGreater(after_drop, valve)
        now += timedelta(minutes=5)
        self.assertFalse(control.detect_open_window(observation, 19.0, 18, now)[0])
        self.assertIsNone(observation.window_until)

    def test_window_hint_expires_and_ignores_old_readings(self):
        now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
        observation = control.ControlState()
        control.detect_open_window(observation, 21, 5, now)
        self.assertTrue(control.detect_open_window(observation, 20.4, 5, now + timedelta(minutes=5))[0])
        self.assertFalse(control.detect_open_window(observation, 20.4, 5, now + timedelta(minutes=100))[0])

    def test_invalid_sensor_is_rejected(self):
        with self.assertRaises(ValueError):
            control.update_control(control.ControlState(), float("nan"), 20, datetime.now(timezone.utc))


if __name__ == "__main__":
    unittest.main()
