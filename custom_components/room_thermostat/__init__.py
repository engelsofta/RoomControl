"""Room thermostat integration and sidebar panel."""

from pathlib import Path

try:
    import probatio as vol
except ImportError:  # Home Assistant releases before the schema migration
    import voluptuous as vol

from homeassistant.components import frontend, panel_custom, websocket_api
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_STATE_CHANGED
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from datetime import timedelta

from .const import DOMAIN, PANEL_URL, PANEL_VERSION
from .manager import RoomManager, target_entity_id

PLATFORMS = ["binary_sensor"]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Register the panel API once."""
    websocket_api.async_register_command(hass, ws_rooms)
    websocket_api.async_register_command(hass, ws_save_room)
    websocket_api.async_register_command(hass, ws_save_settings)
    websocket_api.async_register_command(hass, ws_delete_room)
    websocket_api.async_register_command(hass, ws_reorder_rooms)
    websocket_api.async_register_command(hass, ws_manual_prepare)
    websocket_api.async_register_command(hass, ws_manual_stop)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Load saved rooms and start periodic calculation."""
    if entry.title != "Engelsoft RoomControl":
        hass.config_entries.async_update_entry(entry, title="Engelsoft RoomControl")
    manager = RoomManager(hass)
    await manager.load()
    hass.data[DOMAIN] = manager
    frontend_path = Path(__file__).parent / "frontend"
    await hass.http.async_register_static_paths(
        [StaticPathConfig(f"/{DOMAIN}/frontend", str(frontend_path), False)]
    )
    await panel_custom.async_register_panel(
        hass,
        webcomponent_name="room-thermostat-panel",
        frontend_url_path=PANEL_URL,
        module_url=f"/{DOMAIN}/frontend/panel.js?v={PANEL_VERSION}",
        sidebar_title="Engelsoft RoomControl",
        sidebar_icon="mdi:home-thermometer-outline",
        require_admin=True,
        config={},
        config_panel_domain=DOMAIN,
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await manager.tick()
    await manager.exercise_tick()

    async def periodic(_now) -> None:
        await manager.tick()

    unsub = async_track_time_interval(hass, periodic, timedelta(minutes=5))
    entry.async_on_unload(unsub)
    async def exercise_periodic(_now) -> None:
        await manager.exercise_tick()

    exercise_unsub = async_track_time_interval(hass, exercise_periodic, timedelta(minutes=1))
    entry.async_on_unload(exercise_unsub)
    entry.async_on_unload(hass.bus.async_listen(EVENT_STATE_CHANGED, manager.handle_state_change))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Stop control and remove the panel."""
    manager = hass.data.get(DOMAIN)
    if manager is not None and not await manager.stop_exercises():
        return False
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    entry.async_unload()
    frontend.async_remove_panel(hass, PANEL_URL)
    if manager is not None:
        for room_id in manager.rooms:
            hass.states.async_remove(target_entity_id(room_id))
    hass.data.pop(DOMAIN, None)
    return True


def _manager(hass: HomeAssistant) -> RoomManager | None:
    return hass.data.get(DOMAIN)


@websocket_api.websocket_command({vol.Required("type"): "room_thermostat/rooms"})
@websocket_api.async_response
async def ws_rooms(hass, connection, msg) -> None:
    """Return rooms and current calculated values."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    connection.send_result(msg["id"], await manager.fresh_snapshot())


@websocket_api.websocket_command(
    {vol.Required("type"): "room_thermostat/save_settings", vol.Required("settings"): dict}
)
@websocket_api.async_response
async def ws_save_settings(hass, connection, msg) -> None:
    """Update settings shared by all rooms."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    try:
        await manager.save_settings(msg["settings"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_settings", str(err))
        return
    connection.send_result(msg["id"], manager.snapshot())


@websocket_api.websocket_command(
    {vol.Required("type"): "room_thermostat/save_room", vol.Required("room"): dict}
)
@websocket_api.async_response
async def ws_save_room(hass, connection, msg) -> None:
    """Create or edit a room from the administrator panel."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    try:
        room = await manager.save_room(msg["room"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_room", str(err))
        return
    connection.send_result(msg["id"], {"room": room, **manager.snapshot()})


@websocket_api.websocket_command(
    {vol.Required("type"): "room_thermostat/reorder_rooms", vol.Required("room_ids"): list}
)
@websocket_api.async_response
async def ws_reorder_rooms(hass, connection, msg) -> None:
    """Save the room order from the administrator panel."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    try:
        await manager.reorder_rooms(msg["room_ids"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_order", str(err))
        return
    connection.send_result(msg["id"], manager.snapshot())


@websocket_api.websocket_command(
    {vol.Required("type"): "room_thermostat/delete_room", vol.Required("room_id"): str}
)
@websocket_api.async_response
async def ws_delete_room(hass, connection, msg) -> None:
    """Remove a room from the administrator panel."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    try:
        await manager.delete_room(msg["room_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "not_found", str(err))
        return
    connection.send_result(msg["id"], manager.snapshot())


@websocket_api.websocket_command(
    {vol.Required("type"): "room_thermostat/manual_prepare", vol.Required("room_id"): str}
)
@websocket_api.async_response
async def ws_manual_prepare(hass, connection, msg) -> None:
    """Watch for an actual change after opening the HA valve dialog."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    try:
        ready = await manager.prepare_manual_edit(msg["room_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_room", str(err))
        return
    connection.send_result(msg["id"], {"ready": ready})


@websocket_api.websocket_command(
    {vol.Required("type"): "room_thermostat/manual_stop", vol.Required("room_id"): str}
)
@websocket_api.async_response
async def ws_manual_stop(hass, connection, msg) -> None:
    """End a room's manual period and resume its automatic control."""
    if not connection.user.is_admin:
        connection.send_error(msg["id"], "unauthorized", "Administrator required")
        return
    manager = _manager(hass)
    if manager is None:
        connection.send_error(msg["id"], "not_ready", "Integration not ready")
        return
    try:
        await manager.stop_manual_hold(msg["room_id"])
    except ValueError as err:
        connection.send_error(msg["id"], "invalid_room", str(err))
        return
    connection.send_result(msg["id"], manager.snapshot())
