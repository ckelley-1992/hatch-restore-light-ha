#!/usr/bin/env python3
"""Standalone smoke test for Hatch Restore devices."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
import getpass
import os
from urllib.parse import urlencode
from typing import Any

try:
    from aiohttp import ClientSession
    from hatch_rest_api import BaseError, Hatch, RestoreIot, RestoreV5, get_rest_devices
    from hatch_rest_api import hatch as hatch_module
    from hatch_rest_api.errors import RateError
except ModuleNotFoundError as err:
    missing = err.name or "dependency"
    raise SystemExit(
        f"Missing Python package: {missing}\n"
        "Install required packages in your active environment:\n"
        "  python3 -m pip install aiohttp hatch-rest-api"
    ) from err


@dataclass
class TestOptions:
    email: str
    password: str
    active_test: bool
    on_seconds: int


API_BASES = [
    "https://data.hatchbaby.com/",
    "https://prod-sleep.hatchbaby.com/",
]

HOMEBRIDGE_KNOWN_PRODUCTS = [
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


async def _call_with_rate_limit_retry(coro_factory, attempts: int = 4):
    """Retry Hatch API calls when temporary 429 throttling happens."""
    delay_seconds = 2
    for attempt in range(1, attempts + 1):
        try:
            return await coro_factory()
        except RateError:
            if attempt >= attempts:
                raise
            print(f"  Rate limited (429). Retrying in {delay_seconds}s (attempt {attempt}/{attempts})...")
            await asyncio.sleep(delay_seconds)
            delay_seconds *= 2


def _parse_args() -> TestOptions:
    parser = argparse.ArgumentParser(
        description="Validate Hatch credentials and Restore light control without Home Assistant."
    )
    parser.add_argument("--email", default=os.getenv("HATCH_EMAIL"), help="Hatch account email.")
    parser.add_argument(
        "--password",
        default=os.getenv("HATCH_PASSWORD"),
        help="Hatch account password. If omitted, prompt securely.",
    )
    parser.add_argument(
        "--active-test",
        action="store_true",
        help="Run a short light control test (turn on warm white then off).",
    )
    parser.add_argument(
        "--on-seconds",
        type=int,
        default=3,
        help="Seconds to keep light on during --active-test (default: 3).",
    )
    args = parser.parse_args()

    if not args.email:
        parser.error("Missing --email (or HATCH_EMAIL env var).")

    password = args.password or getpass.getpass("Hatch password: ")
    return TestOptions(
        email=args.email,
        password=password,
        active_test=args.active_test,
        on_seconds=max(1, args.on_seconds),
    )


async def _run(opts: TestOptions) -> int:
    mqtt_connection = None
    async with ClientSession() as session:
        print("Logging into Hatch cloud...")
        selected_base = None
        raw_devices: list[dict[str, Any]] = []
        last_member: dict[str, Any] = {}
        prod_sleep_token: str | None = None

        for base in API_BASES:
            hatch_module.API_URL = base
            print(f"Probing API base: {base}")
            try:
                api = Hatch(client_session=session)
                token = await _call_with_rate_limit_retry(
                    lambda: api.login(email=opts.email, password=opts.password)
                )
                if base == "https://prod-sleep.hatchbaby.com/":
                    prod_sleep_token = token
                print("  Login success.")
                member = await _call_with_rate_limit_retry(
                    lambda: api.member(auth_token=token)
                )
                devices = await _call_with_rate_limit_retry(
                    lambda: api.iot_devices(auth_token=token)
                )
                member_products = member.get("products", []) if isinstance(member, dict) else []
                print(
                    "  Member context: "
                    f"products={member_products!r} "
                    f"member_email={member.get('member', {}).get('email') if isinstance(member, dict) else None!r}"
                )
                print(f"  Raw iot device count: {len(devices)}")
                last_member = member
                raw_devices = devices
                if devices:
                    selected_base = base
                    break
            except RateError:
                print("  Rate limited by Hatch API (429). Wait 2-5 minutes and retry.")
                return 1
            except Exception as err:  # noqa: BLE001
                print(f"  Probe failed: {err}")

        if selected_base is None:
            print("No iot devices returned by built-in hatch_rest_api filters.")
            if last_member:
                print(
                    "Last successful member context: "
                    f"products={last_member.get('products')!r} "
                    f"member_email={last_member.get('member', {}).get('email')!r}"
                )
            print("Trying Homebridge-style iot product query...")
            if not prod_sleep_token:
                print("Could not obtain a prod-sleep auth token for Homebridge-style query.")
                return 1
            all_products = list(dict.fromkeys(HOMEBRIDGE_KNOWN_PRODUCTS + (last_member.get("products", []) if last_member else [])))
            query = urlencode([("iotProducts", p) for p in all_products])
            iot_url = f"{API_BASES[-1]}service/app/iotDevice/v2/fetch?{query}"
            headers = {"X-HatchBaby-Auth": prod_sleep_token, "USER_AGENT": "hatch_rest_api"}
            response = await _call_with_rate_limit_retry(lambda: session.get(iot_url, headers=headers))
            payload = await response.json()
            raw_devices = payload.get("payload", []) if isinstance(payload, dict) else []
            print(f"Homebridge-style raw iot device count: {len(raw_devices)}")
            if not raw_devices:
                print("No iot devices returned by Homebridge-style query either.")
                return 1
            selected_base = API_BASES[-1]

        hatch_module.API_URL = selected_base
        print(f"Using API base: {selected_base}")
        for idx, device in enumerate(raw_devices, start=1):
            print(
                f"  {idx}. name={device.get('name')!r} "
                f"product={device.get('product')!r} "
                f"thingName={device.get('thingName')!r} "
                f"macAddress={device.get('macAddress')!r}"
            )

        try:
            _, mqtt_connection, devices, expiration = await get_rest_devices(
                email=opts.email,
                password=opts.password,
                client_session=session,
                on_connection_interrupted=lambda: print("MQTT interrupted"),
                on_connection_resumed=lambda: print("MQTT resumed"),
            )
        except RateError:
            print(
                "Rate limited by Hatch API (429) during deep device bootstrap.\n"
                "Wait 2-5 minutes, then retry."
            )
            return 1
        except BaseError as err:
            if "No compatible devices found on this hatch account" not in str(err):
                raise

            print(f"Device compatibility error: {err}")
            supported = {
                "restMini",
                "restPlus",
                "riot",
                "riotPlus",
                "restBaby",
                "restoreIot",
                "restoreV4",
                "restoreV5",
            }
            unsupported = [d for d in raw_devices if d.get("product") not in supported]
            if unsupported:
                print("Found product values not supported by hatch_rest_api:")
                for d in unsupported:
                    print(f"  - {d.get('product')!r} ({d.get('name')!r})")
            return 1
        print(f"Discovered {len(devices)} Hatch device(s). Token expires at unix={int(expiration)}.")

        restore_devices = [d for d in devices if isinstance(d, (RestoreIot, RestoreV5))]
        if not restore_devices:
            print("No Restore devices found on this account.")
            return 1

        print("Restore devices:")
        for idx, device in enumerate(restore_devices, start=1):
            print(
                f"  {idx}. name={device.device_name!r} model={device.__class__.__name__} "
                f"thing_name={device.thing_name} light_on={device.is_light_on} "
                f"brightness={device.brightness} rgbw=({device.red},{device.green},{device.blue},{device.white})"
            )

        if opts.active_test:
            target = restore_devices[0]
            print(f"Running active light test on: {target.device_name!r}")
            target.set_color(255, 180, 100, 0, 25)
            print(f"Light turned on for {opts.on_seconds}s...")
            await asyncio.sleep(opts.on_seconds)
            target.turn_light_off()
            print("Light turned off. Active test complete.")
        else:
            print("Read-only smoke test complete (no device state changed).")

    if mqtt_connection is not None:
        try:
            mqtt_connection.disconnect().result()
        except Exception:
            pass
    return 0


def main() -> int:
    opts = _parse_args()
    try:
        return asyncio.run(_run(opts))
    except KeyboardInterrupt:
        return 130
    except Exception as err:  # noqa: BLE001
        if "Login failed for" in str(err):
            print(
                "Smoke test failed: Hatch login rejected these credentials.\n"
                "Double-check email/password, then log out and back into the Hatch app once and retry."
            )
            return 2
        print(f"Smoke test failed: {err}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
