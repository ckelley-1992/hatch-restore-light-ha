"""Light platform for Hatch Restore devices."""

from __future__ import annotations

import logging

from hatch_rest_api import RestoreIot, RestoreV5
from hatch_rest_api.restore_v4 import RestoreV4
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGBW_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN
from .coordinator import HatchRestoreDataUpdateCoordinator
from .hatch_entity import HatchEntity
from .legacy_restore_device import LegacyRestoreDevice

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Hatch Restore light entities."""
    coordinator: HatchRestoreDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[LightEntity] = []

    for rest_device in coordinator.rest_devices:
        if isinstance(rest_device, LegacyRestoreDevice):
            entities.append(HatchRestoreRoutineLightEntity(coordinator, rest_device.thing_name))
        elif isinstance(rest_device, (RestoreIot, RestoreV4, RestoreV5)):
            entities.append(HatchRestoreLightEntity(coordinator, rest_device.thing_name))

    async_add_entities(entities)


class HatchRestoreLightEntity(HatchEntity, LightEntity):
    """Expose a Hatch Restore light as a Home Assistant light entity."""

    _attr_color_mode = ColorMode.RGBW
    _attr_supported_color_modes = {ColorMode.RGBW}

    _last_light_on_colors = {
        "r": 127,
        "g": 127,
        "b": 127,
        "w": 127,
        "brightness": 50,
    }

    def __init__(self, coordinator: HatchRestoreDataUpdateCoordinator, thing_name: str):
        super().__init__(coordinator=coordinator, thing_name=thing_name, entity_type="Light")

    @property
    def is_on(self) -> bool | None:
        if self.rest_device.is_light_on:
            self._last_light_on_colors["r"] = self.rest_device.red
            self._last_light_on_colors["g"] = self.rest_device.green
            self._last_light_on_colors["b"] = self.rest_device.blue
            self._last_light_on_colors["w"] = self.rest_device.white
            self._last_light_on_colors["brightness"] = self.rest_device.brightness
        return self.rest_device.is_light_on

    @property
    def brightness(self) -> int | None:
        return int(round(self.rest_device.brightness / 100 * 255.0, 0))

    @property
    def rgbw_color(self) -> tuple[int, int, int, int]:
        return (
            self.rest_device.red,
            self.rest_device.green,
            self.rest_device.blue,
            self.rest_device.white,
        )

    def turn_on(self, **kwargs) -> None:
        _LOGGER.debug("Hatch light turn_on args=%s", kwargs)
        if not kwargs:
            self.rest_device.set_color(
                self._last_light_on_colors["r"],
                self._last_light_on_colors["g"],
                self._last_light_on_colors["b"],
                self._last_light_on_colors["w"],
                self._last_light_on_colors["brightness"],
            )
            return

        brightness = round(kwargs.get(ATTR_BRIGHTNESS, self.brightness) / 255 * 100)
        red, green, blue, white = kwargs.get(ATTR_RGBW_COLOR, self.rgbw_color)

        # Match Hatch app behavior: when white is set, add offset to RGB.
        if white and white > 0:
            max_value = max(red, green, blue)
            offset = max(0, min(min(white, 255 - max_value), 255))
            red += offset
            green += offset
            blue += offset

        self.rest_device.set_color(red, green, blue, white, brightness)

    def turn_off(self, **kwargs) -> None:
        self.rest_device.turn_light_off()


class HatchRestoreRoutineLightEntity(HatchEntity, LightEntity):
    """Legacy Restore independent light behavior via remote mode."""

    _attr_color_mode = ColorMode.BRIGHTNESS
    _attr_supported_color_modes = {ColorMode.BRIGHTNESS}

    def __init__(self, coordinator: HatchRestoreDataUpdateCoordinator, thing_name: str):
        super().__init__(coordinator=coordinator, thing_name=thing_name, entity_type="Light")

    @property
    def is_on(self) -> bool | None:
        return self.rest_device.is_on

    @property
    def brightness(self) -> int | None:
        return int(round((self.rest_device.color_intensity / 65535) * 255))

    def turn_on(self, **kwargs) -> None:
        brightness = kwargs.get(ATTR_BRIGHTNESS)
        if brightness is not None:
            brightness_percent = (brightness / 255.0) * 100.0
            self.rest_device.set_light_brightness_percent(brightness_percent)
            return
        self.rest_device.set_light_enabled(True)

    def turn_off(self, **kwargs) -> None:
        self.rest_device.set_light_enabled(False)
