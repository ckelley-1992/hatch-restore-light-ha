"""Base entity for Hatch devices."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import AVAILABILITY_REQUIRES_DEVICE_CONNECTED, DOMAIN
from .coordinator import HatchRestoreCoordinator


class HatchEntity(CoordinatorEntity[HatchRestoreCoordinator]):
    """Common Hatch entity wiring: unique id, device info, availability."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: HatchRestoreCoordinator,
        thing_name: str,
        unique_suffix: str,
        translation_key: str | None = None,
    ) -> None:
        super().__init__(coordinator=coordinator, context=thing_name)
        self._thing_name = thing_name
        self._attr_unique_id = f"{thing_name}_{unique_suffix}"
        self._attr_translation_key = translation_key

    @property
    def rest_device(self):
        return self.coordinator.rest_device_by_thing_name(self._thing_name)

    @property
    def device_info(self) -> DeviceInfo:
        device = self.rest_device
        mac = device.mac.lower()
        return DeviceInfo(
            connections={(CONNECTION_NETWORK_MAC, mac), (CONNECTION_NETWORK_MAC, f"{mac[:-1]}0")},
            identifiers={(DOMAIN, self._thing_name)},
            manufacturer="Hatch",
            model=device.__class__.__name__,
            name=device.device_name,
            sw_version=device.firmware_version,
        )

    @property
    def available(self) -> bool:
        if not super().available:
            return False
        device = self.rest_device
        if device is None:
            return False
        return device.is_online or not AVAILABILITY_REQUIRES_DEVICE_CONNECTED
