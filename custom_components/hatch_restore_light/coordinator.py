"""Data coordinator for Hatch Restore Light."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from re import IGNORECASE, sub
from urllib.parse import urlencode
from uuid import uuid4

from awscrt import io
from awscrt.auth import AwsCredentialsProvider
from awsiot.iotshadow import IotShadowClient
from awsiot.mqtt_connection_builder import websockets_with_default_aws_signing
from hatch_rest_api import AwsHttp, Hatch, RestoreIot, RestoreV5
from hatch_rest_api.errors import RateError
from hatch_rest_api.restore_v4 import RestoreV4
from hatch_rest_api import hatch as hatch_module
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN, PREFERRED_API_BASE
from .legacy_restore_device import LegacyRestoreDevice

_LOGGER = logging.getLogger(__name__)


class HatchRestoreDataUpdateCoordinator(DataUpdateCoordinator[list[dict]]):
    """Coordinate Hatch API state + MQTT callback updates."""

    mqtt_connection = None
    rest_devices: list[object] = []
    expiration_time: float | None = None

    def __init__(self, hass: HomeAssistant, email: str, password: str) -> None:
        self.email = email
        self.password = password
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}-{email}",
            always_update=False,
        )

    def _disconnect_mqtt(self) -> None:
        if self.mqtt_connection is None:
            return
        try:
            self.mqtt_connection.disconnect().result()
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Failed to disconnect mqtt connection cleanly: %s", err)

    def _unregister_device_callbacks(self) -> None:
        for rest_device in self.rest_devices:
            rest_device.remove_callback(self.async_update_listeners)

    async def _retry_rate_limited(self, coro_factory, attempts: int = 5):
        wait_s = 2
        for attempt in range(1, attempts + 1):
            try:
                return await coro_factory()
            except RateError:
                if attempt >= attempts:
                    raise
                _LOGGER.debug("Hatch API rate limited, retrying in %ss", wait_s)
                await asyncio.sleep(wait_s)
                wait_s *= 2

    async def _fetch_iot_devices_homebridge_style(self, api: Hatch, auth_token: str) -> list[dict]:
        member = await self._retry_rate_limited(lambda: api.member(auth_token=auth_token))
        member_products = member.get("products", []) if isinstance(member, dict) else []
        products = list(
            dict.fromkeys(
                [
                    "restPlus",
                    "riot",
                    "riotPlus",
                    "restMini",
                    "restore",
                    "restoreIot",
                    "restoreV4",
                    "restoreV5",
                    "restBaby",
                    *member_products,
                ]
            )
        )
        query = urlencode([("iotProducts", product) for product in products])
        url = f"{PREFERRED_API_BASE}service/app/iotDevice/v2/fetch?{query}"
        headers = {"X-HatchBaby-Auth": auth_token, "USER_AGENT": "hatch_restore_light"}
        response = await self._retry_rate_limited(
            lambda: api.api_session.get(url=url, headers=headers)
        )
        response_json = await response.json()
        payload = response_json.get("payload", [])
        if isinstance(payload, list):
            return payload
        return []

    async def _bootstrap_devices(
        self,
        api: Hatch,
        auth_token: str,
        iot_devices: list[dict],
    ) -> tuple[object, list[object], float]:
        loop = asyncio.get_running_loop()
        aws_token = await self._retry_rate_limited(lambda: api.token(auth_token=auth_token))
        aws_http = AwsHttp(api.api_session)
        aws_credentials = await self._retry_rate_limited(
            lambda: aws_http.aws_credentials(
                region=aws_token["region"],
                identityId=aws_token["identityId"],
                aws_token=aws_token["token"],
            )
        )
        credentials_provider = AwsCredentialsProvider.new_static(
            aws_credentials["Credentials"]["AccessKeyId"],
            aws_credentials["Credentials"]["SecretKey"],
            session_token=aws_credentials["Credentials"]["SessionToken"],
        )
        event_loop_group = io.EventLoopGroup(1)
        host_resolver = io.DefaultHostResolver(event_loop_group)
        client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)
        endpoint = aws_token["endpoint"].lstrip("https://")
        safe_email = sub("[^a-z]", "", self.email, flags=IGNORECASE).lower()
        mqtt_connection = await loop.run_in_executor(
            None,
            partial(
                websockets_with_default_aws_signing,
                region=aws_token["region"],
                credentials_provider=credentials_provider,
                keep_alive_secs=30,
                client_bootstrap=client_bootstrap,
                endpoint=endpoint,
                client_id=f"hatch_restore_light/{safe_email}/{str(uuid4())}",
                on_connection_interrupted=lambda *_: _LOGGER.debug("Hatch mqtt interrupted"),
                on_connection_resumed=lambda *_: _LOGGER.debug("Hatch mqtt resumed"),
            ),
        )
        connect_future = await loop.run_in_executor(None, mqtt_connection.connect)
        await loop.run_in_executor(None, connect_future.result)
        shadow_client = IotShadowClient(mqtt_connection)

        devices: list[object] = []
        for iot_device in iot_devices:
            product = iot_device.get("product")
            device_name = iot_device.get("name")
            thing_name = iot_device.get("thingName")
            mac = iot_device.get("macAddress")
            if not product or not thing_name or not mac or not device_name:
                continue

            if product == "restore":
                devices.append(
                    LegacyRestoreDevice(
                        device_name=device_name,
                        thing_name=thing_name,
                        mac=mac,
                        shadow_client=shadow_client,
                    )
                )
            elif product == "restoreIot":
                devices.append(
                    RestoreIot(
                        device_name=device_name,
                        thing_name=thing_name,
                        mac=mac,
                        shadow_client=shadow_client,
                    )
                )
            elif product == "restoreV4":
                devices.append(
                    RestoreV4(
                        device_name=device_name,
                        thing_name=thing_name,
                        mac=mac,
                        shadow_client=shadow_client,
                    )
                )
            elif product == "restoreV5":
                devices.append(
                    RestoreV5(
                        device_name=device_name,
                        thing_name=thing_name,
                        mac=mac,
                        shadow_client=shadow_client,
                    )
                )

        return mqtt_connection, devices, aws_credentials["Credentials"]["Expiration"]

    async def _async_update_data(self) -> list[dict]:
        try:
            self._disconnect_mqtt()
            self._unregister_device_callbacks()
            hatch_module.API_URL = PREFERRED_API_BASE
            client_session = async_get_clientsession(self.hass)
            api = Hatch(client_session=client_session)
            auth_token = await self._retry_rate_limited(
                lambda: api.login(email=self.email, password=self.password)
            )
            iot_devices = await self._fetch_iot_devices_homebridge_style(api, auth_token)
            if not iot_devices:
                raise UpdateFailed("No Hatch iot devices found for this account")

            self.mqtt_connection, self.rest_devices, self.expiration_time = await self._bootstrap_devices(
                api,
                auth_token,
                iot_devices,
            )
            expires_at = datetime.fromtimestamp(self.expiration_time, UTC)
            self.update_interval = expires_at - datetime.now(UTC) - timedelta(minutes=1)

            for rest_device in self.rest_devices:
                rest_device.register_callback(self.async_update_listeners)

            return [rest_device.__repr__() for rest_device in self.rest_devices]
        except Exception as err:  # noqa: BLE001
            self.update_interval = timedelta(minutes=1)
            raise UpdateFailed(err) from err

    async def async_shutdown(self) -> None:
        """Shutdown coordinator resources."""
        self._disconnect_mqtt()
        self._unregister_device_callbacks()
        await super().async_shutdown()

    def rest_device_by_thing_name(self, thing_name: str):
        return next(
            (rest_device for rest_device in self.rest_devices if rest_device.thing_name == thing_name),
            None,
        )
