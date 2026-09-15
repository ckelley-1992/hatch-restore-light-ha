"""Entity behaviour against a fake session."""

from __future__ import annotations

from homeassistant.const import STATE_OFF, STATE_ON, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hatch_restore_light.const import DOMAIN

from .conftest import INITIAL_REPORTED, THING

LIGHT = "light.bedroom_hatch_light"
FAN = "fan.bedroom_hatch_sound"
SWITCH = "switch.bedroom_hatch_sleep_mode"
BUTTON = "button.bedroom_hatch_next_routine_step"
PLAYING = "sensor.bedroom_hatch_playing"
STEP = "sensor.bedroom_hatch_routine_step"
CONNECTED = "binary_sensor.bedroom_hatch_connected"

ROUTINE_STEP1 = {
    "content": {"playing": "routine", "paused": False, "offset": 0, "step": 1},
    "color": {"enabled": True, "id": 229, "i": 39321},
    "sound": {"enabled": False, "id": 10040, "v": 0},
}
ROUTINE_STEP2 = {
    "content": {"playing": "routine", "paused": False, "offset": 0, "step": 2},
    "color": {"enabled": False, "id": 229, "i": 0},
    "sound": {"enabled": True, "id": 10040, "v": 29490},
}


async def _report(hass: HomeAssistant, session, reported: dict) -> None:
    session.report(reported)
    await hass.async_block_till_done()


async def test_entities_created_with_initial_state(hass: HomeAssistant, setup_integration) -> None:
    registry = er.async_get(hass)
    for entity_id, suffix in (
        (LIGHT, "light"),
        (FAN, "sound"),
        (SWITCH, "sleep_mode"),
        (BUTTON, "next_routine_step"),
        (PLAYING, "playing"),
        (STEP, "routine_step"),
        (CONNECTED, "connected"),
    ):
        entry = registry.async_get(entity_id)
        assert entry is not None, entity_id
        assert entry.unique_id == f"{THING}_{suffix}"

    assert hass.states.get(LIGHT).state == STATE_OFF
    assert hass.states.get(FAN).state == STATE_OFF
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(PLAYING).state == "none"
    assert hass.states.get(STEP).state == "0"
    assert hass.states.get(CONNECTED).state == STATE_ON
    # Dropped entities must not exist.
    assert hass.states.get("media_player.bedroom_hatch_sound_media") is None
    assert hass.states.get("light.bedroom_hatch_sound_level") is None
    assert hass.states.get("switch.bedroom_hatch_sound") is None
    # Color id number is disabled by default.
    number = registry.async_get("number.bedroom_hatch_color_id")
    assert number is not None and number.disabled


async def test_nightly_flow_is_observed(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await _report(hass, session, ROUTINE_STEP1)
    assert hass.states.get(SWITCH).state == STATE_ON
    assert hass.states.get(LIGHT).state == STATE_ON
    assert hass.states.get(LIGHT).attributes["brightness"] == 153
    assert hass.states.get(FAN).state == STATE_OFF
    assert hass.states.get(STEP).state == "1"

    # The physical tap: step 1 -> 2.
    await _report(hass, session, ROUTINE_STEP2)
    assert hass.states.get(STEP).state == "2"
    assert hass.states.get(LIGHT).state == STATE_OFF
    assert hass.states.get(FAN).state == STATE_ON
    assert hass.states.get(FAN).attributes["percentage"] == 45
    assert hass.states.get(SWITCH).state == STATE_ON

    # Sound finishes: everything off.
    await _report(hass, session, INITIAL_REPORTED)
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(FAN).state == STATE_OFF
    assert hass.states.get(PLAYING).state == "none"


async def test_ghost_levels_read_as_off(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await _report(hass, session, {"color": {"enabled": True, "i": 300}, "sound": {"enabled": True, "v": 10}})
    assert hass.states.get(LIGHT).state == STATE_OFF
    assert hass.states.get(FAN).state == STATE_OFF


async def test_sleep_mode_switch_sends_routine(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    assert session.shadow.published[-1] == {
        "content": {"playing": "routine", "paused": False, "offset": 0, "step": 1}
    }
    await hass.services.async_call("switch", "turn_off", {"entity_id": SWITCH}, blocking=True)
    assert session.shadow.published[-1] == {
        "content": {"playing": "none", "paused": False, "offset": 0, "step": 0}
    }


async def test_button_advances_routine_only_when_in_routine(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    await _report(hass, session, ROUTINE_STEP1)
    await hass.services.async_call("button", "press", {"entity_id": BUTTON}, blocking=True)
    assert session.shadow.published[-1] == {
        "content": {"playing": "routine", "paused": False, "offset": 0, "step": 2}
    }


async def test_light_write_inside_routine_keeps_routine(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await _report(hass, session, ROUTINE_STEP1)
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT, "brightness": 128}, blocking=True)
    payload = session.shadow.published[-1]
    assert "content" not in payload
    assert payload["color"]["enabled"] is True
    assert payload["color"]["i"] == round(128 / 255 * 65535)
    await hass.services.async_call("light", "turn_off", {"entity_id": LIGHT}, blocking=True)
    assert session.shadow.published[-1] == {"color": {"enabled": False}}


async def test_light_write_outside_routine_uses_remote_mode(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT}, blocking=True)
    payload = session.shadow.published[-1]
    assert payload["content"]["playing"] == "remote"
    assert payload["color"]["enabled"] is True
    assert payload["color"]["i"] == 32767  # last known audible level default
    assert payload["sound"]["enabled"] is False


async def test_fan_percentage_and_off(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await hass.services.async_call(
        "fan", "set_percentage", {"entity_id": FAN, "percentage": 40}, blocking=True
    )
    payload = session.shadow.published[-1]
    assert payload["content"]["playing"] == "remote"
    assert payload["sound"] == {"enabled": True, "id": 10040, "v": round(0.4 * 65535)}
    await _report(hass, session, {"content": {"playing": "remote"}, "sound": {"enabled": True, "v": 26214}})
    assert hass.states.get(FAN).state == STATE_ON
    assert hass.states.get(FAN).attributes["percentage"] == 40

    await hass.services.async_call("fan", "turn_off", {"entity_id": FAN}, blocking=True)
    assert session.shadow.published[-1] == {
        "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
        "color": {"enabled": False},
        "sound": {"enabled": False},
    }


async def test_fan_off_during_step2_stops_routine(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await _report(hass, session, ROUTINE_STEP2)
    await hass.services.async_call("fan", "turn_off", {"entity_id": FAN}, blocking=True)
    assert session.shadow.published[-1] == {
        "content": {"playing": "none", "paused": False, "offset": 0, "step": 0}
    }


async def test_health_loss_marks_unavailable_and_recovers(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    session.set_health(False, "MQTT interrupted")
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == STATE_UNAVAILABLE
    assert hass.states.get(SWITCH).state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTED).state == STATE_UNAVAILABLE

    session.set_health(True, "resumed")
    await hass.async_block_till_done()
    assert hass.states.get(LIGHT).state == STATE_OFF


async def test_device_offline_marks_unavailable_but_connected_sensor_reports(
    hass: HomeAssistant, setup_integration
) -> None:
    session = setup_integration
    await _report(hass, session, {"connected": False})
    assert hass.states.get(LIGHT).state == STATE_UNAVAILABLE
    assert hass.states.get(CONNECTED).state == STATE_OFF


async def test_removed_entities_are_purged_from_registry(
    hass: HomeAssistant, config_entry, fake_session_cls
) -> None:
    registry = er.async_get(hass)
    stale = [
        registry.async_get_or_create(
            "media_player", DOMAIN, f"{THING}_sound_media", config_entry=config_entry
        ),
        registry.async_get_or_create("light", DOMAIN, f"{THING}_sound_level", config_entry=config_entry),
        registry.async_get_or_create("switch", DOMAIN, f"{THING}_sound", config_entry=config_entry),
    ]
    keep = registry.async_get_or_create("switch", DOMAIN, f"{THING}_sleep_mode", config_entry=config_entry)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    for entry in stale:
        assert registry.async_get(entry.entity_id) is None
    assert registry.async_get(keep.entity_id) is not None


async def test_unload(hass: HomeAssistant, config_entry, setup_integration) -> None:
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    await hass.async_block_till_done()
    assert setup_integration.health_reason == "stopped"
    assert hass.states.get(LIGHT).state == STATE_UNAVAILABLE


async def test_auth_failure_at_setup_starts_reauth(hass: HomeAssistant, fake_session_cls) -> None:
    from custom_components.hatch_restore_light.hatch_cloud import HatchAuthError

    entry = MockConfigEntry(
        domain=DOMAIN, unique_id="x@example.com", data={"email": "x@example.com", "password": "bad"}
    )
    entry.add_to_hass(hass)
    original_init = fake_session_cls.__init__

    def failing_init(self, **kwargs):
        original_init(self, **kwargs)
        self.fail_start = HatchAuthError("nope")

    fake_session_cls.__init__ = failing_init
    try:
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    finally:
        fake_session_cls.__init__ = original_init
    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"].get("source") == "reauth" for f in flows)


ROUTINE_LINGERING_AFTER_SOUND = {
    # Observed on Gen 1: step returns to 0 when the routine finishes, playing stays "routine".
    "content": {"playing": "routine", "paused": False, "offset": 0, "step": 0},
    "color": {"enabled": False, "id": 229, "i": 0},
    "sound": {"enabled": False, "id": 10040, "v": 0},
}


async def test_sleep_mode_turns_off_when_routine_lingers_after_sound(
    hass: HomeAssistant, setup_integration
) -> None:
    session = setup_integration
    await _report(hass, session, ROUTINE_STEP2)
    assert hass.states.get(SWITCH).state == STATE_ON
    await _report(hass, session, ROUTINE_LINGERING_AFTER_SOUND)
    assert hass.states.get(SWITCH).state == STATE_OFF
    assert hass.states.get(STEP).state == "0"
    assert hass.states.get(PLAYING).state == "routine"  # raw mode is still reported truthfully
    assert hass.states.get(FAN).state == STATE_OFF


async def test_sleep_mode_stays_on_at_step1_with_light_off(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await _report(hass, session, ROUTINE_STEP1)
    # Light switched off by hand while the routine waits for the tap: still armed.
    await _report(hass, session, {"color": {"enabled": False, "i": 0}})
    assert hass.states.get(LIGHT).state == STATE_OFF
    assert hass.states.get(SWITCH).state == STATE_ON


async def test_manual_sound_on_resumes_last_audible_sound(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    # Pink noise (id 10514) played during step 2, then the device went idle reporting a default id.
    await _report(hass, session, {**ROUTINE_STEP2, "sound": {"enabled": True, "id": 10514, "v": 29490}})
    await _report(hass, session, {**INITIAL_REPORTED, "sound": {"enabled": False, "id": 10040, "v": 0}})
    assert hass.states.get(FAN).attributes["sound_id"] == 10040
    assert hass.states.get(FAN).attributes["last_active_sound_id"] == 10514

    await hass.services.async_call("fan", "turn_on", {"entity_id": FAN}, blocking=True)
    payload = session.shadow.published[-1]
    assert payload["sound"] == {"enabled": True, "id": 10514, "v": 29490}
    assert payload["content"]["playing"] == "remote"


async def test_manual_light_on_resumes_last_active_color(hass: HomeAssistant, setup_integration) -> None:
    session = setup_integration
    await _report(hass, session, {"color": {"enabled": True, "id": 231, "i": 40000}})
    await _report(hass, session, {"color": {"enabled": False, "id": 229, "i": 0}})
    await hass.services.async_call("light", "turn_on", {"entity_id": LIGHT}, blocking=True)
    assert session.shadow.published[-1]["color"] == {"enabled": True, "id": 231, "i": 40000}
