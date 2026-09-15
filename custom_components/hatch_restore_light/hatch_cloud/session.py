"""One long-lived connection to Hatch's cloud for a single account.

Lifecycle::

    login -> discover devices -> Cognito creds -> MQTT (SigV4 websocket) -> attach devices
        |                                              |
        +-- credential refresh loop (before expiry) ---+-- on interrupt: grace -> unavailable,
                                                          watchdog -> make-before-break rebuild
                                                       +-- on resume: re-subscribe + shadow get

Credentials are handed to awscrt through a *delegate* provider, so every reconnect handshake is
signed with whatever credentials are cached at that moment rather than the ones present at
construction time. All awscrt futures are awaited with timeouts; CRT-thread callbacks do nothing
but hop onto the asyncio loop.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import partial
import logging
from re import IGNORECASE, sub
import threading
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp import ClientError, ClientSession
from awscrt import io, mqtt
from awscrt.auth import AwsCredentials, AwsCredentialsProvider
from awsiot.iotshadow import IotShadowClient
from awsiot.mqtt_connection_builder import websockets_with_default_aws_signing
from hatch_rest_api import AwsHttp, Hatch, RestoreIot, RestoreV5
from hatch_rest_api import hatch as hatch_module
from hatch_rest_api.errors import AuthError, RateError
from hatch_rest_api.restore_v4 import RestoreV4
from hatch_rest_api.shadow_client_subscriber import ShadowClientSubscriberMixin

from .const import (
    CONNECT_TIMEOUT_S,
    CREDENTIAL_REFRESH_MARGIN,
    KNOWN_IOT_PRODUCTS,
    MQTT_KEEP_ALIVE_S,
    MQTT_PING_TIMEOUT_MS,
    MQTT_RECONNECT_MAX_S,
    MQTT_RECONNECT_MIN_S,
    OP_TIMEOUT_S,
    PREFERRED_API_BASE,
    REBUILD_BACKOFF_MAX_S,
    REBUILD_BACKOFF_MIN_S,
    UNAVAILABLE_GRACE_S,
    WATCHDOG_REBUILD_S,
)
from .errors import HatchAuthError, HatchCloudError
from .legacy_restore_device import LegacyRestoreDevice

_LOGGER = logging.getLogger(__name__)

LIBRARY_MODELS: dict[str, type] = {
    "restoreIot": RestoreIot,
    "restoreV4": RestoreV4,
    "restoreV5": RestoreV5,
}

DeviceUpdateCallback = Callable[[str], None]
HealthCallback = Callable[[bool, str], None]
AuthFailedCallback = Callable[[], None]


@dataclass
class ConnectionParams:
    """Everything a connection factory needs to build an ``awscrt.mqtt.Connection``."""

    client_id: str
    client_bootstrap: io.ClientBootstrap
    on_connection_interrupted: Callable[..., None]
    on_connection_resumed: Callable[..., None]
    on_connection_success: Callable[..., None]
    on_connection_failure: Callable[..., None]
    credentials_provider: AwsCredentialsProvider | None = None
    region: str | None = None
    endpoint: str | None = None


ConnectionFactory = Callable[[ConnectionParams], mqtt.Connection]


def default_connection_factory(params: ConnectionParams) -> mqtt.Connection:
    """SigV4-signed MQTT-over-WebSocket connection to AWS IoT (blocking; run in an executor)."""
    return websockets_with_default_aws_signing(
        region=params.region,
        credentials_provider=params.credentials_provider,
        client_bootstrap=params.client_bootstrap,
        endpoint=params.endpoint,
        client_id=params.client_id,
        clean_session=True,
        keep_alive_secs=MQTT_KEEP_ALIVE_S,
        ping_timeout_ms=MQTT_PING_TIMEOUT_MS,
        protocol_operation_timeout_ms=OP_TIMEOUT_S * 1000,
        reconnect_min_timeout_secs=MQTT_RECONNECT_MIN_S,
        reconnect_max_timeout_secs=MQTT_RECONNECT_MAX_S,
        on_connection_interrupted=params.on_connection_interrupted,
        on_connection_resumed=params.on_connection_resumed,
        on_connection_success=params.on_connection_success,
        on_connection_failure=params.on_connection_failure,
    )


class HatchCloudSession:
    """Owns the REST client, the credential cache, the MQTT connection and the device objects."""

    def __init__(
        self,
        *,
        loop: asyncio.AbstractEventLoop,
        client_session: ClientSession,
        email: str,
        password: str,
        on_device_update: DeviceUpdateCallback,
        on_health_changed: HealthCallback,
        on_auth_failed: AuthFailedCallback,
        connection_factory: ConnectionFactory | None = None,
        device_infos: list[dict[str, Any]] | None = None,
        credential_refresh_margin: timedelta = CREDENTIAL_REFRESH_MARGIN,
        client_id_prefix: str = "hatch_restore_light",
        unavailable_grace_s: float = UNAVAILABLE_GRACE_S,
        watchdog_rebuild_s: float = WATCHDOG_REBUILD_S,
    ) -> None:
        self._loop = loop
        self._email = email
        self._password = password
        self._on_device_update = on_device_update
        self._on_health_changed = on_health_changed
        self._on_auth_failed = on_auth_failed
        self._connection_factory = connection_factory or default_connection_factory
        self._uses_cloud = connection_factory is None
        self._credential_refresh_margin = credential_refresh_margin
        self._client_id_prefix = client_id_prefix
        self._unavailable_grace_s = unavailable_grace_s
        self._watchdog_rebuild_s = watchdog_rebuild_s

        hatch_module.API_URL = PREFERRED_API_BASE
        self._api = Hatch(client_session=client_session)
        self._aws_http = AwsHttp(client_session)
        self._auth_token: str | None = None
        self._aws_token: dict[str, Any] = {}
        self._creds_lock = threading.Lock()
        self._creds: AwsCredentials | None = None
        self.credentials_expiry: datetime | None = None
        self.credential_refreshes: int = 0

        self.device_infos: list[dict[str, Any]] = list(device_infos or [])
        self.devices: list[Any] = []

        self._elg: io.EventLoopGroup | None = None
        self._resolver: io.DefaultHostResolver | None = None
        self._bootstrap: io.ClientBootstrap | None = None
        self._mqtt: mqtt.Connection | None = None
        self._shadow: IotShadowClient | None = None

        self.healthy: bool = False
        self.health_reason: str = "not started"
        self._interrupted: bool = False
        self._stopping: bool = False
        self._grace_handle: asyncio.TimerHandle | None = None
        self._watchdog_handle: asyncio.TimerHandle | None = None
        self._refresh_task: asyncio.Task | None = None
        self._rebuild_task: asyncio.Task | None = None
        self._resume_task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    async def async_start(self) -> None:
        if self._uses_cloud:
            await self._async_login()
            self.device_infos = await self._async_fetch_iot_devices()
            if not self.device_infos:
                raise HatchCloudError("No Hatch IoT devices found for this account")
            await self.async_refresh_credentials()
        elif not self.device_infos:
            raise HatchCloudError("device_infos are required when using a custom connection factory")

        self._elg, self._resolver, self._bootstrap = await self._loop.run_in_executor(
            None, self._make_bootstrap
        )
        self._mqtt, self._shadow = await self._async_build_and_connect()
        try:
            self.devices = await self._async_create_devices(self._shadow)
        except Exception:
            await self._async_disconnect(self._mqtt)
            self._mqtt = self._shadow = None
            raise
        if not self.devices:
            await self._async_disconnect(self._mqtt)
            self._mqtt = self._shadow = None
            raise HatchCloudError(
                "No supported Hatch devices found (products: %s)"
                % [d.get("product") for d in self.device_infos]
            )

        if self._uses_cloud:
            self._refresh_task = self._loop.create_task(self._credential_refresh_loop())
        self._set_health(True, "connected")

    async def async_stop(self) -> None:
        self._stopping = True
        self._cancel_timers()
        for task in (self._refresh_task, self._rebuild_task, self._resume_task):
            await self._async_cancel(task)
        for device in self.devices:
            if isinstance(device, LegacyRestoreDevice):
                device.detach()
        if self._mqtt is not None:
            await self._async_disconnect(self._mqtt)
            self._mqtt = self._shadow = None
        self.healthy = False
        self.health_reason = "stopped"

    async def async_resync(self) -> None:
        """Re-request every device's shadow (manual refresh)."""
        if self._shadow is None:
            raise HatchCloudError("not connected")
        for device in self.devices:
            if isinstance(device, LegacyRestoreDevice):
                await device.async_refresh()
            else:
                await self._loop.run_in_executor(None, device.refresh)

    async def async_rebuild(self) -> None:
        """Force a make-before-break rebuild of the MQTT connection."""
        await self._async_rebuild_once()

    def snapshots(self) -> dict[str, dict[str, Any]]:
        return {device.thing_name: _snapshot(device) for device in self.devices}

    def device_by_thing_name(self, thing_name: str) -> Any | None:
        return next((d for d in self.devices if d.thing_name == thing_name), None)

    # ------------------------------------------------------------------ REST

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
        return None  # pragma: no cover - loop always returns or raises

    async def _async_login(self) -> None:
        try:
            self._auth_token = await self._retry_rate_limited(
                lambda: self._api.login(email=self._email, password=self._password)
            )
        except AuthError as err:
            raise HatchAuthError("Hatch rejected the stored credentials") from err
        except ClientError as err:
            if "Login failed" in str(err):
                raise HatchAuthError("Hatch rejected the stored credentials") from err
            raise HatchCloudError(f"Hatch login failed: {err}") from err

    async def _async_fetch_iot_devices(self) -> list[dict[str, Any]]:
        member = await self._retry_rate_limited(lambda: self._api.member(auth_token=self._auth_token))
        member_products = member.get("products", []) if isinstance(member, dict) else []
        products = list(dict.fromkeys([*KNOWN_IOT_PRODUCTS, *member_products]))
        query = urlencode([("iotProducts", product) for product in products])
        url = f"{PREFERRED_API_BASE}service/app/iotDevice/v2/fetch?{query}"
        headers = {"X-HatchBaby-Auth": self._auth_token, "USER_AGENT": self._client_id_prefix}

        async def _get():
            response = await self._api.api_session.get(url=url, headers=headers)
            if response.status == 429:
                raise RateError("rate limited fetching iot devices")
            if response.status >= 400:
                raise HatchCloudError(f"iotDevice fetch failed with HTTP {response.status}")
            return await response.json()

        response_json = await self._retry_rate_limited(_get)
        if response_json.get("errorCode") == 1001:
            raise HatchAuthError("Hatch session invalid while fetching devices")
        payload = response_json.get("payload", [])
        devices = payload if isinstance(payload, list) else []
        _LOGGER.debug(
            "Hatch iot devices: %s",
            [(d.get("name"), d.get("product"), d.get("thingName")) for d in devices],
        )
        return devices

    async def async_refresh_credentials(self) -> None:
        """Fetch a fresh Cognito credential set (re-logging in once if the session died)."""
        try:
            aws_token = await self._retry_rate_limited(lambda: self._api.token(auth_token=self._auth_token))
        except AuthError:
            _LOGGER.info("Hatch session expired; logging in again")
            await self._async_login()
            try:
                aws_token = await self._retry_rate_limited(
                    lambda: self._api.token(auth_token=self._auth_token)
                )
            except AuthError as err:
                raise HatchAuthError("Hatch rejected the stored credentials") from err
        aws_credentials = await self._retry_rate_limited(
            lambda: self._aws_http.aws_credentials(
                region=aws_token["region"],
                identityId=aws_token["identityId"],
                aws_token=aws_token["token"],
            )
        )
        raw = aws_credentials["Credentials"]
        expiry = datetime.fromtimestamp(raw["Expiration"], UTC)
        creds = AwsCredentials(raw["AccessKeyId"], raw["SecretKey"], raw["SessionToken"], expiry)
        with self._creds_lock:
            self._creds = creds
        self._aws_token = aws_token
        self.credentials_expiry = expiry
        self.credential_refreshes += 1
        _LOGGER.debug("Hatch AWS credentials refreshed; expire at %s", expiry.isoformat())

    def _delegate_get_credentials(self) -> AwsCredentials:
        # Runs on a CRT thread during every websocket handshake; must not block.
        with self._creds_lock:
            if self._creds is None:
                raise RuntimeError("Hatch AWS credentials not loaded yet")
            return self._creds

    async def _credential_refresh_loop(self) -> None:
        while not self._stopping:
            assert self.credentials_expiry is not None
            delay = (
                self.credentials_expiry - datetime.now(UTC) - self._credential_refresh_margin
            ).total_seconds()
            await asyncio.sleep(max(30.0, delay))
            backoff = REBUILD_BACKOFF_MIN_S
            while not self._stopping:
                try:
                    await self.async_refresh_credentials()
                    break
                except HatchAuthError as err:
                    _LOGGER.error("Hatch credential refresh failed: %s", err)
                    self._on_auth_failed()
                    return
                except (HatchCloudError, ClientError, TimeoutError, RateError, KeyError) as err:
                    _LOGGER.warning("Hatch credential refresh failed (%s); retrying in %ss", err, backoff)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, REBUILD_BACKOFF_MAX_S)

    # ------------------------------------------------------------------ MQTT

    @staticmethod
    def _make_bootstrap() -> tuple[io.EventLoopGroup, io.DefaultHostResolver, io.ClientBootstrap]:
        elg = io.EventLoopGroup(1)
        resolver = io.DefaultHostResolver(elg)
        return elg, resolver, io.ClientBootstrap(elg, resolver)

    def _client_id(self) -> str:
        safe_email = sub("[^a-z]", "", self._email, flags=IGNORECASE).lower()
        return f"{self._client_id_prefix}/{safe_email}/{uuid4()}"

    async def _async_build_and_connect(self) -> tuple[mqtt.Connection, IotShadowClient]:
        assert self._bootstrap is not None
        params = ConnectionParams(
            client_id=self._client_id(),
            client_bootstrap=self._bootstrap,
            on_connection_interrupted=self._crt_on_interrupted,
            on_connection_resumed=self._crt_on_resumed,
            on_connection_success=self._crt_on_success,
            on_connection_failure=self._crt_on_failure,
        )
        if self._uses_cloud:
            params.credentials_provider = AwsCredentialsProvider.new_delegate(self._delegate_get_credentials)
            params.region = self._aws_token["region"]
            params.endpoint = self._aws_token["endpoint"].removeprefix("https://").removeprefix("wss://")
        connection = await self._loop.run_in_executor(None, self._connection_factory, params)
        await asyncio.wait_for(asyncio.wrap_future(connection.connect()), CONNECT_TIMEOUT_S)
        _LOGGER.debug("Hatch MQTT connected (client_id=%s)", params.client_id)
        return connection, IotShadowClient(connection)

    async def _async_disconnect(self, connection: mqtt.Connection) -> None:
        try:
            await asyncio.wait_for(asyncio.wrap_future(connection.disconnect()), OP_TIMEOUT_S)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Hatch MQTT disconnect was not clean: %s", err)

    async def _async_create_devices(self, shadow: IotShadowClient) -> list[Any]:
        devices: list[Any] = []
        for info in self.device_infos:
            product = info.get("product")
            name, thing_name, mac = info.get("name"), info.get("thingName"), info.get("macAddress")
            if not (product and name and thing_name and mac):
                continue
            if product == "restore":
                device = LegacyRestoreDevice(device_name=name, thing_name=thing_name, mac=mac)
                await device.async_attach(shadow)
            elif product in LIBRARY_MODELS:
                # Library constructors subscribe + block; keep them off the loop.
                device = await self._loop.run_in_executor(
                    None,
                    partial(
                        LIBRARY_MODELS[product],
                        device_name=name,
                        thing_name=thing_name,
                        mac=mac,
                        shadow_client=shadow,
                    ),
                )
            else:
                _LOGGER.info("Skipping unsupported Hatch product %s (%s)", product, name)
                continue
            device.register_callback(
                partial(self._loop.call_soon_threadsafe, self._on_device_update, thing_name)
            )
            devices.append(device)
        return devices

    async def _async_attach_devices(self, shadow: IotShadowClient) -> None:
        for device in self.devices:
            if isinstance(device, LegacyRestoreDevice):
                await device.async_attach(shadow)
            else:
                await self._loop.run_in_executor(
                    None,
                    partial(
                        ShadowClientSubscriberMixin.__init__,
                        device,
                        device_name=device.device_name,
                        thing_name=device.thing_name,
                        mac=device.mac,
                        shadow_client=shadow,
                        favorites=getattr(device, "favorites", None),
                        sounds=getattr(device, "sounds", None),
                    ),
                )

    # ------------------------------------------------------------------ CRT-thread callbacks

    def _crt_on_interrupted(self, connection: mqtt.Connection, error: Any, **kwargs: Any) -> None:
        self._loop.call_soon_threadsafe(self._handle_interrupted, connection, str(error))

    def _crt_on_resumed(
        self, connection: mqtt.Connection, return_code: Any, session_present: bool, **kwargs: Any
    ) -> None:
        self._loop.call_soon_threadsafe(self._handle_resumed, connection, bool(session_present))

    def _crt_on_success(self, connection: mqtt.Connection, callback_data: Any, **kwargs: Any) -> None:
        self._loop.call_soon_threadsafe(
            _LOGGER.debug,
            "Hatch MQTT connect succeeded (session_present=%s)",
            getattr(callback_data, "session_present", None),
        )

    def _crt_on_failure(self, connection: mqtt.Connection, callback_data: Any, **kwargs: Any) -> None:
        self._loop.call_soon_threadsafe(
            _LOGGER.warning,
            "Hatch MQTT connect attempt failed: %s",
            getattr(callback_data, "error", callback_data),
        )

    # ------------------------------------------------------------------ loop-thread handlers

    def _handle_interrupted(self, connection: mqtt.Connection, error: str) -> None:
        if connection is not self._mqtt or self._stopping:
            return
        _LOGGER.info("Hatch MQTT connection interrupted: %s", error)
        self._interrupted = True
        if self._grace_handle is None:
            self._grace_handle = self._loop.call_later(self._unavailable_grace_s, self._grace_expired, error)
        if self._watchdog_handle is None:
            self._watchdog_handle = self._loop.call_later(self._watchdog_rebuild_s, self._watchdog_fired)

    def _handle_resumed(self, connection: mqtt.Connection, session_present: bool) -> None:
        if connection is not self._mqtt or self._stopping:
            return
        _LOGGER.info("Hatch MQTT connection resumed (session_present=%s)", session_present)
        self._interrupted = False
        self._cancel_timers()
        if self._rebuild_task is not None and not self._rebuild_task.done():
            _LOGGER.debug("Ignoring resume; a connection rebuild is already in progress")
            return
        if self._resume_task is None or self._resume_task.done():
            self._resume_task = self._loop.create_task(self._async_after_resume(self._shadow))

    async def _async_after_resume(self, shadow: IotShadowClient | None) -> None:
        if shadow is None:
            return
        try:
            await self._async_attach_devices(shadow)
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Hatch resync after resume failed (%s); rebuilding connection", err)
            self._schedule_rebuild()
            return
        if shadow is not self._shadow:
            # A rebuild swapped connections while we were re-attaching; it owns the devices now.
            return
        self._set_health(True, "resumed")

    def _grace_expired(self, error: str) -> None:
        self._grace_handle = None
        if self._interrupted:
            self._set_health(False, f"MQTT interrupted: {error}")

    def _watchdog_fired(self) -> None:
        self._watchdog_handle = None
        if self._interrupted:
            _LOGGER.warning(
                "Hatch MQTT still interrupted after %ss; rebuilding connection", self._watchdog_rebuild_s
            )
            self._schedule_rebuild()

    def _schedule_rebuild(self) -> None:
        if self._rebuild_task is None or self._rebuild_task.done():
            self._rebuild_task = self._loop.create_task(self._async_rebuild_loop())

    async def _async_rebuild_loop(self) -> None:
        backoff = REBUILD_BACKOFF_MIN_S
        while not self._stopping:
            try:
                await self._async_rebuild_once()
                return
            except HatchAuthError as err:
                _LOGGER.error("Hatch rebuild failed: %s", err)
                self._set_health(False, str(err))
                self._on_auth_failed()
                return
            except Exception as err:  # noqa: BLE001
                _LOGGER.warning("Hatch connection rebuild failed (%s); retrying in %ss", err, backoff)
                if self._interrupted or not self.healthy:
                    self._set_health(False, f"rebuild failed: {err}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, REBUILD_BACKOFF_MAX_S)

    async def _async_rebuild_once(self) -> None:
        await self._async_cancel(self._resume_task)
        if self._uses_cloud:
            await self.async_refresh_credentials()
        new_conn, new_shadow = await self._async_build_and_connect()
        try:
            await self._async_attach_devices(new_shadow)
        except Exception:
            await self._async_disconnect(new_conn)
            raise
        old_conn = self._mqtt
        self._mqtt, self._shadow = new_conn, new_shadow
        self._interrupted = False
        self._cancel_timers()
        self._set_health(True, "rebuilt")
        if old_conn is not None:
            await self._async_disconnect(old_conn)

    @staticmethod
    async def _async_cancel(task: asyncio.Task | None) -> None:
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _cancel_timers(self) -> None:
        for attr in ("_grace_handle", "_watchdog_handle"):
            handle = getattr(self, attr)
            if handle is not None:
                handle.cancel()
                setattr(self, attr, None)

    def _set_health(self, healthy: bool, reason: str) -> None:
        changed = healthy != self.healthy
        self.healthy = healthy
        self.health_reason = reason
        if changed:
            (_LOGGER.info if healthy else _LOGGER.warning)(
                "Hatch cloud session %s: %s", "healthy" if healthy else "unhealthy", reason
            )
            self._on_health_changed(healthy, reason)


def _snapshot(device: Any) -> dict[str, Any]:
    if isinstance(device, LegacyRestoreDevice):
        return device.as_dict()
    data = device.__repr__()
    return data if isinstance(data, dict) else {"repr": str(data)}
