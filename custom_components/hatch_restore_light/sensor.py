"""Diagnostic sensors: what the device is playing and which routine step it is on.

These are what an automation triggers on to notice the physical tap (routine step 1 -> 2).
"""

from __future__ import annotations

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HatchRestoreConfigEntry
from .coordinator import HatchRestoreCoordinator
from .hatch_cloud import LegacyRestoreDevice
from .hatch_cloud.legacy_restore_device import PLAYING_STATES
from .hatch_entity import HatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HatchRestoreConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data
    entities: list[SensorEntity] = []
    for device in coordinator.rest_devices:
        if isinstance(device, LegacyRestoreDevice):
            entities.append(HatchLegacyRestorePlayingSensorEntity(coordinator, device.thing_name))
            entities.append(HatchLegacyRestoreRoutineStepSensorEntity(coordinator, device.thing_name))
    async_add_entities(entities)


class HatchLegacyRestorePlayingSensorEntity(HatchEntity, SensorEntity):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(PLAYING_STATES)
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(coordinator, thing_name, unique_suffix="playing", translation_key="playing")

    @property
    def native_value(self) -> str | None:
        value = self.rest_device.current_playing
        return value if value in PLAYING_STATES else None


class HatchLegacyRestoreRoutineStepSensorEntity(HatchEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(
            coordinator, thing_name, unique_suffix="routine_step", translation_key="routine_step"
        )

    @property
    def native_value(self) -> int:
        return self.rest_device.routine_step
