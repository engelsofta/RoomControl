"""Exercise valve writes and a manual HA number change without HA installed."""

import importlib.util
import sys
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch


ROOT = Path(__file__).parents[1] / "custom_components" / "room_thermostat"
package = types.ModuleType("room_control_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package


def load(name):
    spec = importlib.util.spec_from_file_location(f"room_control_test.{name}", ROOT / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


load("const")
load("control")
ha = types.ModuleType("homeassistant")
ha.__path__ = []
core = types.ModuleType("homeassistant.core")
core.HomeAssistant = object
core.callback = lambda func: func
helpers = types.ModuleType("homeassistant.helpers")
helpers.__path__ = []
storage = types.ModuleType("homeassistant.helpers.storage")


class FakeStore:
    data = {}

    @classmethod
    def __class_getitem__(cls, _item):
        return cls

    def __init__(self, _hass, _version, key):
        self.key = key

    async def async_load(self):
        return self.data.get(self.key)

    async def async_save(self, value):
        self.data[self.key] = value


storage.Store = FakeStore
util = types.ModuleType("homeassistant.util")
util.__path__ = []
dt = types.ModuleType("homeassistant.util.dt")
now = datetime(2026, 9, 25, 12, tzinfo=timezone.utc)
dt.now = lambda: now
util.dt = dt
sys.modules.update({
    "homeassistant": ha,
    "homeassistant.core": core,
    "homeassistant.helpers": helpers,
    "homeassistant.helpers.storage": storage,
    "homeassistant.util": util,
    "homeassistant.util.dt": dt,
})
manager_module = load("manager")


class State:
    def __init__(self, value, unit=None, user_id=None, **attributes):
        self.state = str(value)
        self.attributes = {**attributes}
        if unit:
            self.attributes["unit_of_measurement"] = unit
        self.context = types.SimpleNamespace(user_id=user_id)


class States(dict):
    def get(self, entity_id):
        return super().get(entity_id)

    def async_set(self, entity_id, value, attributes):
        self[entity_id] = State(value, **attributes)

    def async_remove(self, entity_id):
        self.pop(entity_id, None)


class Services:
    def __init__(self):
        self.calls = []

    async def async_call(self, domain, service, data, blocking):
        self.calls.append((domain, service, data, blocking))


class Hass:
    def __init__(self):
        self.states = States({
            "sensor.room": State(20, "°C"),
            "sensor.outside": State(-15, "°C"),
            "number.valve": State(30, min=0, max=100),
        })
        self.services = Services()
        self.capture_background = False
        self.background_tasks = []

    def async_create_background_task(self, coro, _name):
        if self.capture_background:
            self.background_tasks.append(coro)
        else:
            coro.close()  # Other tests call tick explicitly.


class ManagerTests(IsolatedAsyncioTestCase):
    async def test_valve_exercise_staggers_rooms_and_restores_after_ten_minutes(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.second_valve"] = State(25, min=0, max=100)
        manager = manager_module.RoomManager(hass)
        schedule = [[{"minute": 0, "temperature": 20}] for _ in range(7)]
        first = await manager.save_room({"name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve", "schedule": schedule})
        second = await manager.save_room({"name": "Küche", "actual_entity": "sensor.room", "valve_entity": "number.second_valve", "schedule": schedule})
        first_id, second_id = first["id"], second["id"]
        original_now = dt.now
        try:
            start = now + timedelta(days=8)
            dt.now = lambda: start
            hass.services.calls.clear()
            await manager.exercise_tick()
            self.assertEqual(hass.services.calls[-1][2]["value"], 100)
            self.assertEqual(set(manager.exercise_active), {first_id})
            self.assertEqual(manager.status[first_id]["state"], "exercise")
            self.assertEqual(manager.snapshot()["valve_exercise"][second_id]["next_run"], (start + timedelta(minutes=2)).isoformat())
            hass.states["number.valve"] = State(100, min=0, max=100)

            dt.now = lambda: start + timedelta(minutes=1)
            await manager.exercise_tick()
            self.assertEqual(set(manager.exercise_active), {first_id})
            dt.now = lambda: start + timedelta(minutes=2)
            await manager.exercise_tick()
            self.assertEqual(set(manager.exercise_active), {first_id, second_id})
            hass.states["number.second_valve"] = State(100, min=0, max=100)

            dt.now = lambda: start + timedelta(minutes=10)
            await manager.exercise_tick()
            self.assertNotIn(first_id, manager.exercise_active)
            self.assertIn(second_id, manager.exercise_active)
            self.assertIn(first_id, manager.exercise_last_run)
            self.assertNotEqual(hass.services.calls[-1][2]["value"], 100)
            self.assertTrue(await manager.stop_exercises())
            self.assertFalse(manager.exercise_active)
            self.assertEqual(manager.exercise_last_result[second_id], "interrupted")
            self.assertNotEqual(hass.services.calls[-1][2]["value"], 100)
        finally:
            dt.now = original_now

    async def test_recent_full_open_postpones_exercise_and_restart_restores_active_run(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        room_id = room["id"]
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(days=6)
            manager.handle_state_change(types.SimpleNamespace(data={
                "entity_id": "number.valve", "old_state": State(30), "new_state": State(100),
            }))
            dt.now = lambda: now + timedelta(days=8)
            hass.services.calls.clear()
            await manager.exercise_tick()
            self.assertFalse(manager.exercise_active)
            self.assertFalse(hass.services.calls)

            manager.exercise_last_full[room_id] = now
            await manager.exercise_tick()
            self.assertIn(room_id, manager.exercise_active)
            hass.states["number.valve"] = State(100, min=0, max=100)
            restored = manager_module.RoomManager(hass)
            await restored.load()
            self.assertTrue(restored.exercise_active[room_id]["interrupted"])
            dt.now = lambda: now + timedelta(days=8, minutes=1)
            await restored.exercise_tick()
            self.assertNotIn(room_id, restored.exercise_active)
            self.assertEqual(restored.exercise_last_result[room_id], "interrupted")
            self.assertNotEqual(hass.services.calls[-1][2]["value"], 100)
        finally:
            dt.now = original_now

    async def test_manual_control_postpones_protection_and_failed_start_waits_a_day(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        room_id = room["id"]
        original_now = dt.now
        try:
            start = now + timedelta(days=8)
            dt.now = lambda: start
            manager.runtime[room_id].manual_until = start + timedelta(hours=2)
            await manager.exercise_tick()
            self.assertFalse(manager.exercise_active)
            manager.runtime[room_id].manual_until = None
            with patch.object(manager, "_write_valve", return_value=False):
                await manager.exercise_tick()
            self.assertEqual(manager.exercise_last_result[room_id], "failed")
            self.assertFalse(manager.exercise_active)
            self.assertEqual(manager.snapshot()["valve_exercise"][room_id]["next_run"], (start + timedelta(days=1)).isoformat())
        finally:
            dt.now = original_now

    async def test_exercise_is_skipped_when_disabled_and_can_be_switched_off(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(days=8)
            await manager.save_room({**room, "enabled": False})
            hass.services.calls.clear()
            await manager.exercise_tick()
            self.assertFalse(manager.exercise_active)
            self.assertFalse(hass.services.calls)

            await manager.save_room({**room, "enabled": True})
            await manager.save_settings({"outdoor_entity": "", "valve_exercise_enabled": False})
            hass.services.calls.clear()
            await manager.exercise_tick()
            self.assertFalse(manager.exercise_active)
            self.assertFalse(hass.services.calls)
            restored = manager_module.RoomManager(hass)
            await restored.load()
            self.assertFalse(restored.valve_exercise_enabled)
        finally:
            dt.now = original_now

    async def test_changing_valve_restores_old_exercise_before_switching_entity(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.second_valve"] = State(20, min=0, max=100)
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(days=8)
            await manager.exercise_tick()
            self.assertIn(room["id"], manager.exercise_active)
            hass.services.calls.clear()
            await manager.save_room({**room, "valve_entity": "number.second_valve"})
            self.assertFalse(manager.exercise_active)
            self.assertEqual(hass.services.calls[0][2]["entity_id"], "number.valve")
            self.assertNotEqual(hass.services.calls[0][2]["value"], 100)
            self.assertEqual(manager.rooms[room["id"]]["valve_entity"], "number.second_valve")
        finally:
            dt.now = original_now

    async def test_heating_gate_uses_short_mean_hysteresis_and_closes_valve(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 21}] for _ in range(7)],
        })
        await manager.save_settings({"outdoor_entity": "sensor.outside"})

        def set_outdoor(value):
            hass.states["sensor.outside"] = State(value, "°C")
            manager.outdoor_samples["sensor.outside"] = [
                (now - timedelta(minutes=5 * index), value) for index in range(48, -1, -1)
            ]

        set_outdoor(19)
        await manager.tick()
        self.assertFalse(manager.snapshot()["heating_gate"]["allowed"])
        self.assertEqual(manager.status[room["id"]]["state"], "blocked")
        self.assertEqual(hass.services.calls[-1][2]["value"], 0)

        hass.states["number.valve"] = State(0, min=0, max=100)
        set_outdoor(17)
        await manager.tick()
        self.assertFalse(manager.snapshot()["heating_gate"]["allowed"])

        restored = manager_module.RoomManager(hass)
        await restored.load()
        self.assertFalse(restored.heating_gate_allowed)
        hass.states["sensor.outside"] = State("unavailable")
        await manager.tick()
        self.assertTrue(manager.snapshot()["heating_gate"]["allowed"])
        self.assertEqual(manager.snapshot()["heating_gate"]["reason"], "unavailable")
        set_outdoor(19)
        await manager.tick()
        self.assertFalse(manager.snapshot()["heating_gate"]["allowed"])
        set_outdoor(15)
        await manager.tick()
        self.assertTrue(manager.snapshot()["heating_gate"]["allowed"])
        self.assertEqual(manager.status[room["id"]]["state"], "active")

    async def test_heating_gate_fails_open_without_enough_current_measurements(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 21}] for _ in range(7)],
        })
        await manager.save_settings({"outdoor_entity": "sensor.outside"})
        hass.states["sensor.outside"] = State(20, "°C")
        await manager.tick()
        self.assertTrue(manager.snapshot()["heating_gate"]["allowed"])
        self.assertEqual(manager.status[room["id"]]["state"], "active")
        self.assertEqual(manager.snapshot()["heating_gate"]["reason"], "unavailable")

        with self.assertRaises(ValueError):
            await manager.save_settings({"outdoor_entity": "sensor.outside", "heating_gate_on": 18, "heating_gate_off": 18})
        await manager.save_settings({"outdoor_entity": "sensor.outside", "heating_gate_hours": 6, "heating_gate_on": 15, "heating_gate_off": 17})
        restored = manager_module.RoomManager(hass)
        await restored.load()
        self.assertEqual(restored.heating_gate_hours, 6)
        self.assertEqual(restored.heating_gate_on, 15)
        self.assertEqual(restored.heating_gate_off, 17)

    async def test_room_order_is_validated_and_persists(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.second_valve"] = State(30, min=0, max=100)
        manager = manager_module.RoomManager(hass)
        schedule = [[{"minute": 0, "temperature": 20}] for _ in range(7)]
        first = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": schedule,
        })
        second = await manager.save_room({
            "name": "Küche", "actual_entity": "sensor.room", "valve_entity": "number.second_valve",
            "schedule": schedule,
        })
        with self.assertRaises(ValueError):
            await manager.reorder_rooms([first["id"], first["id"]])
        self.assertEqual([room["id"] for room in manager.snapshot()["rooms"]], [first["id"], second["id"]])
        await manager.reorder_rooms([second["id"], first["id"]])
        self.assertEqual([room["id"] for room in manager.snapshot()["rooms"]], [second["id"], first["id"]])
        restored = manager_module.RoomManager(hass)
        await restored.load()
        self.assertEqual(list(restored.rooms), [second["id"], first["id"]])

    async def test_minimum_valve_is_saved_and_used_for_automatic_output(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.valve"] = State(0, min=0, max=100)
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "minimum_valve": 25,
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertEqual(room["minimum_valve"], 25)
        with self.assertRaises(ValueError):
            manager_module.validate_room({**room, "minimum_valve": 101})
        self.assertEqual(manager_module.validate_room({key: value for key, value in room.items() if key != "minimum_valve"})["minimum_valve"], 0)
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(minutes=5)
            await manager.tick()
            self.assertEqual(manager.status[room["id"]]["valve"], 25)
            self.assertEqual(hass.services.calls[-1][2]["value"], 25)
            restored = manager_module.RoomManager(hass)
            await restored.load()
            self.assertEqual(restored.rooms[room["id"]]["minimum_valve"], 25)
        finally:
            dt.now = original_now

    async def test_automatic_valve_command_uses_whole_percent(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })

        def fractional_control(state, *_args):
            state.valve = 30.6
            state.filtered = 20
            return state, 30.6

        with patch.object(manager_module, "update_control", side_effect=fractional_control):
            await manager.tick()

        self.assertEqual(manager.status[room["id"]]["valve"], 31)
        self.assertEqual(manager.status[room["id"]]["valve_30m"], 31)
        self.assertEqual(hass.services.calls[-1][2]["value"], 31)
        self.assertIsInstance(hass.services.calls[-1][2]["value"], int)

    async def test_disabling_room_closes_valve_and_does_not_repeat_at_zero(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        hass.states["number.valve"] = State(35, min=0, max=100)
        await manager.save_room({**room, "enabled": False})
        self.assertEqual(hass.services.calls[-1][2], {"entity_id": "number.valve", "value": 0})
        self.assertEqual(manager.status[room["id"]]["state"], "paused")
        self.assertNotIn(room["id"], manager.runtime)

        call_count = len(hass.services.calls)
        hass.states["number.valve"] = State(0, min=0, max=100)
        await manager.tick()
        self.assertEqual(len(hass.services.calls), call_count)

    async def test_disabled_room_retries_closing_when_valve_becomes_available(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.valve"] = State("unavailable", min=0, max=100)
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "enabled": False,
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertEqual(manager.status[room["id"]]["state"], "error")
        self.assertFalse(hass.services.calls)

        hass.states["number.valve"] = State(30, min=0, max=100)
        await manager.tick()
        self.assertEqual(hass.services.calls[-1][2]["value"], 0)
        self.assertEqual(manager.status[room["id"]]["state"], "paused")

    async def test_room_deletion_removes_alarm_entity(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        added = []
        removed = []
        manager.add_window_alarm = lambda room: added.append(room["id"])

        async def remove_alarm(room_id):
            removed.append(room_id)

        manager.remove_window_alarm = remove_alarm
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertEqual(added, [room["id"]])
        await manager.delete_room(room["id"])
        self.assertEqual(removed, [room["id"]])
        self.assertNotIn(room["id"], manager.rooms)

    async def test_window_hint_is_visible_without_pausing_control(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        await manager.save_settings({"outdoor_entity": "sensor.outside"})
        room = await manager.save_room({
            "name": "Bad", "actual_entity": "sensor.room", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 23}] for _ in range(7)],
        })
        before = manager.status[room["id"]]["valve"]
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(minutes=5)
            hass.states["sensor.room"] = State(19.4, "°C")
            await manager.tick()
            status = manager.status[room["id"]]
            self.assertTrue(status["window_open"])
            self.assertEqual(status["state"], "active")
            self.assertGreater(status["valve"], before)
        finally:
            dt.now = original_now

    async def test_outdoor_sensor_migrates_to_shared_settings(self):
        FakeStore.data = {}
        hass = Hass()
        legacy = manager_module.validate_room({
            "name": "Bad", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        FakeStore.data["room_thermostat.rooms"] = {"rooms": [legacy]}
        manager = manager_module.RoomManager(hass)
        await manager.load()
        self.assertEqual(manager.snapshot()["settings"]["outdoor_entity"], "sensor.outside")
        await manager.save_settings({"outdoor_entity": ""})
        self.assertEqual(manager.rooms[legacy["id"]]["outdoor_entity"], "")
        self.assertEqual(manager.snapshot()["settings"]["outdoor_entity"], "")
        restored = manager_module.RoomManager(hass)
        await restored.load()
        self.assertEqual(restored.outdoor_entity, "")

    async def test_startup_valve_recovery_refreshes_error_immediately(self):
        FakeStore.data = {}
        hass = Hass()
        unavailable = State("unavailable", min=0, max=100)
        hass.states["number.valve"] = unavailable
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertEqual(manager.status[room["id"]]["state"], "error")
        self.assertIsNone(manager.status[room["id"]]["valve_current"])
        ready = State(30, min=0, max=100)
        hass.states["number.valve"] = ready
        hass.capture_background = True
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": unavailable, "new_state": ready,
        }))
        self.assertEqual(len(hass.background_tasks), 1)
        await hass.background_tasks.pop()
        self.assertEqual(manager.status[room["id"]]["state"], "active")
        hass.states["number.valve"] = unavailable
        await manager.tick()
        hass.states["number.valve"] = ready
        snapshot = await manager.fresh_snapshot()
        self.assertEqual(snapshot["status"][room["id"]]["state"], "active")

    async def test_preheat_target_and_saved_heating_rate(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["sensor.outside"] = State(5, "°C")
        manager = manager_module.RoomManager(hass)
        schedule = [[{"minute": 0, "temperature": 20}] for _ in range(7)]
        schedule[4].append({"minute": 780, "temperature": 22})
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "cold_offset": 0, "warm_offset": 0, "schedule": schedule,
        })
        self.assertTrue(manager.status[room["id"]]["preheat"]["active"])
        self.assertEqual(manager.status[room["id"]]["target"], 22)
        original_now = dt.now
        try:
            hass.states["sensor.room"] = State(22, "°C")
            dt.now = lambda: now + timedelta(minutes=5)
            await manager.tick()
            self.assertTrue(manager.status[room["id"]]["preheat"]["active"])
            restored_preheat = manager_module.RoomManager(hass)
            await restored_preheat.load()
            self.assertEqual(restored_preheat.preheat_locks[room["id"]], manager.preheat_locks[room["id"]])
            dt.now = lambda: now + timedelta(minutes=10)
            await restored_preheat.tick()
            self.assertEqual(restored_preheat.status[room["id"]]["target"], 22)
        finally:
            dt.now = original_now
        for minute in range(0, 186, 5):
            manager._observe_heating(room["id"], now + timedelta(minutes=minute),
                                     20 + 0.2 * minute / 60, 22, 60, 30)
        self.assertEqual(manager.heating_rate_counts[room["id"]], 1)
        await manager._save_learning()
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(minutes=190)
            restored = manager_module.RoomManager(hass)
            await restored.load()
            self.assertEqual(restored.heating_rates[room["id"]], manager.heating_rates[room["id"]])
            self.assertEqual(restored.last_rate_learning[room["id"]], manager.last_rate_learning[room["id"]])
        finally:
            dt.now = original_now

    async def test_small_negative_valve_reading_does_not_block_manual_dialog(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.valve"] = State(-0.1, min=0, max=100)
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertEqual(manager.status[room["id"]]["valve_current"], 0)
        self.assertTrue(await manager.prepare_manual_edit(room["id"]))
        self.assertEqual(manager.status[room["id"]]["state"], "active")
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": hass.states["number.valve"],
            "new_state": State(10, user_id="person", min=0, max=100),
        }))
        hass.states["number.valve"] = State(10, min=0, max=100)
        await manager.tick()
        self.assertEqual(manager.status[room["id"]]["state"], "manual")

    async def test_unavailable_valve_does_not_raise_on_dialog_request(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["number.valve"] = State("unavailable", min=0, max=100)
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertFalse(await manager.prepare_manual_edit(room["id"]))
        self.assertEqual(manager.status[room["id"]]["state"], "error")

    async def test_learned_hold_valve_is_saved_per_room(self):
        FakeStore.data = {}
        hass = Hass()
        hass.states["sensor.outside"] = State(5, "°C")
        manager = manager_module.RoomManager(hass)
        await manager.save_settings({"outdoor_entity": "sensor.outside"})
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "cold_offset": 0, "warm_offset": 0,
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        original_now = dt.now
        try:
            for minute in range(5, 191, 5):
                dt.now = lambda minute=minute: now + timedelta(minutes=minute)
                await manager.tick()
            status = manager.status[room["id"]]
            self.assertGreater(status["learning_count"], 0)
            self.assertGreater(status["hold_valve"], 28)
            restored = manager_module.RoomManager(hass)
            await restored.load()
            self.assertEqual(restored.learning_models[room["id"]], manager.learning_models[room["id"]])
            self.assertEqual(restored.last_learning[room["id"]], manager.last_learning[room["id"]])
            for minute in range(195, 481, 5):
                dt.now = lambda minute=minute: now + timedelta(minutes=minute)
                await restored.tick()
            self.assertEqual(restored.learning_counts[room["id"]], 1)
            for minute in range(485, 541, 5):
                dt.now = lambda minute=minute: now + timedelta(minutes=minute)
                await restored.tick()
            self.assertEqual(restored.learning_counts[room["id"]], 2)
        finally:
            dt.now = original_now

    async def test_dialog_click_only_pauses_after_a_contextless_change_and_can_stop(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        self.assertTrue(await manager.prepare_manual_edit(room["id"]))
        await manager.tick()
        self.assertEqual(manager.status[room["id"]]["state"], "active")
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": State(30),
            "new_state": State(30),
        }))
        self.assertIsNone(manager.runtime[room["id"]].manual_until)
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": State(30),
            "new_state": State(30, user_id="person"),
        }))
        self.assertIsNone(manager.runtime[room["id"]].manual_until)
        manager.recent_auto_commands[room["id"]] = [(now, 35)]
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": State(30),
            "new_state": State(35),
        }))
        self.assertIsNone(manager.runtime[room["id"]].manual_until)
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": State(35),
            "new_state": State(55),
        }))
        hass.states["number.valve"] = State(55, min=0, max=100)
        await manager.tick()
        self.assertEqual(manager.status[room["id"]]["state"], "manual")
        self.assertEqual(manager.status[room["id"]]["valve"], 55)
        restored = manager_module.RoomManager(hass)
        await restored.load()
        self.assertIsNotNone(restored.runtime[room["id"]].manual_until)
        self.assertTrue(await manager.stop_manual_hold(room["id"]))
        self.assertEqual(manager.status[room["id"]]["state"], "active")
        self.assertIsNone(manager.runtime[room["id"]].manual_until)
        restored_after_stop = manager_module.RoomManager(hass)
        await restored_after_stop.load()
        self.assertNotIn(room["id"], restored_after_stop.runtime)
        self.assertEqual(hass.services.calls, [])

    async def test_dialog_watch_expires_without_a_change(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        await manager.prepare_manual_edit(room["id"])
        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(minutes=11)
            await manager.tick()
            self.assertNotIn(room["id"], manager.pending_manual)
            manager.handle_state_change(types.SimpleNamespace(data={
                "entity_id": "number.valve", "old_state": State(30),
                "new_state": State(50),
            }))
            self.assertIsNone(manager.runtime[room["id"]].manual_until)
        finally:
            dt.now = original_now

    async def test_automatic_mean_and_manual_valve_hold(self):
        FakeStore.data = {}
        hass = Hass()
        manager = manager_module.RoomManager(hass)
        await manager.save_settings({"outdoor_entity": "sensor.outside"})
        room = await manager.save_room({
            "name": "Room", "actual_entity": "sensor.room",
            "outdoor_entity": "sensor.outside", "valve_entity": "number.valve",
            "schedule": [[{"minute": 0, "temperature": 20}] for _ in range(7)],
        })
        status = manager.status[room["id"]]
        self.assertEqual(status["outdoor_mean"], -15)
        self.assertEqual(status["target"], 21)
        self.assertEqual(hass.states[status["target_entity"]].state, "21.0")
        self.assertEqual(hass.states[status["target_entity"]].attributes["unit_of_measurement"], "°C")
        self.assertEqual(status["valve_current"], 30)

        previous_valve = hass.states["number.valve"]
        hass.states["number.valve"] = State(45, user_id="person", min=0, max=100)
        manager.handle_state_change(types.SimpleNamespace(data={
            "entity_id": "number.valve", "old_state": previous_valve,
            "new_state": hass.states["number.valve"],
        }))
        await manager.tick()
        status = manager.status[room["id"]]
        self.assertEqual(status["state"], "manual")
        self.assertEqual(status["valve"], 45)
        self.assertEqual(hass.services.calls, [])

        original_now = dt.now
        try:
            dt.now = lambda: now + timedelta(hours=2, minutes=5)
            await manager.tick()
            self.assertEqual(manager.status[room["id"]]["state"], "active")
        finally:
            dt.now = original_now

        restored = manager_module.RoomManager(hass)
        await restored.load()
        self.assertIn("sensor.outside", restored.outdoor_samples)


if __name__ == "__main__":
    import unittest

    unittest.main()
