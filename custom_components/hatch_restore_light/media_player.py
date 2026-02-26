"""Media player platform for Hatch Restore devices."""

from __future__ import annotations

from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature
from homeassistant.components.media_player.const import MediaPlayerState
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
    """Set up Hatch Restore media player entities."""
    coordinator: HatchRestoreDataUpdateCoordinator = hass.data[DOMAIN][config_entry.entry_id]
    entities: list[MediaPlayerEntity] = []

    for rest_device in coordinator.rest_devices:
        if isinstance(rest_device, LegacyRestoreDevice):
            entities.append(HatchRestoreSoundMediaPlayerEntity(coordinator, rest_device.thing_name))

    async_add_entities(entities)


class HatchRestoreSoundMediaPlayerEntity(HatchEntity, MediaPlayerEntity):
    """Combined sound on/off + volume control for legacy Restore."""

    _attr_supported_features = (
        MediaPlayerEntityFeature.PLAY
        | MediaPlayerEntityFeature.STOP
        | MediaPlayerEntityFeature.TURN_ON
        | MediaPlayerEntityFeature.TURN_OFF
        | MediaPlayerEntityFeature.VOLUME_SET
    )

    def __init__(self, coordinator: HatchRestoreDataUpdateCoordinator, thing_name: str):
        super().__init__(coordinator=coordinator, thing_name=thing_name, entity_type="Sound Media")

    @property
    def state(self) -> MediaPlayerState:
        return MediaPlayerState.PLAYING if self.rest_device.is_sound_active else MediaPlayerState.IDLE

    @property
    def volume_level(self) -> float:
        return max(0.0, min(1.0, self.rest_device.sound_volume / 65535.0))

    def turn_on(self) -> None:
        self.rest_device.set_sound_enabled(True)

    def turn_off(self) -> None:
        self.rest_device.set_sound_enabled(False)

    def media_play(self) -> None:
        self.rest_device.set_sound_enabled(True)

    def media_stop(self) -> None:
        self.rest_device.set_sound_enabled(False)

    def set_volume_level(self, volume: float) -> None:
        self.rest_device.set_sound_volume_percent(volume * 100.0)
