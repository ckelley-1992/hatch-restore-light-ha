#!/usr/bin/env python3
"""Emulate a Hatch Restore Gen 1 on a local MQTT broker (no cloud, no Hatch logins).

Speaks the AWS IoT *classic shadow* topic contract for one thing so the integration's
``hatch_cloud`` session can be exercised offline::

    $aws/things/<thing>/shadow/get            -> get/accepted   (full reported document)
    $aws/things/<thing>/shadow/update         -> update/accepted (desired echo, then reported delta)

Gen 1 rules: ``content.playing`` in {none, remote, routine}; routine step 1 = light on,
step 2 = sound on + light off; ``playing: none`` = everything off. With ``--strict-routine`` a bare
``color``/``sound`` write while a routine plays is *not* applied (simulates firmware that ignores it).

Interactive keys (stdin): ``t`` tap (advance step), ``e`` end sound, ``d`` drop connection for 5 s,
``s`` print state, ``q`` quit. ``--auto-tap-after`` / ``--step2-seconds`` do the same on a timer.

Example (broker via ``python -m amqtt`` or mosquitto on localhost:1883)::

    python scripts/fake_restore_device.py --thing-name fake-restore
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import sys
import threading
import time
from typing import Any

from awscrt import io, mqtt

WARM_WHITE_ID = 229
PINK_NOISE_ID = 10040
RAW_MAX = 65535


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--broker", default="localhost:1883", help="host:port of the local MQTT broker")
    parser.add_argument("--thing-name", default="fake-restore")
    parser.add_argument(
        "--strict-routine", action="store_true", help="ignore bare color/sound writes during a routine"
    )
    parser.add_argument(
        "--reject-step-change", action="store_true", help="ignore step changes while already in a routine"
    )
    parser.add_argument(
        "--auto-tap-after",
        type=float,
        default=0,
        help="seconds after step 1 starts to auto-advance to step 2 (0 = never)",
    )
    parser.add_argument(
        "--step2-seconds",
        type=float,
        default=0,
        help="how long step 2 sound plays before the routine ends (0 = until 'e')",
    )
    parser.add_argument(
        "--report-delay", type=float, default=0.3, help="seconds between desired echo and reported update"
    )
    parser.add_argument("--quiet", action="store_true")
    return parser.parse_args()


class FakeRestore:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.thing = args.thing_name
        self.version = 1
        self.state: dict[str, Any] = {
            "deviceInfo": {"f": "fake-1.0.0", "hw": "fake"},
            "connected": True,
            "content": {"playing": "none", "paused": False, "offset": 0, "step": 0},
            "color": {"enabled": False, "id": WARM_WHITE_ID, "i": 0},
            "sound": {"enabled": False, "id": PINK_NOISE_ID, "v": 0},
        }
        self.preferred_color_i = int(RAW_MAX * 0.6)
        self.preferred_sound_v = int(RAW_MAX * 0.45)
        self.conn: mqtt.Connection | None = None
        self.loop = asyncio.new_event_loop()
        self._timers: list[asyncio.TimerHandle] = []

    # ------------------------------------------------------------------ mqtt

    def topic(self, suffix: str) -> str:
        return f"$aws/things/{self.thing}/shadow/{suffix}"

    def connect(self) -> None:
        host, _, port = self.args.broker.partition(":")
        elg = io.EventLoopGroup(1)
        bootstrap = io.ClientBootstrap(elg, io.DefaultHostResolver(elg))
        self._elg, self._bootstrap = elg, bootstrap
        self.conn = mqtt.Connection(
            client=mqtt.Client(bootstrap),
            host_name=host or "localhost",
            port=int(port or 1883),
            client_id=f"fake-device/{self.thing}",
            clean_session=True,
            keep_alive_secs=30,
            reconnect_min_timeout_secs=1,
            reconnect_max_timeout_secs=5,
            on_connection_interrupted=lambda connection, error, **kw: self.log(
                f"[device] mqtt interrupted: {error}"
            ),
            on_connection_resumed=self._on_resumed,
            disable_metrics=True,  # awscrt otherwise sends an SDK-metrics username local brokers reject
        )
        self.conn.connect().result(timeout=10)
        self._subscribe(self.conn)
        self.log(f"[device] connected to {self.args.broker} as thing '{self.thing}'")

    def _on_resumed(
        self, connection: mqtt.Connection, return_code: Any, session_present: bool, **kw: Any
    ) -> None:
        self.log(f"[device] mqtt resumed session_present={session_present}")
        if not session_present:
            # Like real firmware, re-establish subscriptions the broker forgot.
            threading.Thread(target=self._subscribe, args=(connection,), daemon=True).start()

    def _subscribe(self, conn: mqtt.Connection) -> None:
        for suffix, handler in (("get", self.on_get), ("update", self.on_update)):
            fut, _ = conn.subscribe(self.topic(suffix), mqtt.QoS.AT_LEAST_ONCE, handler)
            fut.result(timeout=10)

    def publish(self, suffix: str, payload: dict[str, Any]) -> None:
        if self.conn is None:
            return
        self.conn.publish(self.topic(suffix), json.dumps(payload), mqtt.QoS.AT_LEAST_ONCE)

    def log(self, message: str) -> None:
        if not self.args.quiet:
            print(f"{time.strftime('%H:%M:%S')} {message}", flush=True)

    # ------------------------------------------------------------------ shadow handlers (CRT thread)

    def on_get(self, topic: str, payload: bytes, **kwargs: Any) -> None:
        self.loop.call_soon_threadsafe(self._handle_get, payload)

    def on_update(self, topic: str, payload: bytes, **kwargs: Any) -> None:
        self.loop.call_soon_threadsafe(self._handle_update, payload)

    def _handle_get(self, payload: bytes) -> None:
        self.log("[device] get -> get/accepted")
        self.publish(
            "get/accepted",
            {
                "state": {"reported": copy.deepcopy(self.state)},
                "metadata": {},
                "version": self.version,
                "timestamp": int(time.time()),
            },
        )

    def _handle_update(self, payload: bytes) -> None:
        try:
            request = json.loads(payload)
        except json.JSONDecodeError:
            self.publish("update/rejected", {"code": 400, "message": "invalid json"})
            return
        desired = (request.get("state") or {}).get("desired")
        if not isinstance(desired, dict):
            self.publish("update/rejected", {"code": 400, "message": "missing state.desired"})
            return
        self.log(f"[device] desired: {json.dumps(desired)}")
        # 1. AWS echoes the accepted desired document.
        self.version += 1
        self.publish(
            "update/accepted",
            {
                "state": {"desired": desired},
                "metadata": {},
                "version": self.version,
                "timestamp": int(time.time()),
            },
        )
        # 2. The device applies it and reports.
        delta = self._apply_desired(desired)
        if delta:
            self._schedule(self.args.report_delay, self._report, delta)

    # ------------------------------------------------------------------ Gen 1 behaviour

    def _apply_desired(self, desired: dict[str, Any]) -> dict[str, Any]:
        content = desired.get("content")
        color = desired.get("color")
        sound = desired.get("sound")
        playing = self.state["content"]["playing"]
        delta: dict[str, Any] = {}

        if isinstance(content, dict):
            new_playing = content.get("playing", playing)
            step = int(content.get("step", 0) or 0)
            if new_playing == "routine" and playing == "routine" and self.args.reject_step_change:
                self.log("[device] (reject-step-change) ignoring step change while in routine")
                return {}
            self._cancel_timers()
            if new_playing == "none":
                delta.update(self._set_all_off())
            elif new_playing == "routine":
                delta.update(self._enter_routine_step(step))
            elif new_playing == "remote":
                self.state["content"] = {"playing": "remote", "paused": False, "offset": 0, "step": 0}
                delta["content"] = dict(self.state["content"])
                if isinstance(color, dict):
                    delta["color"] = self._merge("color", color)
                if isinstance(sound, dict):
                    delta["sound"] = self._merge("sound", sound)
            return delta

        # Bare colour / sound writes (no content).
        if playing == "routine" and self.args.strict_routine:
            self.log("[device] (strict-routine) ignoring bare color/sound write during routine")
            return {}
        if isinstance(color, dict):
            delta["color"] = self._merge("color", color)
        if isinstance(sound, dict):
            delta["sound"] = self._merge("sound", sound)
        return delta

    def _merge(self, key: str, values: dict[str, Any]) -> dict[str, Any]:
        self.state[key].update(values)
        if key == "color" and self.state["color"]["enabled"] and self.state["color"]["i"] > 700:
            self.preferred_color_i = self.state["color"]["i"]
        if key == "sound" and self.state["sound"]["enabled"] and self.state["sound"]["v"] > 700:
            self.preferred_sound_v = self.state["sound"]["v"]
        if not self.state[key]["enabled"]:
            # Gen 1 reports residual zero level while disabled.
            self.state[key]["i" if key == "color" else "v"] = 0
        return dict(self.state[key])

    def _set_all_off(self) -> dict[str, Any]:
        self.state["content"] = {"playing": "none", "paused": False, "offset": 0, "step": 0}
        self.state["color"].update({"enabled": False, "i": 0})
        self.state["sound"].update({"enabled": False, "v": 0})
        return {k: dict(self.state[k]) for k in ("content", "color", "sound")}

    def _enter_routine_step(self, step: int) -> dict[str, Any]:
        if step <= 1:
            self.state["content"] = {"playing": "routine", "paused": False, "offset": 0, "step": 1}
            self.state["color"].update({"enabled": True, "id": WARM_WHITE_ID, "i": self.preferred_color_i})
            self.state["sound"].update({"enabled": False, "v": 0})
            if self.args.auto_tap_after > 0:
                self._schedule(self.args.auto_tap_after, self.tap)
        elif step == 2:
            self.state["content"] = {"playing": "routine", "paused": False, "offset": 0, "step": 2}
            self.state["color"].update({"enabled": False, "i": 0})
            self.state["sound"].update({"enabled": True, "id": PINK_NOISE_ID, "v": self.preferred_sound_v})
            if self.args.step2_seconds > 0:
                self._schedule(self.args.step2_seconds, self.end_sound)
        else:
            return self._set_all_off()
        return {k: dict(self.state[k]) for k in ("content", "color", "sound")}

    def _report(self, delta: dict[str, Any]) -> None:
        self.version += 1
        self.log(f"[device] reported v{self.version}: {json.dumps(delta)}")
        self.publish(
            "update/accepted",
            {
                "state": {"reported": delta},
                "metadata": {},
                "version": self.version,
                "timestamp": int(time.time()),
            },
        )

    # ------------------------------------------------------------------ user / timer actions (loop thread)

    def tap(self) -> None:
        content = self.state["content"]
        if content["playing"] != "routine":
            self.log("[device] tap: not in a routine -> starting routine step 1")
            delta = self._enter_routine_step(1)
        else:
            self.log(f"[device] tap: advancing from step {content['step']}")
            self._cancel_timers()
            delta = self._enter_routine_step(content["step"] + 1)
        self._report(delta)

    def end_sound(self) -> None:
        """Real Gen 1 behaviour: step returns to 0 but content.playing stays "routine"."""
        self.log("[device] step 2 sound finished -> step 0, still reporting playing=routine")
        self._cancel_timers()
        self.state["content"]["step"] = 0
        self.state["sound"].update({"enabled": False, "v": 0})
        self._report({"content": dict(self.state["content"]), "sound": dict(self.state["sound"])})

    def drop_connection(self, seconds: float = 5.0) -> None:
        if self.conn is None:
            return
        self.log(f"[device] dropping connection for {seconds}s")
        self.state["connected"] = False
        self._report({"connected": False})
        conn = self.conn
        self.conn = None
        conn.disconnect().result(timeout=10)

        def _reconnect() -> None:
            conn.connect().result(timeout=10)
            self._subscribe(conn)
            self.conn = conn
            self.state["connected"] = True
            self.loop.call_soon_threadsafe(self._report, {"connected": True})

        self._schedule(seconds, lambda: threading.Thread(target=_reconnect, daemon=True).start())

    def _schedule(self, delay: float, callback, *args: Any) -> None:
        self._timers.append(self.loop.call_later(delay, callback, *args))

    def _cancel_timers(self) -> None:
        for handle in self._timers:
            handle.cancel()
        self._timers.clear()

    # ------------------------------------------------------------------ main

    def run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.connect()
        print("keys: t=tap  e=end sound  d=drop connection  s=state  q=quit", flush=True)
        threading.Thread(target=self._stdin_thread, daemon=True).start()
        try:
            self.loop.run_forever()
        finally:
            if self.conn is not None:
                self.conn.disconnect().result(timeout=5)

    def _stdin_thread(self) -> None:
        for line in sys.stdin:
            key = line.strip().lower()[:1]
            if key == "t":
                self.loop.call_soon_threadsafe(self.tap)
            elif key == "e":
                self.loop.call_soon_threadsafe(self.end_sound)
            elif key == "d":
                self.loop.call_soon_threadsafe(self.drop_connection)
            elif key == "s":
                self.loop.call_soon_threadsafe(lambda: self.log(f"[device] state: {json.dumps(self.state)}"))
            elif key == "q":
                self.loop.call_soon_threadsafe(self.loop.stop)
                return


if __name__ == "__main__":
    FakeRestore(_parse_args()).run()
