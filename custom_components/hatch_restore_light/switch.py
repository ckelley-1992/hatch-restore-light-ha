"""Switch platform: start/stop the Hatch-app routine ("sleep mode")."""

from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HatchRestoreConfigEntry
from .coordinator import HatchRestoreCoordinator
from .hatch_cloud import HatchCloudError, LegacyRestoreDevice
from .hatch_entity import HatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HatchRestoreConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data
    async_add_entities(
        HatchRestoreSleepModeSwitchEntity(coordinator, device.thing_name)
        for device in coordinator.rest_devices
        if isinstance(device, LegacyRestoreDevice)
    )


class HatchRestoreSleepModeSwitchEntity(HatchEntity, SwitchEntity):
    """On = the Hatch-app routine is playing (starts at step 1); off = stop everything."""

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="sleep_mode", translation_key="sleep_mode")

    @property
    def is_on(self) -> bool:
        return self.rest_device.is_sleep_mode

    def turn_on(self, **kwargs: Any) -> None:
        try:
            self.rest_device.start_routine(1)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err

    def turn_off(self, **kwargs: Any) -> None:
        try:
            self.rest_device.stop()
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err
