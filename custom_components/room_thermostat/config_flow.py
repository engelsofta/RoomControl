"""UI setup for the room thermostat."""

from homeassistant import config_entries

from .const import DOMAIN


class RoomThermostatConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create the single local integration entry."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Set up the integration without external credentials."""
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        if user_input is not None:
            return self.async_create_entry(title="Engelsoft RoomControl", data={})
        return self.async_show_form(step_id="user")
