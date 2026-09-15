"""Button platform: advance the running routine one step (what a tap on the device does)."""

from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import HatchRestoreConfigEntry
from .coordinator import HatchRestoreCoordinator
from .hatch_cloud import HatchCloudError, LegacyRestoreDevice, NotInRoutineError
from .hatch_entity import HatchEntity


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: HatchRestoreConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator = config_entry.runtime_data
    async_add_entities(
        HatchLegacyRestoreNextStepButtonEntity(coordinator, device.thing_name)
        for device in coordinator.rest_devices
        if isinstance(device, LegacyRestoreDevice)
    )


class HatchLegacyRestoreNextStepButtonEntity(HatchEntity, ButtonEntity):
    def __init__(self, coordinator: HatchRestoreCoordinator, thing_name: str) -> None:
        super().__init__(
            coordinator, thing_name, unique_suffix="next_routine_step", translation_key="next_routine_step"
        )

    def press(self) -> None:
        try:
            self.rest_device.advance_routine()
        except NotInRoutineError as err:
            raise HomeAssistantError("Sleep mode is not running, nothing to advance") from err
        except HatchCloudError as err:
            raise HomeAssistantError(str(err)) from err
