"""Config flow for Hatch Restore Light."""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

from aiohttp import ClientError
from hatch_rest_api import Hatch
from hatch_rest_api import hatch as hatch_module
from hatch_rest_api.errors import AuthError, RateError
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
import voluptuous as vol

from .const import CONFIG_FLOW_VERSION, DOMAIN, PREFERRED_API_BASE

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
    }
)
REAUTH_SCHEMA = vol.Schema({vol.Required(CONF_PASSWORD): cv.string})


class HatchRestoreConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Hatch Restore Light."""

    VERSION = CONFIG_FLOW_VERSION

    async def _async_validate(self, email: str, password: str) -> str | None:
        """Try a Hatch login; return an error key or None on success."""
        hatch_module.API_URL = PREFERRED_API_BASE
        api = Hatch(client_session=async_get_clientsession(self.hass))
        try:
            await api.login(email=email, password=password)
        except AuthError:
            return "invalid_auth"
        except RateError:
            return "rate_limited"
        except (ClientError, TimeoutError) as err:
            # hatch_rest_api wraps API-level failures (bad password included) in ClientError.
            if "Login failed" in str(err):
                return "invalid_auth"
            _LOGGER.debug("Hatch login failed: %s", err)
            return "cannot_connect"
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Unexpected error validating Hatch credentials")
            return "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            email = user_input[CONF_EMAIL].strip()
            await self.async_set_unique_id(email.lower())
            self._abort_if_unique_id_configured()
            error = await self._async_validate(email, user_input[CONF_PASSWORD])
            if error is None:
                return self.async_create_entry(
                    title=email,
                    data={CONF_EMAIL: email, CONF_PASSWORD: user_input[CONF_PASSWORD]},
                )
            errors["base"] = error

        return self.async_show_form(step_id="user", data_schema=USER_SCHEMA, errors=errors)

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Handle re-authentication when the stored password stops working."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            error = await self._async_validate(entry.data[CONF_EMAIL], user_input[CONF_PASSWORD])
            if error is None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: user_input[CONF_PASSWORD]}
                )
            errors["base"] = error

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"email": entry.data[CONF_EMAIL]},
        )
