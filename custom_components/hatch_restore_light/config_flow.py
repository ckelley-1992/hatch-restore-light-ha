"""Config flow for Hatch Restore Light."""

from __future__ import annotations

from typing import Any
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
import homeassistant.helpers.config_validation as cv

from .const import CONFIG_FLOW_VERSION, DOMAIN, PREFERRED_API_BASE


@config_entries.HANDLERS.register(DOMAIN)
class HatchRestoreConfigFlow(config_entries.ConfigFlow):
    """Handle a config flow for Hatch Restore Light."""

    VERSION = CONFIG_FLOW_VERSION
    CONNECTION_CLASS = config_entries.CONN_CLASS_CLOUD_PUSH

    async def async_step_user(self, user_input: dict[str, Any] | None = None):
        errors: dict[str, str] = {}
        data_schema = vol.Schema(
            {
                vol.Required(CONF_EMAIL): cv.string,
                vol.Required(CONF_PASSWORD): cv.string,
            }
        )

        if user_input is not None:
            api_cloud = None
            try:
                from hatch_rest_api import Hatch
                from hatch_rest_api import hatch as hatch_module

                hatch_module.API_URL = PREFERRED_API_BASE
                api_cloud = Hatch()
                await api_cloud.login(
                    email=user_input[CONF_EMAIL],
                    password=user_input[CONF_PASSWORD],
                )
                await self.async_set_unique_id(user_input[CONF_EMAIL].lower())
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=user_input[CONF_EMAIL],
                    data=user_input,
                )
            except Exception:  # noqa: BLE001
                errors["base"] = "auth"
            finally:
                if api_cloud is not None:
                    await api_cloud.cleanup_client_session()

        return self.async_show_form(step_id="user", data_schema=data_schema, errors=errors)
