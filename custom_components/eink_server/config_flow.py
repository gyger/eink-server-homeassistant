"""Config flow: one listener for all tablets."""

from typing import Any

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PORT
from homeassistant.helpers import config_validation as cv
import voluptuous as vol

from .const import DEFAULT_PORT, DOMAIN

SCHEMA_USER = vol.Schema({vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port})


class EinkServerConfigFlow(ConfigFlow, domain=DOMAIN):
    """Ask for the TCP port tablets connect to."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle the initial step."""
        if user_input is None:
            return self.async_show_form(step_id="user", data_schema=SCHEMA_USER)

        await self.async_set_unique_id(str(user_input[CONF_PORT]))
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Tablets on port {user_input[CONF_PORT]}", data=user_input
        )
