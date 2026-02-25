"""Legacy Restore (`product=restore`) device model."""

from __future__ import annotations

from hatch_rest_api.shadow_client_subscriber import ShadowClientSubscriberMixin
from hatch_rest_api.util import safely_get_json_value


class LegacyRestoreDevice(ShadowClientSubscriberMixin):
    """Implements minimal on/off behavior used by Homebridge for legacy Restore."""

    firmware_version: str | None = None
    current_playing: str = "none"
    is_online: bool = False
    color_enabled: bool = False
    color_id: int = 229
    color_intensity: int = 32767
    sound_enabled: bool = False
    sound_id: int = 10040
    sound_volume: int = 32767
    last_nonzero_sound_volume: int = 32767

    def _update_local_state(self, state):
        if safely_get_json_value(state, "deviceInfo.f") is not None:
            self.firmware_version = safely_get_json_value(state, "deviceInfo.f")
        if safely_get_json_value(state, "content.playing") is not None:
            self.current_playing = safely_get_json_value(state, "content.playing")
        if safely_get_json_value(state, "connected") is not None:
            self.is_online = safely_get_json_value(state, "connected", bool)
        if safely_get_json_value(state, "color.enabled") is not None:
            self.color_enabled = safely_get_json_value(state, "color.enabled", bool)
        if safely_get_json_value(state, "color.id") is not None:
            self.color_id = safely_get_json_value(state, "color.id", int)
        if safely_get_json_value(state, "color.i") is not None:
            self.color_intensity = safely_get_json_value(state, "color.i", int)
        if safely_get_json_value(state, "sound.enabled") is not None:
            self.sound_enabled = safely_get_json_value(state, "sound.enabled", bool)
        if safely_get_json_value(state, "sound.id") is not None:
            self.sound_id = safely_get_json_value(state, "sound.id", int)
        if safely_get_json_value(state, "sound.v") is not None:
            self.sound_volume = safely_get_json_value(state, "sound.v", int)
            if self.sound_volume > 0:
                self.last_nonzero_sound_volume = self.sound_volume
        self.publish_updates()

    @property
    def is_on(self) -> bool:
        return self.color_enabled

    def turn_on_routine(self, step: int = 1) -> None:
        self._update(
            {
                "content": {
                    "playing": "routine",
                    "paused": False,
                    "offset": 0,
                    "step": step,
                }
            }
        )

    def turn_off(self) -> None:
        self._update(
            {
                "content": {
                    "playing": "none",
                    "paused": False,
                    "offset": 0,
                    "step": 0,
                }
            }
        )

    def _apply_remote_state(self, color_enabled: bool, sound_enabled: bool) -> None:
        if color_enabled or sound_enabled:
            self._update(
                {
                    "content": {"playing": "remote", "paused": False, "offset": 0, "step": 0},
                    "color": {
                        "enabled": color_enabled,
                        "id": self.color_id,
                        "i": self.color_intensity,
                    },
                    "sound": {
                        "enabled": sound_enabled,
                        "id": self.sound_id,
                        "v": self.sound_volume,
                    },
                }
            )
            return

        self._update(
            {
                "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
                "color": {"enabled": False},
                "sound": {"enabled": False},
            }
        )

    def set_light_enabled(self, enabled: bool) -> None:
        self._apply_remote_state(color_enabled=enabled, sound_enabled=self.sound_enabled)

    def set_sound_enabled(self, enabled: bool) -> None:
        if enabled and self.sound_volume <= 0:
            # Device reports v=0 while disabled; restore last audible volume when enabling.
            self.sound_volume = self.last_nonzero_sound_volume
        self._apply_remote_state(color_enabled=self.color_enabled, sound_enabled=enabled)

    @property
    def light_brightness_percent(self) -> float:
        return round((self.color_intensity / 65535) * 100, 1)

    def set_light_brightness_percent(self, percent: float) -> None:
        percent = max(0.0, min(100.0, float(percent)))
        self.color_intensity = int(round((percent / 100.0) * 65535))
        if self.color_intensity <= 0:
            self.color_enabled = False
            self._apply_remote_state(
                color_enabled=False,
                sound_enabled=self.sound_enabled,
            )
            return

        self.color_enabled = True
        self._apply_remote_state(
            color_enabled=True,
            sound_enabled=self.sound_enabled,
        )

    def set_color_id(self, color_id: int) -> None:
        self.color_id = max(0, int(color_id))
        if self.color_enabled or self.sound_enabled:
            self._apply_remote_state(
                color_enabled=self.color_enabled,
                sound_enabled=self.sound_enabled,
            )
            return

        # Persist chosen color id while leaving playback off.
        self._update(
            {
                "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
                "color": {
                    "enabled": False,
                    "id": self.color_id,
                    "i": self.color_intensity,
                },
            }
        )

    def set_color_intensity_raw(self, raw_value: int) -> None:
        self.color_intensity = max(0, min(65535, int(raw_value)))
        if self.color_enabled or self.sound_enabled:
            self._apply_remote_state(
                color_enabled=self.color_enabled,
                sound_enabled=self.sound_enabled,
            )
            return

        self._update(
            {
                "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
                "color": {
                    "enabled": False,
                    "id": self.color_id,
                    "i": self.color_intensity,
                },
            }
        )

    @property
    def sound_volume_percent(self) -> float:
        return round((self.sound_volume / 65535) * 100, 1)

    def set_sound_volume_percent(self, percent: float) -> None:
        percent = max(0.0, min(100.0, float(percent)))
        raw_volume = int(round((percent / 100.0) * 65535))
        self.sound_volume = raw_volume
        if raw_volume > 0:
            self.last_nonzero_sound_volume = raw_volume

        if self.color_enabled or self.sound_enabled:
            self._apply_remote_state(
                color_enabled=self.color_enabled,
                sound_enabled=self.sound_enabled,
            )
            return

        # Keep content stopped while persisting preferred sound settings.
        self._update(
            {
                "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
                "sound": {
                    "enabled": False,
                    "id": self.sound_id,
                    "v": self.sound_volume,
                },
            }
        )

    def __repr__(self):
        return {
            "device_name": self.device_name,
            "thing_name": self.thing_name,
            "mac": self.mac,
            "firmware_version": self.firmware_version,
            "current_playing": self.current_playing,
            "is_on": self.is_on,
            "is_online": self.is_online,
            "color_enabled": self.color_enabled,
            "sound_enabled": self.sound_enabled,
        }
