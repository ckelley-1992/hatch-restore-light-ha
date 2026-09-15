"""Select platform: pick which sound the Gen 1 plays, by name.

HA-only — leave it out of the HomeKit Bridge include list (HomeKit would render 30+ switches).
"""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
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
        HatchLegacyRestoreSoundSelectEntity(coordinator, device.thing_name)
        for device in coordinator.rest_devices
        if isinstance(device, LegacyRestoreDevice)
    )


class HatchLegacyRestoreSoundSelectEntity(HatchEntity, SelectEntity):
    """Current sound (or the one the next "on" will play), chosen from the Hatch catalog."""

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="sound_track", translation_key="sound_track")

    @property
    def options(self) -> list[str]:
        return list(self.rest_device.sound_catalog.values())

    @property
    def current_option(self) -> str | None:
        title = self.rest_device.selected_sound_title
        return title if title in self.rest_device.sound_catalog.values() else None

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        return {"sound_id": self.rest_device.selected_sound_id}

    def select_option(self, option: str) -> None:
        device = self.rest_device
        sound_id = next((sid for sid, title in device.sound_catalog.items() if title == option), None)
        if sound_id is None:
            raise HomeAssistantError(f"Unknown sound: {option}")
        try:
            device.select_sound(sound_id)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err
