"""Check window-alarm identity, state changes, and room deletion."""

import importlib.util
import sys
import types
from pathlib import Path
from unittest import IsolatedAsyncioTestCase


ROOT = Path(__file__).parents[1] / "custom_components" / "room_thermostat"
package = types.ModuleType("room_alarm_test")
package.__path__ = [str(ROOT)]
sys.modules[package.__name__] = package
constants = types.ModuleType("room_alarm_test.const")
constants.DOMAIN = "room_thermostat"
sys.modules[constants.__name__] = constants


class FakeBinarySensorEntity:
    async def async_added_to_hass(self):
        pass

    async def async_will_remove_from_hass(self):
        pass

    async def async_remove(self):
        self.removed = True
        await self.async_will_remove_from_hass()

    def async_write_ha_state(self):
        self.writes = getattr(self, "writes", 0) + 1


components = types.ModuleType("homeassistant.components")
components.__path__ = []
binary = types.ModuleType("homeassistant.components.binary_sensor")
binary.BinarySensorEntity = FakeBinarySensorEntity
binary.BinarySensorDeviceClass = types.SimpleNamespace(PROBLEM="problem")
components.binary_sensor = binary
helpers = types.ModuleType("homeassistant.helpers")
helpers.__path__ = []
registry_module = types.ModuleType("homeassistant.helpers.entity_registry")


class Registry:
    def __init__(self):
        self.removed = []

    def async_get_entity_id(self, platform, domain, unique_id):
        return f"{platform}.engelsoft_roomcontrol_bad_fensteralarm" if (domain, unique_id) == ("room_thermostat", "room-1_window_alarm") else None

    def async_remove(self, entity_id):
        self.removed.append(entity_id)


registry = Registry()
registry_module.async_get = lambda _hass: registry
helpers.entity_registry = registry_module
sys.modules.update({
    "homeassistant.components": components,
    "homeassistant.components.binary_sensor": binary,
    "homeassistant.helpers": helpers,
    "homeassistant.helpers.entity_registry": registry_module,
})
spec = importlib.util.spec_from_file_location("room_alarm_test.binary_sensor", ROOT / "binary_sensor.py")
alarm_module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = alarm_module
spec.loader.exec_module(alarm_module)


class AlarmTests(IsolatedAsyncioTestCase):
    async def test_alarm_is_registered_and_removed_with_room(self):
        registry.removed.clear()
        manager = types.SimpleNamespace(
            rooms={"room-1": {"id": "room-1", "name": "OG Bad"}},
            status={"room-1": {"actual": 21.0, "outdoor": 5.0, "window_open": False}},
            window_alarm_entities={},
        )
        hass = types.SimpleNamespace(data={"room_thermostat": manager})
        added = []
        await alarm_module.async_setup_entry(hass, object(), lambda entities: added.extend(entities))
        sensor = added[0]
        self.assertEqual(sensor.name, "Engelsoft RoomControl OG Bad Fensteralarm")
        self.assertEqual(sensor.unique_id if hasattr(sensor, "unique_id") else sensor._attr_unique_id, "room-1_window_alarm")
        self.assertEqual(sensor._attr_device_class, "problem")
        self.assertFalse(sensor.is_on)
        await sensor.async_added_to_hass()
        manager.status["room-1"]["window_open"] = True
        sensor.refresh()
        self.assertTrue(sensor.is_on)
        self.assertEqual(sensor.writes, 1)
        await manager.remove_window_alarm("room-1")
        self.assertTrue(sensor.removed)
        self.assertEqual(registry.removed, ["binary_sensor.engelsoft_roomcontrol_bad_fensteralarm"])

