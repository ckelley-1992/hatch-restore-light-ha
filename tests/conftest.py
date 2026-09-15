"""Shared fixtures: a fake HatchCloudSession so no cloud or MQTT is needed."""

from __future__ import annotations

from concurrent.futures import Future
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.hatch_restore_light.const import DOMAIN
from custom_components.hatch_restore_light.hatch_cloud import LegacyRestoreDevice

THING = "thing123"
MAC = "AA:BB:CC:DD:EE:FF"
DEVICE_NAME = "Bedroom Hatch"

INITIAL_REPORTED = {
    "deviceInfo": {"f": "1.2.3"},
    "connected": True,
    "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
    "color": {"enabled": False, "id": 229, "i": 0},
    "sound": {"enabled": False, "id": 10040, "v": 0},
}


@pytest.fixture(autouse=True, scope="session")
def _prime_pycares_thread():
    """pycares>=5 starts one process-wide daemon thread on first DNS use.

    Start it before the HA test harness snapshots threads, or the first test that creates an
    aiohttp session is flagged for a "lingering thread" it did not own.
    """
    try:
        import pycares

        pycares._shutdown_manager.start()
    except (ImportError, AttributeError):  # pragma: no cover - older/newer pycares
        pass
    yield


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


class FakeShadowClient:
    """Records desired-state publishes instead of talking to MQTT."""

    def __init__(self) -> None:
        self.published: list[dict[str, Any]] = []

    def publish_update_shadow(self, request, qos):
        self.published.append(request.state.desired)
        future: Future = Future()
        future.set_result(None)
        return future

    def publish_get_shadow(self, request, qos):
        future: Future = Future()
        future.set_result(None)
        return future


class FakeSession:
    """Stands in for HatchCloudSession: one legacy device, in-memory shadow."""

    instances: list[FakeSession] = []

    def __init__(self, *, loop, on_device_update, on_health_changed, on_auth_failed, **kwargs) -> None:
        self.loop = loop
        self._on_device_update = on_device_update
        self._on_health_changed = on_health_changed
        self._on_auth_failed = on_auth_failed
        self.device = LegacyRestoreDevice(device_name=DEVICE_NAME, thing_name=THING, mac=MAC)
        self.shadow = FakeShadowClient()
        self.device._shadow_client = self.shadow
        self.devices = [self.device]
        self.device_infos = [
            {"name": DEVICE_NAME, "product": "restore", "thingName": THING, "macAddress": MAC}
        ]
        self.healthy = False
        self.health_reason = "not started"
        self.credentials_expiry = None
        self.credential_refreshes = 0
        self.resyncs = 0
        self.fail_start: Exception | None = None
        FakeSession.instances.append(self)

    async def async_start(self) -> None:
        if self.fail_start is not None:
            raise self.fail_start
        self.device.register_callback(lambda: self.loop.call_soon_threadsafe(self._on_device_update, THING))
        self.report(INITIAL_REPORTED)
        self.healthy = True
        self.health_reason = "connected"

    async def async_stop(self) -> None:
        self.healthy = False
        self.health_reason = "stopped"

    async def async_resync(self) -> None:
        self.resyncs += 1

    def snapshots(self) -> dict[str, dict]:
        return {d.thing_name: d.as_dict() for d in self.devices}

    def device_by_thing_name(self, thing_name: str):
        return next((d for d in self.devices if d.thing_name == thing_name), None)

    # test helpers
    def report(self, reported: dict[str, Any]) -> None:
        """Simulate an update/accepted with reported state from the device."""
        self.device._update_local_state(reported)

    def set_health(self, healthy: bool, reason: str = "test") -> None:
        self.healthy = healthy
        self.health_reason = reason
        self._on_health_changed(healthy, reason)


@pytest.fixture
def fake_session_cls():
    FakeSession.instances.clear()
    with patch("custom_components.hatch_restore_light.coordinator.HatchCloudSession", FakeSession):
        yield FakeSession


@pytest.fixture
def config_entry(hass):
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="user@example.com",
        unique_id="user@example.com",
        data={"email": "user@example.com", "password": "secret"},
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
async def setup_integration(hass, config_entry, fake_session_cls):
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    session = fake_session_cls.instances[-1]
    return session
