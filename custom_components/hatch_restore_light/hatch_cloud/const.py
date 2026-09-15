"""Tunables for the Hatch cloud session and device models."""

from datetime import timedelta

PREFERRED_API_BASE = "https://prod-sleep.hatchbaby.com/"

# Products to ask for in the Homebridge-style iotDevice query. The upstream library omits the
# legacy ``restore`` product, which is the whole reason this integration exists.
KNOWN_IOT_PRODUCTS = [
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

# Refresh Cognito credentials this long before they expire so awscrt's reconnects always have
# fresh material to sign the websocket handshake with.
CREDENTIAL_REFRESH_MARGIN = timedelta(minutes=5)

CONNECT_TIMEOUT_S = 30
# QoS1 operations (subscribe / publish / get) give up after this long instead of hanging forever.
OP_TIMEOUT_S = 10
# How long an MQTT interruption may last before entities are marked unavailable.
UNAVAILABLE_GRACE_S = 90
# How long an MQTT interruption may last before the session gives up on awscrt's reconnect and
# builds a brand-new connection (make-before-break).
WATCHDOG_REBUILD_S = 300
REBUILD_BACKOFF_MIN_S = 30
REBUILD_BACKOFF_MAX_S = 600

MQTT_KEEP_ALIVE_S = 30
MQTT_PING_TIMEOUT_MS = 5000
MQTT_RECONNECT_MIN_S = 2
MQTT_RECONNECT_MAX_S = 60

# Legacy Restore reports residual near-zero intensity/volume with ``enabled: true``; anything at or
# below this raw value (~1% of 65535) is treated as off.
ACTIVE_FLOOR_RAW = 700
RAW_MAX = 65535
DEFAULT_COLOR_ID = 229
DEFAULT_SOUND_ID = 10040

# While a routine is playing, write only the touched ``color``/``sound`` sub-object and leave
# ``content`` alone so the routine (and its device-side timer) keeps running. The newer Restore
# models honour this; whether Gen 1 firmware does is settled by the experiment in
# scripts/hatch_restore_shadow_probe.py. Set to False to always drop into "remote" mode instead.
ROUTINE_PRESERVING_WRITES = True
# If Gen 1 ignores a ``step`` change while already in a routine, send ``playing: none`` first.
ROUTINE_STEP_TWO_PHASE = False
ROUTINE_STEP_TWO_PHASE_DELAY_S = 1.0
