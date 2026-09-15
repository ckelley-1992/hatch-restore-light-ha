#!/usr/bin/env python3
"""Probe / poke the Hatch Restore Gen 1 (legacy ``product=restore``) device shadow.

Read-only by default (prints the reported document). Optional writes publish a single desired
update built from the ``--set-*`` flags, and ``--watch-seconds N`` keeps printing timestamped
diffs of the reported state for N seconds afterwards (tap the device, watch what changes).

The experiment that settles how the integration should write during a routine::

    # 1. start the Hatch-app routine at step 1 and watch it settle
    hatch_restore_shadow_probe.py --set-content-playing routine --set-content-step 1 --watch-seconds 20
    # 2. partial write while in routine: does color.i change with playing still "routine"?
    hatch_restore_shadow_probe.py --set-color-enabled on --set-color-intensity 20 --watch-seconds 20
    # 3. advance to step 2 without tapping: does the device go light-off / sound-on?
    hatch_restore_shadow_probe.py --set-content-playing routine --set-content-step 2 --watch-seconds 30
    # 4. watch the physical tap / end of sound
    hatch_restore_shadow_probe.py --watch-seconds 180
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
from pathlib import Path
import threading
import time
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
from hatch_rest_api import hatch as hatch_module
from hatch_rest_api.aws_http import AwsHttp
from hatch_rest_api.errors import RateError

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
    parser = argparse.ArgumentParser(description="Inspect raw AWS IoT shadow for Hatch Restore devices.")
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
        help="Optional experimental write: desired.color.i as 0-100 percent (sent as raw 0-65535).",
    )
    parser.add_argument(
        "--set-color-enabled",
        choices=["on", "off"],
        default=None,
        help="Optional experimental write: desired.color.enabled true/false.",
    )
    parser.add_argument(
        "--set-sound-enabled", choices=["on", "off"], default=None, help="desired.sound.enabled"
    )
    parser.add_argument("--set-sound-volume", type=int, default=None, help="desired.sound.v (0-100 percent)")
    parser.add_argument("--set-sound-id", type=int, default=None, help="desired.sound.id")
    parser.add_argument(
        "--set-content-playing",
        choices=["none", "remote", "routine"],
        default=None,
        help="desired.content.playing (sent with paused=false, offset=0)",
    )
    parser.add_argument("--set-content-step", type=int, default=None, help="desired.content.step")
    parser.add_argument(
        "--list-sounds",
        action="store_true",
        help="ask Hatch's content endpoint for the Gen 1 sound catalog (id -> title) and exit",
    )
    parser.add_argument(
        "--watch-seconds",
        type=float,
        default=0,
        help="after any write, keep printing reported-state diffs for this many seconds",
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


SUMMARY_PATHS = (
    "connected",
    "content.playing",
    "content.step",
    "color.enabled",
    "color.id",
    "color.i",
    "sound.enabled",
    "sound.id",
    "sound.v",
)


def _summarize(reported: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for path in SUMMARY_PATHS:
        node: Any = reported
        for key in path.split("."):
            node = node.get(key) if isinstance(node, dict) else None
        if node is not None:
            summary[path] = node
    return summary


def _pct_to_raw(percent: int) -> int:
    return int(round(max(0, min(100, percent)) / 100 * 65535))


async def _list_sounds(api: Hatch, token: str) -> int:
    """Print whatever ``fetchByProduct`` returns for the legacy product (untested on Gen 1)."""
    for product in ("restore", "restoreIot"):
        try:
            payload = await _retry_rate_limited(
                lambda: api.content(auth_token=token, product=product, content=["sound"])
            )
        except Exception as err:  # noqa: BLE001
            print(f"product={product}: request failed: {err}")
            continue
        items = payload.get("contentItems") if isinstance(payload, dict) else None
        if not items:
            print(f"product={product}: no contentItems; raw payload: {json.dumps(payload)[:800]}")
            continue
        print(f"product={product}: {len(items)} sounds")
        for item in sorted(items, key=lambda i: i.get("id", 0)):
            print(f"  {item.get('id')!s:>6}  {item.get('title') or item.get('name')}")
        return 0
    return 1


async def _fetch_iot_devices(
    session: ClientSession, auth_token: str, member_products: list[str]
) -> list[dict[str, Any]]:
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
        endpoint=endpoint.removeprefix("https://"),
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

        if args.list_sounds:
            return await _list_sounds(api, token)

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

        tracked: dict[str, Any] = {}

        def on_update_shadow_accepted(response: iotshadow.UpdateShadowResponse):
            get_payload["last_update_version"] = response.version
            reported_delta = response.state.reported if response.state else None
            desired_echo = response.state.desired if response.state else None
            stamp = time.strftime("%H:%M:%S")
            if reported_delta:
                get_payload["last_update_state"] = reported_delta
                new_summary = _summarize(reported_delta)
                changes = {k: v for k, v in new_summary.items() if tracked.get(k) != v}
                tracked.update(new_summary)
                print(f"{stamp} reported v{response.version}: {json.dumps(changes)}", flush=True)
            elif desired_echo:
                print(
                    f"{stamp} desired  v{response.version} accepted: {json.dumps(desired_echo)}", flush=True
                )
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

        if isinstance(reported, dict):
            tracked.update(_summarize(reported))
            print(f"Summary: {json.dumps(tracked)}")

        desired: dict[str, Any] = {}
        desired_color: dict[str, Any] = {}
        if args.set_color_id is not None:
            desired_color["id"] = args.set_color_id
        if args.set_color_intensity is not None:
            desired_color["i"] = _pct_to_raw(args.set_color_intensity)
        if args.set_color_enabled is not None:
            desired_color["enabled"] = args.set_color_enabled == "on"
        if desired_color:
            desired["color"] = desired_color
        desired_sound: dict[str, Any] = {}
        if args.set_sound_id is not None:
            desired_sound["id"] = args.set_sound_id
        if args.set_sound_volume is not None:
            desired_sound["v"] = _pct_to_raw(args.set_sound_volume)
        if args.set_sound_enabled is not None:
            desired_sound["enabled"] = args.set_sound_enabled == "on"
        if desired_sound:
            desired["sound"] = desired_sound
        if args.set_content_playing is not None or args.set_content_step is not None:
            playing = args.set_content_playing or tracked.get("content.playing", "none")
            step = (
                args.set_content_step
                if args.set_content_step is not None
                else (1 if playing == "routine" else 0)
            )
            desired["content"] = {"playing": playing, "paused": False, "offset": 0, "step": step}

        if desired:
            print(f"Publishing desired update: {json.dumps(desired)}")
            update_event.clear()
            shadow_client.publish_update_shadow(
                iotshadow.UpdateShadowRequest(
                    thing_name=target["thingName"],
                    state=iotshadow.ShadowState(desired=desired),
                ),
                mqtt.QoS.AT_LEAST_ONCE,
            ).result(timeout=10)
            if not await asyncio.to_thread(update_event.wait, 8):
                print("No update acknowledgement received before timeout.")

        if args.watch_seconds > 0:
            print(f"Watching reported state for {args.watch_seconds:.0f}s (tap the device now)...")
            await asyncio.sleep(args.watch_seconds)
            print(f"Final summary: {json.dumps(tracked)}")

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
