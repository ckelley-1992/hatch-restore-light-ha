"""Base entity for Hatch devices."""

from __future__ import annotations

from homeassistant.helpers.entity import DeviceInfo
import homeassistant.helpers.device_registry as dr
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import HatchRestoreDataUpdateCoordinator


class HatchEntity(CoordinatorEntity[HatchRestoreDataUpdateCoordinator]):
    """Common Hatch entity wiring."""

    def __init__(self, coordinator: HatchRestoreDataUpdateCoordinator, thing_name: str, entity_type: str):
        super().__init__(coordinator=coordinator, context=thing_name)
        self._attr_unique_id = f"{thing_name}_{entity_type.lower().replace(' ', '_')}"
        self._attr_name = f"{self.rest_device.device_name} {entity_type}"

        self._attr_device_info = DeviceInfo(
            connections={
                (dr.CONNECTION_NETWORK_MAC, self.rest_device.mac.lower()),
                (dr.CONNECTION_NETWORK_MAC, f"{self.rest_device.mac[:-1].lower()}0"),
            },
            identifiers={(DOMAIN, thing_name)},
            manufacturer="Hatch",
            model=self.rest_device.__class__.__name__,
            name=self.rest_device.device_name,
            sw_version=self.rest_device.firmware_version,
        )

    @property
    def rest_device(self):
        return self.coordinator.rest_device_by_thing_name(self.coordinator_context)

    def _handle_coordinator_update(self) -> None:
        self.schedule_update_ha_state()
