#!/usr/bin/env python3
"""Local test for legacy Hatch Restore (`product=restore`) on/off behavior."""

from __future__ import annotations

import argparse
import asyncio
from functools import partial
import getpass
from pathlib import Path
from re import IGNORECASE, sub
import sys
from urllib.parse import urlencode
from uuid import uuid4
import importlib.util

from aiohttp import ClientSession
from awscrt import io
from awscrt.auth import AwsCredentialsProvider
from awsiot.iotshadow import IotShadowClient
from awsiot.mqtt_connection_builder import websockets_with_default_aws_signing
from hatch_rest_api import AwsHttp, Hatch
from hatch_rest_api.errors import RateError
from hatch_rest_api import hatch as hatch_module

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_DEVICE_PATH = PROJECT_ROOT / "custom_components" / "hatch_restore_light" / "legacy_restore_device.py"
spec = importlib.util.spec_from_file_location("legacy_restore_device", LEGACY_DEVICE_PATH)
if spec is None or spec.loader is None:
    raise RuntimeError(f"Unable to load LegacyRestoreDevice from {LEGACY_DEVICE_PATH}")
legacy_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legacy_module)
LegacyRestoreDevice = legacy_module.LegacyRestoreDevice

API_BASE = "https://prod-sleep.hatchbaby.com/"
KNOWN_PRODUCTS = [
    "restPlus",
    "riot",
    "riotPlus",
    "restMini",
    "restore",
    "restoreIot",
    "restoreV4",
    "restoreV5",
    "restBaby",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local legacy Restore on/off test.")
    parser.add_argument("--email", required=True, help="Hatch account email.")
    parser.add_argument("--password", default=None, help="Hatch password (prompt if omitted).")
    parser.add_argument("--thing-name", default=None, help="Optional thingName for a specific restore device.")
    parser.add_argument("--active-test", action="store_true", help="Actually send on/off commands.")
    parser.add_argument("--on-seconds", type=int, default=5, help="Seconds to stay on in active test.")
    args = parser.parse_args()
    if not args.password:
        args.password = getpass.getpass("Hatch password: ")
    return args


async def _retry_rate_limited(coro_factory, attempts: int = 5):
    wait_s = 2
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except RateError:
            if attempt >= attempts:
                raise
            print(f"Rate limited (429). Retrying in {wait_s}s ({attempt}/{attempts})...")
            await asyncio.sleep(wait_s)
            wait_s *= 2


async def _run(args: argparse.Namespace) -> int:
    hatch_module.API_URL = API_BASE
    mqtt_connection = None
    try:
        async with ClientSession() as session:
            api = Hatch(client_session=session)
            token = await _retry_rate_limited(lambda: api.login(email=args.email, password=args.password))
            member = await _retry_rate_limited(lambda: api.member(auth_token=token))
            member_products = member.get("products", []) if isinstance(member, dict) else []
            print(f"Member products: {member_products}")

            products = list(dict.fromkeys(KNOWN_PRODUCTS + member_products))
            query = urlencode([("iotProducts", product) for product in products])
            url = f"{API_BASE}service/app/iotDevice/v2/fetch?{query}"
            headers = {"X-HatchBaby-Auth": token, "USER_AGENT": "hatch_restore_local_test"}
            response = await _retry_rate_limited(lambda: session.get(url, headers=headers))
            payload = await response.json()
            devices = payload.get("payload", []) if isinstance(payload, dict) else []
            restore_devices = [device for device in devices if device.get("product") == "restore"]
            if not restore_devices:
                print("No legacy `product=restore` devices found.")
                return 1

            target = restore_devices[0]
            if args.thing_name:
                explicit = next((d for d in restore_devices if d.get("thingName") == args.thing_name), None)
                if not explicit:
                    print(f"thingName not found: {args.thing_name}")
                    return 1
                target = explicit

            print(
                f"Using restore device: name={target.get('name')!r} "
                f"thingName={target.get('thingName')!r} mac={target.get('macAddress')!r}"
            )

            aws_token = await _retry_rate_limited(lambda: api.token(auth_token=token))
            aws_http = AwsHttp(session)
            aws_credentials = await _retry_rate_limited(
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
            loop = asyncio.get_running_loop()
            event_loop_group = io.EventLoopGroup(1)
            host_resolver = io.DefaultHostResolver(event_loop_group)
            client_bootstrap = io.ClientBootstrap(event_loop_group, host_resolver)
            safe_email = sub("[^a-z]", "", args.email, flags=IGNORECASE).lower()
            mqtt_connection = await loop.run_in_executor(
                None,
                partial(
                    websockets_with_default_aws_signing,
                    region=aws_token["region"],
                    credentials_provider=credentials_provider,
                    keep_alive_secs=30,
                    client_bootstrap=client_bootstrap,
                    endpoint=aws_token["endpoint"].lstrip("https://"),
                    client_id=f"hatch_restore_local/{safe_email}/{str(uuid4())}",
                ),
            )
            connect_future = await loop.run_in_executor(None, mqtt_connection.connect)
            await loop.run_in_executor(None, connect_future.result)
            shadow_client = IotShadowClient(mqtt_connection)
            device = LegacyRestoreDevice(
                device_name=target["name"],
                thing_name=target["thingName"],
                mac=target["macAddress"],
                shadow_client=shadow_client,
            )

            await asyncio.sleep(2)
            print(f"Initial state: playing={device.current_playing!r} is_on={device.is_on}")

            if not args.active_test:
                print("Read-only test complete. Use --active-test to send on/off.")
                return 0

            print("Sending ON (routine step 1)...")
            device.turn_on_routine(step=1)
            await asyncio.sleep(max(1, args.on_seconds))
            print(f"After ON: playing={device.current_playing!r} is_on={device.is_on}")

            print("Sending OFF...")
            device.turn_off()
            await asyncio.sleep(2)
            print(f"After OFF: playing={device.current_playing!r} is_on={device.is_on}")
            return 0
    finally:
        if mqtt_connection is not None:
            try:
                mqtt_connection.disconnect().result()
            except Exception:
                pass


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as err:  # noqa: BLE001
        print(f"Local test failed: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
