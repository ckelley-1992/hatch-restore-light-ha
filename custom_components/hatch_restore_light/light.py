"""Light platform for Hatch Restore devices."""

from __future__ import annotations

import logging
from typing import Any

from hatch_rest_api import RestoreIot, RestoreV5
from hatch_rest_api.restore_v4 import RestoreV4
from homeassistant.components.light import ATTR_BRIGHTNESS, ATTR_RGBW_COLOR, ColorMode, LightEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HatchRestoreConfigEntry
from .coordinator import HatchRestoreCoordinator
from .hatch_cloud import HatchCloudError, LegacyRestoreDevice
from .hatch_entity import HatchEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HatchRestoreConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data
    entities: list[LightEntity] = []
    for rest_device in coordinator.rest_devices:
        if isinstance(rest_device, LegacyRestoreDevice):
            entities.append(HatchLegacyRestoreLightEntity(coordinator, rest_device.thing_name))
        elif isinstance(rest_device, (RestoreIot, RestoreV4, RestoreV5)):
            entities.append(HatchRestoreIotLightEntity(coordinator, rest_device.thing_name))
    async_add_entities(entities)


class HatchLegacyRestoreLightEntity(HatchEntity, LightEntity):
    """Gen 1 Restore light: on/off + brightness.

    While a routine is playing, changes are applied without leaving the routine (see
    ``LegacyRestoreDevice._write``).
    """

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="light", translation_key="light")

    @property
    def is_on(self) -> bool:
        return self.rest_device.is_light_active

    @property
    def extra_state_attributes(self) -> dict[str, int]:
        device = self.rest_device
        return {"color_id": device.color_id, "last_active_color_id": device.last_active_color_id}

    @property
    def brightness(self) -> int:
        return int(round(self.rest_device.color_intensity / 65535 * 255))

    def turn_on(self, **kwargs: Any) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        brightness_pct = None if brightness is None else brightness / 255 * 100
        try:
            self.rest_device.set_light(True, brightness_pct=brightness_pct)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err

    def turn_off(self, **kwargs: Any) -> None:
        try:
            self.rest_device.set_light(False)
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err


class HatchRestoreIotLightEntity(HatchEntity, LightEntity):
    """RGBW light for the newer Restore models handled by hatch_rest_api."""

    _attr_color_mode = ColorMode.RGBW
    _attr_supported_color_modes = {ColorMode.RGBW}

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="light", translation_key="light")
        self._last_on = {"r": 127, "g": 127, "b": 127, "w": 127, "brightness": 50}

    @property
    def is_on(self) -> bool:
        device = self.rest_device
        if device.is_light_on:
            self._last_on = {
                "r": device.red,
                "g": device.green,
                "b": device.blue,
                "w": device.white,
                "brightness": device.brightness,
            }
        return device.is_light_on

    @property
    def brightness(self) -> int:
        return int(round(self.rest_device.brightness / 100 * 255))

    @property
    def rgbw_color(self) -> tuple[int, int, int, int]:
        device = self.rest_device
        return (device.red, device.green, device.blue, device.white)

    def turn_on(self, **kwargs: Any) -> None:
        if not kwargs:
            last = self._last_on
            self.rest_device.set_color(last["r"], last["g"], last["b"], last["w"], last["brightness"])
            return
        brightness = round(kwargs.get(ATTR_BRIGHTNESS, self.brightness) / 255 * 100)
        red, green, blue, white = kwargs.get(ATTR_RGBW_COLOR, self.rgbw_color)
        # Match Hatch app behavior: when white is set, add offset to RGB.
        if white and white > 0:
            offset = max(0, min(min(white, 255 - max(red, green, blue)), 255))
            red, green, blue = red + offset, green + offset, blue + offset
        self.rest_device.set_color(red, green, blue, white, brightness)

    def turn_off(self, **kwargs: Any) -> None:
        self.rest_device.turn_light_off()
