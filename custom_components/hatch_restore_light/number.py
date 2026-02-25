"""Number platform for Hatch Restore devices."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
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
    """Set up Hatch Restore number entities."""
    coordinator: HatchRestoreDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[NumberEntity] = []

    for rest_device in coordinator.rest_devices:
        if isinstance(rest_device, LegacyRestoreDevice):
            entities.append(HatchRestoreColorIdNumberEntity(coordinator, rest_device.thing_name))

    async_add_entities(entities)


class HatchRestoreColorIdNumberEntity(HatchEntity, NumberEntity):
    """Raw color ID selector for legacy Restore."""

    _attr_native_min_value = 0
    _attr_native_max_value = 65535
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: HatchRestoreDataUpdateCoordinator, thing_name: str):
        super().__init__(coordinator=coordinator, thing_name=thing_name, entity_type="Color ID")

    @property
    def native_value(self) -> float:
        return float(self.rest_device.color_id)

    def set_native_value(self, value: float) -> None:
        self.rest_device.set_color_id(int(round(value)))
