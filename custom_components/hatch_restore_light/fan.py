"""Fan platform: the Gen 1 Restore sound machine as a fan with a speed slider.

A fan (rather than a dimmer light) keeps the sound out of Apple Home's "turn off the lights" /
room-light scenes while still giving a volume slider and Siri "set the sound to 40%".
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.fan import FanEntity, FanEntityFeature
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
        HatchLegacyRestoreSoundFanEntity(coordinator, device.thing_name)
        for device in coordinator.rest_devices
        if isinstance(device, LegacyRestoreDevice)
    )


class HatchLegacyRestoreSoundFanEntity(HatchEntity, FanEntity):
    """Sound on/off + volume (0-100 %)."""

    _attr_supported_features = (
        FanEntityFeature.SET_SPEED | FanEntityFeature.TURN_ON | FanEntityFeature.TURN_OFF
    )
    _attr_speed_count = 100

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="sound", translation_key="sound")

    @property
    def is_on(self) -> bool:
        return self.rest_device.is_sound_active

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        device = self.rest_device
        return {"sound_id": device.sound_id, "last_active_sound_id": device.last_active_sound_id}

    @property
    def percentage(self) -> int:
        if not self.rest_device.is_sound_active:
            return 0
        return int(round(self.rest_device.sound_volume_percent))

    def set_percentage(self, percentage: int) -> None:
        try:
            if percentage <= 0:
                self.rest_device.set_sound(False)
            else:
                self.rest_device.set_sound(True, volume_pct=percentage)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err

    def turn_on(self, percentage: int | None = None, preset_mode: str | None = None, **kwargs: Any) -> None:
        try:
            self.rest_device.set_sound(True, volume_pct=percentage)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err

    def turn_off(self, **kwargs: Any) -> None:
        try:
            self.rest_device.set_sound(False)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err
