"""Legacy Restore (``product=restore``, Gen 1) device model.

Unlike the upstream ``ShadowClientSubscriberMixin`` this object is long-lived: it is constructed
once (no I/O) and attached to whichever MQTT connection is current, so learned state such as the
last audible volume survives reconnects and rebuilds. All shadow I/O has timeouts.
"""

from __future__ import annotations

import asyncio
from concurrent import futures
from datetime import UTC, datetime
import logging
import time
from typing import Any

from awscrt import mqtt
from awsiot import iotshadow
from awsiot.iotshadow import (
    ErrorResponse,
    GetShadowResponse,
    IotShadowClient,
    ShadowState,
    UpdateShadowRequest,
    UpdateShadowResponse,
)
from hatch_rest_api.callbacks import CallbacksMixin
from hatch_rest_api.util import safely_get_json_value

from .const import (
    ACTIVE_FLOOR_RAW,
    DEFAULT_COLOR_ID,
    DEFAULT_SOUND_ID,
    OP_TIMEOUT_S,
    RAW_MAX,
    ROUTINE_PRESERVING_WRITES,
    ROUTINE_STEP_TWO_PHASE,
    ROUTINE_STEP_TWO_PHASE_DELAY_S,
)
from .errors import HatchCloudError, HatchNotReadyError, NotInRoutineError
from .sounds import RESTORE_SLEEP_SOUNDS, sound_title

_LOGGER = logging.getLogger(__name__)

PLAYING_NONE = "none"
PLAYING_REMOTE = "remote"
PLAYING_ROUTINE = "routine"
PLAYING_STATES = (PLAYING_NONE, PLAYING_REMOTE, PLAYING_ROUTINE)


def _content(playing: str, step: int) -> dict[str, Any]:
    return {"playing": playing, "paused": False, "offset": 0, "step": step}


def _pct_to_raw(percent: float) -> int:
    percent = max(0.0, min(100.0, float(percent)))
    return int(round(percent / 100.0 * RAW_MAX))


def _raw_to_pct(raw: int) -> float:
    return round(raw / RAW_MAX * 100, 1)


class LegacyRestoreDevice(CallbacksMixin):
    """Light + sound + routine control over the flat Gen 1 shadow document."""

    routine_preserving_writes: bool = ROUTINE_PRESERVING_WRITES
    routine_step_two_phase: bool = ROUTINE_STEP_TWO_PHASE

    def __init__(self, device_name: str, thing_name: str, mac: str) -> None:
        self.device_name = device_name
        self.thing_name = thing_name
        self.mac = mac
        self._shadow_client: IotShadowClient | None = None
        self._subscribed_topics: list[str] = []
        self.document_version: int = -1
        self.last_reported_at: datetime | None = None

        self.firmware_version: str | None = None
        self.is_online: bool = False
        self.current_playing: str = PLAYING_NONE
        self.routine_step: int = 0
        self.color_enabled: bool = False
        self.color_id: int = DEFAULT_COLOR_ID
        self.color_intensity: int = RAW_MAX // 2
        self.last_nonzero_color_intensity: int = RAW_MAX // 2
        # Ids seen while the light/sound were actually active. The device reports defaults while
        # off, so these are what a manual "turn on" should resume with.
        self.last_active_color_id: int = DEFAULT_COLOR_ID
        self.sound_enabled: bool = False
        self.sound_id: int = DEFAULT_SOUND_ID
        self.sound_volume: int = RAW_MAX // 2
        self.last_nonzero_sound_volume: int = RAW_MAX // 2
        self.last_active_sound_id: int = DEFAULT_SOUND_ID
        # id -> title of selectable sleep sounds; replaced by the live catalog when it arrives.
        self.sound_catalog: dict[int, str] = dict(RESTORE_SLEEP_SOUNDS)
        self._setup_callbacks()

    # ---------------------------------------------------------------- shadow plumbing

    @property
    def is_attached(self) -> bool:
        return self._shadow_client is not None

    async def async_attach(self, shadow_client: IotShadowClient, timeout: float = OP_TIMEOUT_S) -> None:
        """Subscribe to this thing's shadow topics on ``shadow_client`` and request its state.

        Safe to call again on the same client (e.g. after a reconnect); re-subscribing simply
        rebinds the callbacks.
        """
        self._shadow_client = shadow_client
        self.document_version = -1
        subscriptions = (
            (
                shadow_client.subscribe_to_update_shadow_accepted,
                iotshadow.UpdateShadowSubscriptionRequest(thing_name=self.thing_name),
                self._on_update_shadow_accepted,
            ),
            (
                shadow_client.subscribe_to_get_shadow_accepted,
                iotshadow.GetShadowSubscriptionRequest(thing_name=self.thing_name),
                self._on_get_shadow_accepted,
            ),
            (
                shadow_client.subscribe_to_update_shadow_rejected,
                iotshadow.UpdateShadowSubscriptionRequest(thing_name=self.thing_name),
                self._on_shadow_rejected,
            ),
            (
                shadow_client.subscribe_to_get_shadow_rejected,
                iotshadow.GetShadowSubscriptionRequest(thing_name=self.thing_name),
                self._on_shadow_rejected,
            ),
        )
        topics: list[str] = []
        for subscribe, request, callback in subscriptions:
            future, topic = subscribe(request=request, qos=mqtt.QoS.AT_LEAST_ONCE, callback=callback)
            await asyncio.wait_for(asyncio.wrap_future(future), timeout)
            topics.append(topic)
        self._subscribed_topics = topics
        _LOGGER.debug("%s subscribed to %s", self.device_name, topics)
        await self.async_refresh(timeout)

    def detach(self) -> None:
        self._shadow_client = None
        self._subscribed_topics = []

    async def async_refresh(self, timeout: float = OP_TIMEOUT_S) -> None:
        """Ask for the full shadow; the answer arrives via ``_on_get_shadow_accepted``."""
        client = self._require_client()
        future = client.publish_get_shadow(
            request=iotshadow.GetShadowRequest(thing_name=self.thing_name, client_token=None),
            qos=mqtt.QoS.AT_LEAST_ONCE,
        )
        await asyncio.wait_for(asyncio.wrap_future(future), timeout)

    def _require_client(self) -> IotShadowClient:
        if self._shadow_client is None:
            raise HatchNotReadyError(f"{self.device_name}: Hatch cloud connection is not ready")
        return self._shadow_client

    def _update(self, desired: dict[str, Any], timeout: float = OP_TIMEOUT_S) -> None:
        """Publish a desired-state update. Blocking; call from a worker thread."""
        client = self._require_client()
        _LOGGER.debug("%s desired update: %s", self.device_name, desired)
        request = UpdateShadowRequest(thing_name=self.thing_name, state=ShadowState(desired=desired))
        try:
            client.publish_update_shadow(request, mqtt.QoS.AT_LEAST_ONCE).result(timeout=timeout)
        except futures.TimeoutError as err:
            raise HatchNotReadyError(f"{self.device_name}: timed out sending command to Hatch cloud") from err
        except Exception as err:  # noqa: BLE001 - awscrt raises AwsCrtError subclasses
            name = getattr(err, "name", "") or ""
            if "CANCELLED" in name or "OFFLINE" in name or "DISCONNECT" in name:
                raise HatchNotReadyError(
                    f"{self.device_name}: Hatch cloud connection is down ({name})"
                ) from err
            raise HatchCloudError(f"{self.device_name}: command failed: {err}") from err

    # ---------------------------------------------------------------- shadow callbacks (CRT thread)

    def _on_update_shadow_accepted(self, response: UpdateShadowResponse) -> None:
        if response.version is not None and response.version < self.document_version:
            _LOGGER.debug(
                "%s ignoring stale update v%s < v%s",
                self.device_name,
                response.version,
                self.document_version,
            )
            return
        if response.state and response.state.reported:
            if response.version is not None:
                self.document_version = response.version
            self._update_local_state(response.state.reported)

    def _on_get_shadow_accepted(self, response: GetShadowResponse) -> None:
        if response.version is not None and response.version < self.document_version:
            return
        if response.state and response.state.reported:
            if response.version is not None:
                self.document_version = response.version
            self._update_local_state(response.state.reported)

    def _on_shadow_rejected(self, response: ErrorResponse) -> None:
        _LOGGER.warning(
            "%s shadow request rejected: code=%s message=%s",
            self.device_name,
            response.code,
            response.message,
        )

    def _update_local_state(self, state: dict[str, Any]) -> None:
        get = safely_get_json_value
        if get(state, "deviceInfo.f") is not None:
            self.firmware_version = get(state, "deviceInfo.f")
        if get(state, "connected") is not None:
            self.is_online = get(state, "connected", bool)
        if get(state, "content.playing") is not None:
            self.current_playing = str(get(state, "content.playing"))
        if get(state, "content.step") is not None:
            self.routine_step = get(state, "content.step", int)
        if get(state, "color.enabled") is not None:
            self.color_enabled = get(state, "color.enabled", bool)
        if get(state, "color.id") is not None:
            self.color_id = get(state, "color.id", int)
        if get(state, "color.i") is not None:
            self.color_intensity = get(state, "color.i", int)
            if self.color_intensity > ACTIVE_FLOOR_RAW:
                self.last_nonzero_color_intensity = self.color_intensity
        if get(state, "sound.enabled") is not None:
            self.sound_enabled = get(state, "sound.enabled", bool)
        if get(state, "sound.id") is not None:
            self.sound_id = get(state, "sound.id", int)
        if get(state, "sound.v") is not None:
            self.sound_volume = get(state, "sound.v", int)
            if self.sound_volume > ACTIVE_FLOOR_RAW:
                self.last_nonzero_sound_volume = self.sound_volume
        if self.is_light_active:
            self.last_active_color_id = self.color_id
        if self.is_sound_active:
            self.last_active_sound_id = self.sound_id
        self.last_reported_at = datetime.now(UTC)
        _LOGGER.debug("%s state: %s", self.device_name, self.as_dict())
        self.publish_updates()

    # ---------------------------------------------------------------- derived state

    @property
    def is_light_active(self) -> bool:
        return bool(self.color_enabled and self.color_intensity > ACTIVE_FLOOR_RAW)

    @property
    def is_sound_active(self) -> bool:
        return bool(self.sound_enabled and self.sound_volume > ACTIVE_FLOOR_RAW)

    @property
    def is_on(self) -> bool:
        return self.is_light_active or self.is_sound_active

    @property
    def is_in_routine(self) -> bool:
        return self.current_playing == PLAYING_ROUTINE

    @property
    def is_sleep_mode(self) -> bool:
        """True while a routine step is in progress.

        Observed on Gen 1: ``content.step`` goes 0 -> 1 -> 2 -> 0 over a night while
        ``content.playing`` stays ``"routine"`` after the last step ends, so the step - not the
        mode - is what says the routine is still doing something.
        """
        return self.is_in_routine and self.routine_step >= 1

    @property
    def selected_sound_id(self) -> int:
        """The sound that is playing, or the one the next "on" will play."""
        return self.sound_id if self.is_sound_active else self.last_active_sound_id

    @property
    def selected_sound_title(self) -> str | None:
        return sound_title(self.selected_sound_id, self.sound_catalog)

    def set_sound_catalog(self, catalog: dict[int, str]) -> None:
        if catalog and catalog != self.sound_catalog:
            self.sound_catalog = dict(catalog)
            self.publish_updates()

    @property
    def light_brightness_percent(self) -> float:
        return _raw_to_pct(self.color_intensity)

    @property
    def sound_volume_percent(self) -> float:
        return _raw_to_pct(self.sound_volume)

    # ---------------------------------------------------------------- commands (worker thread)

    def _color_payload(self, enabled: bool) -> dict[str, Any]:
        return {"enabled": enabled, "id": self.color_id, "i": self.color_intensity}

    def _sound_payload(self, enabled: bool) -> dict[str, Any]:
        return {"enabled": enabled, "id": self.sound_id, "v": self.sound_volume}

    def _write(self, *, color: dict[str, Any] | None = None, sound: dict[str, Any] | None = None) -> None:
        """Apply a light and/or sound change with the right ``content`` mode.

        Inside a routine (and with routine-preserving writes on) only the touched sub-objects are
        sent so the routine keeps running. Otherwise the device is put in ``remote`` mode with the
        full colour + sound state, or fully stopped when both end up off.
        """
        if self.is_in_routine and self.routine_preserving_writes:
            payload = {k: v for k, v in (("color", color), ("sound", sound)) if v is not None}
            if payload:
                self._update(payload)
            return

        color_enabled = color["enabled"] if color is not None else self.color_enabled
        sound_enabled = sound["enabled"] if sound is not None else self.sound_enabled
        if color_enabled or sound_enabled:
            self._update(
                {
                    "content": _content(PLAYING_REMOTE, 0),
                    "color": color if color is not None else self._color_payload(self.color_enabled),
                    "sound": sound if sound is not None else self._sound_payload(self.sound_enabled),
                }
            )
            return
        self._update(
            {
                "content": _content(PLAYING_NONE, 0),
                "color": {"enabled": False},
                "sound": {"enabled": False},
            }
        )

    def _write_preference(
        self, *, color: dict[str, Any] | None = None, sound: dict[str, Any] | None = None
    ) -> None:
        """Persist colour/sound settings while leaving playback stopped."""
        payload: dict[str, Any] = {"content": _content(PLAYING_NONE, 0)}
        if color is not None:
            payload["color"] = color
        if sound is not None:
            payload["sound"] = sound
        self._update(payload)

    def set_light(self, enabled: bool, brightness_pct: float | None = None) -> None:
        """Light on/off, optionally at a brightness. Local state changes only when the device reports."""
        if not enabled:
            self._write(color={"enabled": False})
            return
        if brightness_pct is not None:
            raw = _pct_to_raw(brightness_pct)
            if raw <= ACTIVE_FLOOR_RAW:
                self.set_light(False)
                return
        elif self.color_intensity > ACTIVE_FLOOR_RAW:
            raw = self.color_intensity
        else:
            raw = self.last_nonzero_color_intensity
        color_id = self.color_id if self.is_light_active else self.last_active_color_id
        self._write(color={"enabled": True, "id": color_id, "i": raw})

    def set_sound(self, enabled: bool, volume_pct: float | None = None) -> None:
        """Sound on/off, optionally at a volume. Local state changes only when the device reports."""
        if not enabled:
            if self.is_in_routine and self.routine_preserving_writes and not self.is_light_active:
                # The sound was all the routine had left; stopping it means ending the routine.
                self.stop()
                return
            self._write(sound={"enabled": False})
            return
        if volume_pct is not None:
            raw = _pct_to_raw(volume_pct)
            if raw <= ACTIVE_FLOOR_RAW:
                self.set_sound(False)
                return
        elif self.sound_volume > ACTIVE_FLOOR_RAW:
            raw = self.sound_volume
        else:
            # Device reports v=0 while disabled; restore the last audible volume when enabling.
            raw = self.last_nonzero_sound_volume
        sound_id = self.sound_id if self.is_sound_active else self.last_active_sound_id
        self._write(sound={"enabled": True, "id": sound_id, "v": raw})

    def select_sound(self, sound_id: int) -> None:
        """Choose a sound: switch immediately if playing, otherwise remember it for the next "on"."""
        sound_id = int(sound_id)
        self.last_active_sound_id = sound_id
        if self.is_sound_active:
            self._write(sound={"enabled": True, "id": sound_id, "v": self.sound_volume})
        else:
            self.publish_updates()

    def set_color_id(self, color_id: int) -> None:
        color_id = max(0, int(color_id))
        self.last_active_color_id = color_id
        if self.is_light_active:
            self._write(color={"enabled": True, "id": color_id, "i": self.color_intensity})
        elif self.is_sound_active:
            # Keep remote mode intact; just persist the colour choice alongside it.
            self._write(color={"enabled": False, "id": color_id, "i": self.color_intensity})
        else:
            self._write_preference(color={"enabled": False, "id": color_id, "i": self.color_intensity})

    def start_routine(self, step: int = 1) -> None:
        self._update({"content": _content(PLAYING_ROUTINE, max(1, int(step)))})

    def set_routine_step(self, step: int) -> None:
        step = max(1, int(step))
        if self.is_in_routine and self.routine_step_two_phase:
            self._update({"content": _content(PLAYING_NONE, 0)})
            time.sleep(ROUTINE_STEP_TWO_PHASE_DELAY_S)
        self._update({"content": _content(PLAYING_ROUTINE, step)})

    def advance_routine(self) -> None:
        if not self.is_in_routine:
            raise NotInRoutineError(f"{self.device_name}: no routine is playing")
        self.set_routine_step(self.routine_step + 1)

    def stop(self) -> None:
        self._update({"content": _content(PLAYING_NONE, 0)})

    def set_sleep_mode(self, enabled: bool) -> None:
        if enabled:
            self.start_routine(1)
        else:
            self.stop()

    # ---------------------------------------------------------------- introspection

    def as_dict(self) -> dict[str, Any]:
        return {
            "device_name": self.device_name,
            "thing_name": self.thing_name,
            "mac": self.mac,
            "firmware_version": self.firmware_version,
            "is_online": self.is_online,
            "current_playing": self.current_playing,
            "routine_step": self.routine_step,
            "is_light_active": self.is_light_active,
            "is_sound_active": self.is_sound_active,
            "is_sleep_mode": self.is_sleep_mode,
            "color_enabled": self.color_enabled,
            "color_id": self.color_id,
            "color_intensity": self.color_intensity,
            "sound_enabled": self.sound_enabled,
            "sound_id": self.sound_id,
            "sound_volume": self.sound_volume,
            "last_nonzero_sound_volume": self.last_nonzero_sound_volume,
            "last_active_color_id": self.last_active_color_id,
            "last_active_sound_id": self.last_active_sound_id,
            "selected_sound_title": self.selected_sound_title,
            "document_version": self.document_version,
            "last_reported_at": self.last_reported_at.isoformat() if self.last_reported_at else None,
        }

    def __repr__(self) -> str:
        return f"LegacyRestoreDevice({self.as_dict()!r})"
