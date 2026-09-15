"""HatchCloudSession's live sound-catalog parsing (no network: the API client is mocked)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from custom_components.hatch_restore_light.hatch_cloud import HatchCloudSession, LegacyRestoreDevice
from custom_components.hatch_restore_light.hatch_cloud.sounds import RESTORE_SLEEP_SOUNDS

CATALOG = {
    "contentItems": [
        {
            "id": 10036,
            "title": "Pink Noise",
            "contentType": "sound",
            "hidden": False,
            "alarmOnly": False,
            "products": ["restoreIot", "restore"],
            "displayOrder": 70,
        },
        {
            "id": 10040,
            "title": "Light Rain",
            "contentType": "sound",
            "hidden": False,
            "alarmOnly": False,
            "products": ["restore"],
            "displayOrder": 58,
        },
        {
            "id": 10003,
            "title": "Pink Noise",
            "contentType": "sound",
            "hidden": True,
            "alarmOnly": False,
            "products": ["restore"],
            "displayOrder": 1,
        },
        {
            "id": 10024,
            "title": "Beep Beep",
            "contentType": "sound",
            "hidden": False,
            "alarmOnly": True,
            "products": ["restore"],
            "displayOrder": 87,
        },
        {
            "id": 10514,
            "title": "Pink Noise CGV5",
            "contentType": "sound",
            "hidden": False,
            "alarmOnly": False,
            "products": ["restBaby"],
            "displayOrder": 2,
        },
        {
            "id": 229,
            "title": "Warm White",
            "contentType": "color",
            "hidden": False,
            "alarmOnly": False,
            "products": ["restore"],
            "displayOrder": 3,
        },
    ]
}


def _session(loop, content_result=None, content_error=None) -> tuple[HatchCloudSession, LegacyRestoreDevice]:
    session = HatchCloudSession(
        loop=loop,
        client_session=MagicMock(),
        email="a@b.c",
        password="x",
        on_device_update=lambda thing: None,
        on_health_changed=lambda healthy, reason: None,
        on_auth_failed=lambda: None,
    )
    device = LegacyRestoreDevice("Bedroom Hatch", "thing123", "AA:BB:CC:DD:EE:FF")
    session.devices = [device]
    session._auth_token = "token"
    session._api = MagicMock()
    session._api.content = AsyncMock(return_value=content_result, side_effect=content_error)
    return session, device


async def test_live_catalog_filters_and_orders() -> None:
    session, device = _session(asyncio.get_running_loop(), CATALOG)
    await session._async_load_sound_catalog()
    assert session.sound_catalog == {10040: "Light Rain", 10036: "Pink Noise"}
    assert device.sound_catalog == {10040: "Light Rain", 10036: "Pink Noise"}


async def test_catalog_failure_keeps_builtin_table() -> None:
    session, device = _session(asyncio.get_running_loop(), content_error=TimeoutError())
    await session._async_load_sound_catalog()
    assert session.sound_catalog == {}
    assert device.sound_catalog == RESTORE_SLEEP_SOUNDS
