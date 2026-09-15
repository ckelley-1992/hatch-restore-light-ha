"""Config flow tests."""

from __future__ import annotations

from unittest.mock import patch

from aiohttp import ClientError
from hatch_rest_api.errors import RateError
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hatch_restore_light.const import DOMAIN

LOGIN = "custom_components.hatch_restore_light.config_flow.Hatch.login"


async def test_user_flow_success(hass: HomeAssistant, aioclient_mock) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    with (
        patch(LOGIN, return_value="token"),
        patch("custom_components.hatch_restore_light.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {"email": " User@Example.com ", "password": "pw"}
        )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "User@Example.com"
    assert result["data"] == {"email": "User@Example.com", "password": "pw"}
    assert result["result"].unique_id == "user@example.com"


async def test_user_flow_errors(hass: HomeAssistant, aioclient_mock) -> None:
    for index, (side_effect, expected) in enumerate(
        (
            (ClientError("api error:Login failed for user"), "invalid_auth"),
            (RateError("429"), "rate_limited"),
            (ClientError("boom"), "cannot_connect"),
        )
    ):
        result = await hass.config_entries.flow.async_init(
            DOMAIN, context={"source": config_entries.SOURCE_USER}
        )
        with patch(LOGIN, side_effect=side_effect):
            result = await hass.config_entries.flow.async_configure(
                result["flow_id"], {"email": f"user{index}@example.com", "password": "pw"}
            )
        assert result["type"] is FlowResultType.FORM
        assert result["errors"] == {"base": expected}


async def test_reauth_flow(hass: HomeAssistant, aioclient_mock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id="a@b.c", data={"email": "a@b.c", "password": "old"})
    entry.add_to_hass(hass)
    result = await entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    with (
        patch(LOGIN, return_value="token"),
        patch("custom_components.hatch_restore_light.async_setup_entry", return_value=True),
    ):
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"password": "new"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert entry.data["password"] == "new"
