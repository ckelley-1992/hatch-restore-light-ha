"""Hatch Restore Light integration setup."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .const import PLATFORMS
from .coordinator import HatchRestoreCoordinator

type HatchRestoreConfigEntry = ConfigEntry[HatchRestoreCoordinator]

# Entities dropped in 0.2.0; their registry rows are removed so HomeKit Bridge stops exporting them.
_REMOVED_DOMAINS = {"media_player"}
_REMOVED_UNIQUE_ID_SUFFIXES = ("_sound_level", "_sound_media")
_REMOVED_SWITCH_SUFFIX = "_sound"


def _purge_removed_entities(hass: HomeAssistant, entry: ConfigEntry) -> None:
    registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        unique_id = entity.unique_id or ""
        if (
            entity.domain in _REMOVED_DOMAINS
            or unique_id.endswith(_REMOVED_UNIQUE_ID_SUFFIXES)
            or (entity.domain == "switch" and unique_id.endswith(_REMOVED_SWITCH_SUFFIX))
        ):
            registry.async_remove(entity.entity_id)


async def async_setup_entry(hass: HomeAssistant, entry: HatchRestoreConfigEntry) -> bool:
    """Set up Hatch Restore Light from a config entry."""
    coordinator = HatchRestoreCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator

    _purge_removed_entities(hass, entry)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: HatchRestoreConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    coordinator: HatchRestoreCoordinator | None = getattr(entry, "runtime_data", None)
    if unload_ok and coordinator is not None:
        await coordinator.async_shutdown()
    return unload_ok
