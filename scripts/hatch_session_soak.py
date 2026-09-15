#!/usr/bin/env python3
"""Drive the integration's ``hatch_cloud`` session outside Home Assistant.

Two modes:

* **cloud** (``--email``/``--password``): the real Hatch account and device. Proves login,
  discovery, credential refresh (``--refresh-margin-minutes 55`` makes the first refresh happen a
  few minutes in), and that state keeps flowing past the ~1 h credential expiry
  (``--soak-minutes 75``). Toggle Wi-Fi off for ~30 s mid-run to see interrupt -> resume -> resync.
* **local** (``--broker host:port``): a local MQTT broker running ``scripts/fake_restore_device.py``.
  No Hatch logins. Stop/start the broker to exercise the grace period and the watchdog rebuild.

Optional one-shot commands run a few seconds after start: ``--start-routine``, ``--advance-step``,
``--stop``, ``--light on|off|<pct>``, ``--sound on|off|<pct>``.

Exit code 0 when the soak finished with no exceptions and (cloud mode, soak > margin) at least one
credential refresh happened.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import timedelta
import getpass
import json
import logging
import os
from pathlib import Path
import sys
import time

from aiohttp import ClientSession
from awscrt import mqtt

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "hatch_restore_light"))

from hatch_cloud import ConnectionParams, HatchCloudSession, LegacyRestoreDevice  # noqa: E402
from hatch_cloud.const import (  # noqa: E402
    MQTT_KEEP_ALIVE_S,
    MQTT_PING_TIMEOUT_MS,
    OP_TIMEOUT_S,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--email", default=os.getenv("HATCH_EMAIL"))
    parser.add_argument("--password", default=os.getenv("HATCH_PASSWORD"))
    parser.add_argument("--broker", default=None, help="host:port of a local broker (local mode)")
    parser.add_argument(
        "--thing-name", default=None, help="thingName to drive (default: first legacy restore)"
    )
    parser.add_argument("--soak-minutes", type=float, default=5)
    parser.add_argument("--refresh-margin-minutes", type=float, default=5)
    parser.add_argument("--print-every", type=float, default=30, help="seconds between heartbeat lines")
    parser.add_argument(
        "--command-delay", type=float, default=5, help="seconds after start to run one-shot commands"
    )
    parser.add_argument("--start-routine", action="store_true")
    parser.add_argument("--advance-step", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--light", default=None, help="on | off | brightness percent")
    parser.add_argument("--sound", default=None, help="on | off | volume percent")
    parser.add_argument(
        "--rebuild-after", type=float, default=0, help="force a connection rebuild after N seconds"
    )
    parser.add_argument("--grace-seconds", type=float, default=None, help="override UNAVAILABLE_GRACE_S")
    parser.add_argument("--watchdog-seconds", type=float, default=None, help="override WATCHDOG_REBUILD_S")
    parser.add_argument(
        "--no-routine-preserving", action="store_true", help="always use remote mode for light/sound writes"
    )
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if args.broker is None:
        if not args.email:
            parser.error("--email (or HATCH_EMAIL) is required in cloud mode")
        if not args.password:
            args.password = getpass.getpass("Hatch password: ")
    return args


def _local_factory(broker: str):
    host, _, port = broker.partition(":")

    def factory(params: ConnectionParams) -> mqtt.Connection:
        return mqtt.Connection(
            client=mqtt.Client(params.client_bootstrap),
            host_name=host or "localhost",
            port=int(port or 1883),
            client_id=params.client_id,
            clean_session=True,
            keep_alive_secs=MQTT_KEEP_ALIVE_S,
            ping_timeout_ms=MQTT_PING_TIMEOUT_MS,
            protocol_operation_timeout_ms=OP_TIMEOUT_S * 1000,
            reconnect_min_timeout_secs=1,
            reconnect_max_timeout_secs=10,
            on_connection_interrupted=params.on_connection_interrupted,
            on_connection_resumed=params.on_connection_resumed,
            on_connection_success=params.on_connection_success,
            on_connection_failure=params.on_connection_failure,
            disable_metrics=True,  # awscrt otherwise sends an SDK-metrics username local brokers reject
        )

    return factory


class Soak:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.started = time.monotonic()
        self.updates = 0
        self.health_events: list[tuple[float, bool, str]] = []
        self.errors: list[str] = []

    def log(self, message: str) -> None:
        print(f"{time.strftime('%H:%M:%S')} +{time.monotonic() - self.started:7.1f}s {message}", flush=True)

    def on_device_update(self, thing_name: str) -> None:
        self.updates += 1
        device = self.session.device_by_thing_name(thing_name)
        if isinstance(device, LegacyRestoreDevice):
            self.log(
                f"[state] {thing_name} playing={device.current_playing} step={device.routine_step} "
                f"light={'on' if device.is_light_active else 'off'}({device.light_brightness_percent}%) "
                f"sound={'on' if device.is_sound_active else 'off'}({device.sound_volume_percent}%) "
                f"online={device.is_online} v{device.document_version}"
            )
        else:
            self.log(f"[state] {thing_name}: {json.dumps(self.session.snapshots()[thing_name], default=str)}")

    def on_health_changed(self, healthy: bool, reason: str) -> None:
        self.health_events.append((time.monotonic() - self.started, healthy, reason))
        self.log(f"[health] {'HEALTHY' if healthy else 'UNHEALTHY'}: {reason}")

    def on_auth_failed(self) -> None:
        self.errors.append("auth failed")
        self.log("[auth] Hatch rejected credentials; a real HA install would start re-auth now")

    async def run(self) -> int:
        loop = asyncio.get_running_loop()
        async with ClientSession() as http:
            kwargs = {}
            if self.args.broker:
                thing = self.args.thing_name or "fake-restore"
                kwargs["connection_factory"] = _local_factory(self.args.broker)
                kwargs["device_infos"] = [
                    {
                        "name": "Fake Restore",
                        "product": "restore",
                        "thingName": thing,
                        "macAddress": "00:11:22:33:44:55",
                    }
                ]
            self.session = HatchCloudSession(
                loop=loop,
                client_session=http,
                email=self.args.email or "local@example.com",
                password=self.args.password or "",
                on_device_update=self.on_device_update,
                on_health_changed=self.on_health_changed,
                on_auth_failed=self.on_auth_failed,
                credential_refresh_margin=timedelta(minutes=self.args.refresh_margin_minutes),
                client_id_prefix="hatch_session_soak",
                **kwargs,
            )
            if self.args.grace_seconds is not None:
                self.session._unavailable_grace_s = self.args.grace_seconds
            if self.args.watchdog_seconds is not None:
                self.session._watchdog_rebuild_s = self.args.watchdog_seconds
            self.log("[soak] starting session")
            await self.session.async_start()
            for info in self.session.device_infos:
                self.log(
                    f"[soak] device: {info.get('name')} product={info.get('product')} thing={info.get('thingName')}"
                )
            if self.session.credentials_expiry:
                self.log(f"[soak] credentials expire at {self.session.credentials_expiry.isoformat()}")

            device = self._pick_device()
            if device is not None and self.args.no_routine_preserving:
                device.routine_preserving_writes = False
            commands = loop.create_task(self._run_commands(device))
            deadline = self.started + self.args.soak_minutes * 60
            last_heartbeat = time.monotonic()
            try:
                while time.monotonic() < deadline:
                    await asyncio.sleep(1)
                    if time.monotonic() - last_heartbeat >= self.args.print_every:
                        last_heartbeat = time.monotonic()
                        self.log(
                            f"[soak] healthy={self.session.healthy} ({self.session.health_reason}) "
                            f"updates={self.updates} cred_refreshes={self.session.credential_refreshes}"
                        )
            finally:
                commands.cancel()
                await self.session.async_stop()
                self.log("[soak] session stopped")
        return self._verdict()

    def _pick_device(self) -> LegacyRestoreDevice | None:
        devices = self.session.devices
        if self.args.thing_name:
            device = self.session.device_by_thing_name(self.args.thing_name)
        else:
            device = next(
                (d for d in devices if isinstance(d, LegacyRestoreDevice)), devices[0] if devices else None
            )
        if device is None:
            self.errors.append("no device matched")
        return device

    async def _run_commands(self, device: LegacyRestoreDevice | None) -> None:
        loop = asyncio.get_running_loop()
        args = self.args
        if device is None:
            return
        wanted = any(
            [args.start_routine, args.advance_step, args.stop, args.light, args.sound, args.rebuild_after]
        )
        if not wanted:
            return
        await asyncio.sleep(args.command_delay)

        async def run(label: str, func, *fargs) -> None:
            self.log(f"[cmd] {label}")
            try:
                await loop.run_in_executor(None, func, *fargs)
            except Exception as err:  # noqa: BLE001
                self.errors.append(f"{label}: {err}")
                self.log(f"[cmd] {label} FAILED: {err}")

        if args.start_routine:
            await run("start_routine(1)", device.start_routine, 1)
            await asyncio.sleep(3)
        if args.light is not None:
            if args.light == "on":
                await run("set_light(True)", device.set_light, True)
            elif args.light == "off":
                await run("set_light(False)", device.set_light, False)
            else:
                await run(f"set_light(True, {args.light}%)", device.set_light, True, float(args.light))
            await asyncio.sleep(3)
        if args.sound is not None:
            if args.sound == "on":
                await run("set_sound(True)", device.set_sound, True)
            elif args.sound == "off":
                await run("set_sound(False)", device.set_sound, False)
            else:
                await run(f"set_sound(True, {args.sound}%)", device.set_sound, True, float(args.sound))
            await asyncio.sleep(3)
        if args.advance_step:
            await run("advance_routine()", device.advance_routine)
            await asyncio.sleep(3)
        if args.stop:
            await run("stop()", device.stop)
        if args.rebuild_after:
            await asyncio.sleep(max(0.0, args.rebuild_after - args.command_delay))
            self.log("[cmd] forcing connection rebuild")
            try:
                await self.session.async_rebuild()
            except Exception as err:  # noqa: BLE001
                self.errors.append(f"rebuild: {err}")
                self.log(f"[cmd] rebuild FAILED: {err}")

    def _verdict(self) -> int:
        self.log(
            f"[soak] done: updates={self.updates} health_events={len(self.health_events)} "
            f"cred_refreshes={self.session.credential_refreshes} errors={self.errors}"
        )
        failed = bool(self.errors)
        if (
            not self.args.broker
            and self.args.soak_minutes > self.args.refresh_margin_minutes
            and self.session.credential_refreshes < 2
        ):
            # The first refresh happens at start; a soak longer than the margin must see another.
            self.log("[soak] FAIL: expected a credential refresh during the soak")
            failed = True
        if self.updates == 0:
            self.log("[soak] FAIL: no device state ever arrived")
            failed = True
        if not failed:
            self.log("[soak] PASS")
        return 1 if failed else 0


def main() -> int:
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.debug:
        logging.getLogger("hatch_rest_api").setLevel(logging.WARNING)
    return asyncio.run(Soak(args).run())


if __name__ == "__main__":
    sys.exit(main())
