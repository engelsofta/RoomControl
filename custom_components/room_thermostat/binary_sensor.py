"""Window alarm entities for Engelsoft RoomControl."""

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.helpers import entity_registry as er

from .const import DOMAIN


def window_alarm_unique_id(room_id: str) -> str:
    """Keep the registry identity stable when a room is renamed."""
    return f"{room_id}_window_alarm"


async def async_setup_entry(hass, entry, async_add_entities) -> None:
    """Expose one alarm per room and accept rooms added from the panel."""
    manager = hass.data[DOMAIN]

    def add_room(room: dict) -> None:
        sensor = RoomWindowAlarm(manager, room["id"])
        manager.window_alarm_entities[room["id"]] = sensor
        async_add_entities([sensor])

    async def remove_room(room_id: str) -> None:
        sensor = manager.window_alarm_entities.pop(room_id, None)
        if sensor is not None and sensor._added:
            await sensor.async_remove()
        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id(
            "binary_sensor", DOMAIN, window_alarm_unique_id(room_id)
        )
        if entity_id is not None:
            registry.async_remove(entity_id)

    manager.add_window_alarm = add_room
    manager.remove_window_alarm = remove_room
    for room in manager.rooms.values():
        add_room(room)


class RoomWindowAlarm(BinarySensorEntity):
    """Display-only alarm for a likely open window."""

    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_should_poll = False

    def __init__(self, manager, room_id: str) -> None:
        self.manager = manager
        self.room_id = room_id
        self._attr_unique_id = window_alarm_unique_id(room_id)
        self._added = False
        self._last_published = None

    @property
    def name(self) -> str:
        room = self.manager.rooms.get(self.room_id)
        return f"Engelsoft RoomControl {room['name']} Fensteralarm" if room else "Engelsoft RoomControl Fensteralarm"

    @property
    def is_on(self) -> bool | None:
        status = self.manager.status.get(self.room_id)
        if status is None or status.get("actual") is None or status.get("outdoor") is None:
            return None
        return bool(status.get("window_open", False))

    @property
    def icon(self) -> str:
        state = self.is_on
        return "mdi:window-open-variant" if state else "mdi:window-question" if state is None else "mdi:window-closed-variant"

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self._added = True
        self._last_published = (self.is_on, self.name)

    async def async_will_remove_from_hass(self) -> None:
        self._added = False
        await super().async_will_remove_from_hass()

    def refresh(self) -> None:
        current = (self.is_on, self.name)
        if self._added and current != self._last_published:
            self._last_published = current
            self.async_write_ha_state()
