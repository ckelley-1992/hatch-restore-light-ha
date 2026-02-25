#!/usr/bin/env python3
"""Experimental test for independent light/sound control on legacy Hatch Restore."""

from __future__ import annotations

import argparse
import asyncio
from functools import partial
import getpass
import json
from re import IGNORECASE, sub
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
from hatch_rest_api import AwsHttp, Hatch
from hatch_rest_api import hatch as hatch_module
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
    parser = argparse.ArgumentParser(
        description="Test whether legacy Restore supports independent light/sound control."
    )
    parser.add_argument("--email", required=True, help="Hatch account email.")
    parser.add_argument("--password", default=None, help="Hatch password (prompt if omitted).")
    parser.add_argument("--thing-name", default=None, help="Optional thingName for a specific restore.")
    parser.add_argument("--wait-seconds", type=float, default=2.5, help="Wait after each write.")
    parser.add_argument(
        "--active-matrix",
        action="store_true",
        help="Run active payload matrix intended to trigger real hardware changes.",
    )
    return parser.parse_args()


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


def _state_summary(reported: dict[str, Any]) -> dict[str, Any]:
    content = reported.get("content", {}) if isinstance(reported, dict) else {}
    color = reported.get("color", {}) if isinstance(reported, dict) else {}
    sound = reported.get("sound", {}) if isinstance(reported, dict) else {}
    return {
        "content.playing": content.get("playing"),
        "content.step": content.get("step"),
        "color.enabled": color.get("enabled"),
        "color.id": color.get("id"),
        "color.i": color.get("i"),
        "sound.enabled": sound.get("enabled"),
        "sound.id": sound.get("id"),
        "sound.v": sound.get("v"),
    }


class ShadowSession:
    def __init__(self, shadow_client: IotShadowClient, thing_name: str):
        self.shadow_client = shadow_client
        self.thing_name = thing_name
        self._get_event = threading.Event()
        self._get_payload: dict[str, Any] = {}

    def setup(self) -> None:
        def on_get_accepted(response: iotshadow.GetShadowResponse):
            reported = response.state.reported if response.state else {}
            self._get_payload = reported if isinstance(reported, dict) else {}
            self._get_event.set()

        self.shadow_client.subscribe_to_get_shadow_accepted(
            request=iotshadow.GetShadowSubscriptionRequest(thing_name=self.thing_name),
            qos=mqtt.QoS.AT_LEAST_ONCE,
            callback=on_get_accepted,
        )[0].result()

    async def get_reported(self, timeout_s: float = 10.0) -> dict[str, Any]:
        self._get_event.clear()
        self.shadow_client.publish_get_shadow(
            request=iotshadow.GetShadowRequest(thing_name=self.thing_name, client_token=None),
            qos=mqtt.QoS.AT_LEAST_ONCE,
        ).result()
        ok = await asyncio.to_thread(self._get_event.wait, timeout_s)
        if not ok:
            raise TimeoutError("Timed out waiting for get_shadow.")
        return dict(self._get_payload)

    def update_desired(self, patch: dict[str, Any]) -> None:
        self.shadow_client.publish_update_shadow(
            iotshadow.UpdateShadowRequest(
                thing_name=self.thing_name,
                state=iotshadow.ShadowState(desired=patch),
            ),
            mqtt.QoS.AT_LEAST_ONCE,
        ).result()


async def _run(args: argparse.Namespace) -> int:
    if not args.password:
        args.password = getpass.getpass("Hatch password: ")

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
            headers = {"X-HatchBaby-Auth": token, "USER_AGENT": "hatch_restore_independence_test"}
            response = await _retry_rate_limited(lambda: session.get(url, headers=headers))
            payload = await response.json()
            devices = payload.get("payload", []) if isinstance(payload, dict) else []
            restore_devices = [d for d in devices if d.get("product") == "restore"]
            if not restore_devices:
                print("No legacy `product=restore` devices found.")
                return 1

            target = restore_devices[0]
            if args.thing_name:
                picked = next((d for d in restore_devices if d.get("thingName") == args.thing_name), None)
                if not picked:
                    print(f"thingName not found: {args.thing_name}")
                    return 1
                target = picked

            print(f"Using device: name={target.get('name')!r} thingName={target.get('thingName')!r}")

            aws_token = await _retry_rate_limited(lambda: api.token(auth_token=token))
            aws_http = AwsHttp(session)
            aws_credentials = await _retry_rate_limited(
                lambda: aws_http.aws_credentials(
                    region=aws_token["region"],
                    identityId=aws_token["identityId"],
                    aws_token=aws_token["token"],
                )
            )
            creds = aws_credentials["Credentials"]
            provider = AwsCredentialsProvider.new_static(
                creds["AccessKeyId"],
                creds["SecretKey"],
                session_token=creds["SessionToken"],
            )
            loop = asyncio.get_running_loop()
            event_loop_group = io.EventLoopGroup(1)
            resolver = io.DefaultHostResolver(event_loop_group)
            bootstrap = io.ClientBootstrap(event_loop_group, resolver)
            safe_email = sub("[^a-z]", "", args.email, flags=IGNORECASE).lower()
            mqtt_connection = await loop.run_in_executor(
                None,
                partial(
                    websockets_with_default_aws_signing,
                    region=aws_token["region"],
                    credentials_provider=provider,
                    keep_alive_secs=30,
                    client_bootstrap=bootstrap,
                    endpoint=aws_token["endpoint"].lstrip("https://"),
                    client_id=f"hatch_independence/{safe_email}/{str(uuid4())}",
                ),
            )
            connect_future = await loop.run_in_executor(None, mqtt_connection.connect)
            await loop.run_in_executor(None, connect_future.result)

            shadow = ShadowSession(IotShadowClient(mqtt_connection), target["thingName"])
            shadow.setup()

            baseline = await shadow.get_reported()
            print("Baseline:", json.dumps(_state_summary(baseline), indent=2))

            # Force a known off baseline similar to Homebridge off.
            shadow.update_desired({"content": {"playing": "none", "paused": False, "offset": 0, "step": 0}})
            await asyncio.sleep(args.wait_seconds)
            off_state = await shadow.get_reported()
            print("After content=none:", json.dumps(_state_summary(off_state), indent=2))

            # Attempt light-only change.
            shadow.update_desired({"color": {"enabled": True}})
            await asyncio.sleep(args.wait_seconds)
            light_only = await shadow.get_reported()
            print("After color.enabled=true:", json.dumps(_state_summary(light_only), indent=2))

            # Attempt sound-only change.
            shadow.update_desired({"sound": {"enabled": True}})
            await asyncio.sleep(args.wait_seconds)
            sound_only = await shadow.get_reported()
            print("After sound.enabled=true:", json.dumps(_state_summary(sound_only), indent=2))

            if args.active_matrix:
                baseline_color_id = off_state.get("color", {}).get("id", 229)
                baseline_color_i = off_state.get("color", {}).get("i", 32767)
                baseline_sound_id = off_state.get("sound", {}).get("id", 10040)

                test_cases: list[tuple[str, dict[str, Any]]] = [
                    (
                        "light_only_remote",
                        {
                            "content": {"playing": "remote", "paused": False, "offset": 0, "step": 0},
                            "color": {"enabled": True, "id": baseline_color_id, "i": baseline_color_i},
                            "sound": {"enabled": False},
                        },
                    ),
                    (
                        "sound_only_remote",
                        {
                            "content": {"playing": "remote", "paused": False, "offset": 0, "step": 0},
                            "sound": {"enabled": True, "id": baseline_sound_id, "v": 32767},
                            "color": {"enabled": False},
                        },
                    ),
                    (
                        "both_remote",
                        {
                            "content": {"playing": "remote", "paused": False, "offset": 0, "step": 0},
                            "color": {"enabled": True, "id": baseline_color_id, "i": baseline_color_i},
                            "sound": {"enabled": True, "id": baseline_sound_id, "v": 32767},
                        },
                    ),
                    (
                        "routine_step1_fallback",
                        {
                            "content": {"playing": "routine", "paused": False, "offset": 0, "step": 1},
                        },
                    ),
                ]

                print("\nRunning active matrix. Observe the physical device after each step.")
                for name, patch in test_cases:
                    print(f"\nApplying {name}: {json.dumps(patch)}")
                    shadow.update_desired(patch)
                    await asyncio.sleep(args.wait_seconds)
                    current = await shadow.get_reported()
                    print(f"{name} -> {json.dumps(_state_summary(current), indent=2)}")

            # Cleanup by setting both disabled + content none.
            shadow.update_desired(
                {
                    "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
                    "color": {"enabled": False},
                    "sound": {"enabled": False},
                }
            )
            await asyncio.sleep(args.wait_seconds)
            final_state = await shadow.get_reported()
            print("Final cleanup state:", json.dumps(_state_summary(final_state), indent=2))

            print("\nInterpretation guide:")
            print("- If `color.enabled` changes while `sound.enabled`/`content.playing` do not, light can be independent.")
            print("- If `sound.enabled` changes while `color.enabled`/`content.playing` do not, sound can be independent.")
            print("- If only `content.playing` reliably changes, device is routine/content-coupled (Homebridge behavior).")
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
        print(f"Independence test failed: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
