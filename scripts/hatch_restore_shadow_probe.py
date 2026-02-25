#!/usr/bin/env python3
"""Probe Hatch Restore (legacy `product=restore`) device shadow state."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import threading
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

from aiohttp import ClientSession
from awscrt import io, mqtt
from awscrt.auth import AwsCredentialsProvider
from awsiot import iotshadow
from awsiot.iotshadow import IotShadowClient
from awsiot.mqtt_connection_builder import websockets_with_default_aws_signing
from hatch_rest_api import Hatch
from hatch_rest_api.aws_http import AwsHttp
from hatch_rest_api.errors import RateError
from hatch_rest_api import hatch as hatch_module

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
    parser = argparse.ArgumentParser(
        description="Inspect raw AWS IoT shadow for Hatch Restore devices."
    )
    parser.add_argument("--email", default=os.getenv("HATCH_EMAIL"), help="Hatch account email.")
    parser.add_argument(
        "--password",
        default=os.getenv("HATCH_PASSWORD"),
        help="Hatch account password (prompted if omitted).",
    )
    parser.add_argument(
        "--thing-name",
        default=None,
        help="Specific thingName to inspect. Defaults to first restore device.",
    )
    parser.add_argument(
        "--dump-json",
        default=None,
        help="Optional path to write the raw reported shadow JSON.",
    )
    parser.add_argument(
        "--set-color-id",
        type=int,
        default=None,
        help="Optional experimental write: desired.color.id value.",
    )
    parser.add_argument(
        "--set-color-intensity",
        type=int,
        default=None,
        help="Optional experimental write: desired.color.i (0-100 percent).",
    )
    parser.add_argument(
        "--set-color-enabled",
        choices=["on", "off"],
        default=None,
        help="Optional experimental write: desired.color.enabled true/false.",
    )
    args = parser.parse_args()
    if not args.email:
        parser.error("Missing --email (or HATCH_EMAIL env var).")
    if not args.password:
        args.password = getpass.getpass("Hatch password: ")
    return args


async def _retry_rate_limited(coro_factory, attempts: int = 5):
    wait_s = 2
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except RateError:
            if attempt == attempts:
                raise
            print(f"Rate limited (429). Retrying in {wait_s}s ({attempt}/{attempts})...")
            await asyncio.sleep(wait_s)
            wait_s *= 2


async def _fetch_iot_devices(session: ClientSession, auth_token: str, member_products: list[str]) -> list[dict[str, Any]]:
    products = list(dict.fromkeys(KNOWN_PRODUCTS + member_products))
    query = urlencode([("iotProducts", product) for product in products])
    url = f"{API_BASE}service/app/iotDevice/v2/fetch?{query}"
    headers = {"X-HatchBaby-Auth": auth_token, "USER_AGENT": "hatch_restore_shadow_probe"}
    response = await _retry_rate_limited(lambda: session.get(url, headers=headers))
    payload = await response.json()
    return payload.get("payload", []) if isinstance(payload, dict) else []


def _connect_mqtt(endpoint: str, region: str, credentials: dict[str, Any], email: str):
    provider = AwsCredentialsProvider.new_static(
        credentials["AccessKeyId"],
        credentials["SecretKey"],
        session_token=credentials["SessionToken"],
    )
    event_loop_group = io.EventLoopGroup(1)
    resolver = io.DefaultHostResolver(event_loop_group)
    bootstrap = io.ClientBootstrap(event_loop_group, resolver)
    safe_email = "".join(ch for ch in email.lower() if ch.isalpha())
    return websockets_with_default_aws_signing(
        region=region,
        credentials_provider=provider,
        keep_alive_secs=30,
        client_bootstrap=bootstrap,
        endpoint=endpoint.lstrip("https://"),
        client_id=f"hatch_shadow_probe/{safe_email}/{uuid4()}",
    )


async def _run(args: argparse.Namespace) -> int:
    hatch_module.API_URL = API_BASE

    mqtt_connection = None
    async with ClientSession() as session:
        api = Hatch(client_session=session)
        print(f"Logging in via {API_BASE} ...")
        token = await _retry_rate_limited(lambda: api.login(email=args.email, password=args.password))
        member = await _retry_rate_limited(lambda: api.member(auth_token=token))
        member_products = member.get("products", []) if isinstance(member, dict) else []
        print(f"Member products: {member_products}")

        devices = await _fetch_iot_devices(session, token, member_products)
        restore_devices = [d for d in devices if d.get("product") == "restore"]
        if not restore_devices:
            print("No `product=restore` devices found from Homebridge-style query.")
            return 1

        print("Restore devices discovered:")
        for device in restore_devices:
            print(
                f"  - name={device.get('name')!r} "
                f"thingName={device.get('thingName')!r} "
                f"mac={device.get('macAddress')!r}"
            )

        target = None
        if args.thing_name:
            target = next((d for d in restore_devices if d.get("thingName") == args.thing_name), None)
            if not target:
                print(f"Requested --thing-name not found: {args.thing_name}")
                return 1
        else:
            target = restore_devices[0]

        print(f"Using thingName: {target['thingName']}")

        aws_token = await _retry_rate_limited(lambda: api.token(auth_token=token))
        aws_http = AwsHttp(session)
        aws_creds = await _retry_rate_limited(
            lambda: aws_http.aws_credentials(
                region=aws_token["region"],
                identityId=aws_token["identityId"],
                aws_token=aws_token["token"],
            )
        )
        creds = aws_creds["Credentials"]

        mqtt_connection = _connect_mqtt(
            endpoint=aws_token["endpoint"],
            region=aws_token["region"],
            credentials=creds,
            email=args.email,
        )
        await asyncio.to_thread(lambda: mqtt_connection.connect().result())
        shadow_client = IotShadowClient(mqtt_connection)
        print("MQTT connected.")

        get_event = threading.Event()
        get_payload: dict[str, Any] = {}
        update_event = threading.Event()

        def on_get_shadow_accepted(response: iotshadow.GetShadowResponse):
            state = response.state.reported if response.state else None
            get_payload["version"] = response.version
            get_payload["reported"] = state or {}
            get_payload["full_response"] = str(response)
            get_event.set()

        def on_update_shadow_accepted(response: iotshadow.UpdateShadowResponse):
            get_payload["last_update_version"] = response.version
            get_payload["last_update_state"] = response.state.reported if response.state else {}
            update_event.set()

        shadow_client.subscribe_to_get_shadow_accepted(
            request=iotshadow.GetShadowSubscriptionRequest(thing_name=target["thingName"]),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=on_get_shadow_accepted,
        )[0].result()
        shadow_client.subscribe_to_update_shadow_accepted(
            request=iotshadow.UpdateShadowSubscriptionRequest(thing_name=target["thingName"]),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=on_update_shadow_accepted,
        )[0].result()

        shadow_client.publish_get_shadow(
            request=iotshadow.GetShadowRequest(thing_name=target["thingName"], client_token=None),
            qos=mqtt.QoS.AT_LEAST_ONCE,
        ).result()

        if not await asyncio.to_thread(get_event.wait, 12):
            print("Timed out waiting for get_shadow response.")
            return 1

        reported = get_payload.get("reported", {})
        print(f"Shadow version: {get_payload.get('version')}")
        print(f"Top-level reported keys: {sorted(reported.keys()) if isinstance(reported, dict) else []}")
        if isinstance(reported, dict) and isinstance(reported.get("color"), dict):
            print(f"Reported color payload: {json.dumps(reported['color'], indent=2)}")
        if isinstance(reported, dict) and isinstance(reported.get("content"), dict):
            print(f"Reported content payload: {json.dumps(reported['content'], indent=2)}")

        if args.dump_json:
            dump_path = Path(args.dump_json).expanduser().resolve()
            dump_path.write_text(json.dumps(reported, indent=2), encoding="utf-8")
            print(f"Wrote reported shadow to {dump_path}")

        has_updates = (
            args.set_color_id is not None
            or args.set_color_intensity is not None
            or args.set_color_enabled is not None
        )
        if has_updates:
            desired_color: dict[str, Any] = {}
            if args.set_color_id is not None:
                desired_color["id"] = args.set_color_id
            if args.set_color_intensity is not None:
                desired_color["i"] = max(0, min(100, args.set_color_intensity))
            if args.set_color_enabled is not None:
                desired_color["enabled"] = args.set_color_enabled == "on"

            print(f"Publishing desired color update: {desired_color}")
            shadow_client.publish_update_shadow(
                iotshadow.UpdateShadowRequest(
                    thing_name=target["thingName"],
                    state=iotshadow.ShadowState(desired={"color": desired_color}),
                ),
                mqtt.QoS.AT_LEAST_ONCE,
            ).result()

            if await asyncio.to_thread(update_event.wait, 8):
                print(f"Update acknowledged. Version={get_payload.get('last_update_version')}")
            else:
                print("No update acknowledgement received before timeout.")

    if mqtt_connection is not None:
        try:
            mqtt_connection.disconnect().result()
        except Exception:
            pass
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(_run(args))
    except KeyboardInterrupt:
        return 130
    except Exception as err:  # noqa: BLE001
        print(f"Probe failed: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

