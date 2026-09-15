"""Number platform: raw colour id for experiments (disabled by default)."""

from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory
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
        HatchRestoreColorIdNumberEntity(coordinator, device.thing_name)
        for device in coordinator.rest_devices
        if isinstance(device, LegacyRestoreDevice)
    )


class HatchRestoreColorIdNumberEntity(HatchEntity, NumberEntity):
    _attr_native_min_value = 0
    _attr_native_max_value = 65535
    _attr_native_step = 1
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG
    _attr_entity_registry_enabled_default = False

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="color_id", translation_key="color_id")

    @property
    def native_value(self) -> float:
        return float(self.rest_device.color_id)

    def set_native_value(self, value: float) -> None:
        try:
            self.rest_device.set_color_id(int(round(value)))
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err
