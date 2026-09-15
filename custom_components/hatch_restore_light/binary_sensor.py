"""Binary sensor: whether the device itself reports as connected to Hatch's cloud."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HatchRestoreConfigEntry
from .coordinator import HatchRestoreCoordinator
from .hatch_cloud import LegacyRestoreDevice
from .hatch_entity import HatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HatchRestoreConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data
    async_add_entities(
        HatchLegacyRestoreConnectedBinarySensorEntity(coordinator, device.thing_name)
        for device in coordinator.rest_devices
        if isinstance(device, LegacyRestoreDevice)
    )


class HatchLegacyRestoreConnectedBinarySensorEntity(HatchEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="connected", translation_key="connected")

    @property
    def available(self) -> bool:
        # Must stay available while the device is offline so it can actually show "disconnected".
        return self.coordinator.last_update_success and self.rest_device is not None

    @property
    def is_on(self) -> bool:
        return self.rest_device.is_online
