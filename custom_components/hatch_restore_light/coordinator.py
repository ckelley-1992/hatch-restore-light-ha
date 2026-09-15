"""Push-only coordinator wrapping a HatchCloudSession."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_EMAIL, CONF_PASSWORD
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .hatch_cloud import HatchAuthError, HatchCloudError, HatchCloudSession

_LOGGER = logging.getLogger(__name__)


class HatchRestoreCoordinator(DataUpdateCoordinator[dict[str, dict]]):
    """State arrives over MQTT; there is no polling interval.

    ``data`` is ``{thing_name: snapshot_dict}`` and is only used to wake listeners — entities read
    live attributes from the device objects via ``rest_device_by_thing_name``.
    """

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        email = entry.data[CONF_EMAIL]
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN}-{email}",
            update_interval=None,
            always_update=False,
        )
        self.session = HatchCloudSession(
            loop=hass.loop,
            client_session=async_get_clientsession(hass),
            email=email,
            password=entry.data[CONF_PASSWORD],
            on_device_update=self._on_device_update,
            on_health_changed=self._on_health_changed,
            on_auth_failed=self._on_auth_failed,
        )

    async def _async_setup(self) -> None:
        try:
            await self.session.async_start()
        except HatchAuthError as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except (HatchCloudError, TimeoutError, OSError) as err:
            raise UpdateFailed(f"Hatch cloud setup failed: {err}") from err

    async def _async_update_data(self) -> dict[str, dict]:
        """Manual refresh (e.g. ``homeassistant.update_entity``): re-request shadows."""
        if not self.session.healthy:
            raise UpdateFailed(f"Hatch MQTT connection is down: {self.session.health_reason}")
        try:
            await self.session.async_resync()
        except (HatchCloudError, TimeoutError) as err:
            raise UpdateFailed(f"Hatch resync failed: {err}") from err
        return self.session.snapshots()

    @callback
    def _on_device_update(self, thing_name: str) -> None:
        if self.session.healthy:
            self.async_set_updated_data(self.session.snapshots())

    @callback
    def _on_health_changed(self, healthy: bool, reason: str) -> None:
        if healthy:
            self.async_set_updated_data(self.session.snapshots())
        else:
            self.async_set_update_error(HatchCloudError(reason))

    @callback
    def _on_auth_failed(self) -> None:
        assert self.config_entry is not None
        self.config_entry.async_start_reauth(self.hass)

    async def async_shutdown(self) -> None:
        await self.session.async_stop()
        await super().async_shutdown()

    @property
    def rest_devices(self) -> list:
        return self.session.devices

    def rest_device_by_thing_name(self, thing_name: str):
        return self.session.device_by_thing_name(thing_name)
