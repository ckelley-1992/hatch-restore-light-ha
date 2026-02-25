"""Switch platform for Hatch Restore devices."""

from __future__ import annotations

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HatchRestoreDataUpdateCoordinator
from .hatch_entity import HatchEntity
from .legacy_restore_device import LegacyRestoreDevice


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hatch Restore switches."""
    coordinator: HatchRestoreDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[SwitchEntity] = []

    for rest_device in coordinator.rest_devices:
        if isinstance(rest_device, LegacyRestoreDevice):
            entities.append(HatchRestoreSoundSwitchEntity(coordinator, rest_device.thing_name))

    async_add_entities(entities)


class HatchRestoreSoundSwitchEntity(HatchEntity, SwitchEntity):
    """Independent sound toggle for legacy Restore."""

    def __init__(self, coordinator: HatchRestoreDataUpdateCoordinator, thing_name: str):
        super().__init__(coordinator=coordinator, thing_name=thing_name, entity_type="Sound")

    @property
    def is_on(self) -> bool | None:
        return self.rest_device.sound_enabled

    def turn_on(self, **kwargs) -> None:
        self.rest_device.set_sound_enabled(True)

    def turn_off(self, **kwargs) -> None:
        self.rest_device.set_sound_enabled(False)

