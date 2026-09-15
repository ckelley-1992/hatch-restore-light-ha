# Hatch Restore Light — Home Assistant integration

Controls a **Hatch Restore Gen 1** (`product=restore`) — light, sound machine and the Hatch-app
bedtime routine — through Hatch's cloud, and exposes it in a shape that maps cleanly onto Apple Home
via HA's HomeKit Bridge. Newer Restore models (`restoreIot`/`restoreV4`/`restoreV5`) get an RGBW
light through the upstream `hatch_rest_api` models.

Uses Hatch's `prod-sleep.hatchbaby.com` API host (the one `homebridge-hatch-baby-rest` uses); the
upstream library alone does not discover Gen 1 devices.

## Entities (Gen 1)

| Entity | Apple Home | What it does |
| --- | --- | --- |
| `light.<name>_light` | Light (dimmer) | Light on/off + brightness. Inside a routine it changes the light without ending the routine. |
| `fan.<name>_sound` | Fan (speed slider) | Sound on/off + volume. A *fan* so "turn off the lights" / Good Night scenes leave the sound alone. Turning it on resumes the last sound that actually played (`last_active_sound_id` attribute); the device reports a default id while idle. |
| `switch.<name>_sleep_mode` | Switch | On = start the Hatch-app routine at step 1. Off = stop everything. Reads as off when the routine finishes: the device reports `step` 0 → 1 → 2 → 0 over a night while `playing` lingers as `routine`. |
| `button.<name>_next_routine_step` | Momentary switch | Same as tapping the device: advance the routine one step. |
| `sensor.<name>_playing` | — (diagnostic) | `none` / `remote` / `routine` |
| `sensor.<name>_routine_step` | — (diagnostic) | Current routine step. **Your automations trigger on 1 → 2 to notice the tap.** |
| `binary_sensor.<name>_connected` | — (diagnostic) | Device reports as online to Hatch. |
| `number.<name>_color_id` | — (disabled) | Raw colour id, for experiments. |

Everything becomes *unavailable* when the cloud connection is down for more than 90 s or the device
itself reports offline, so Apple Home shows "No Response" instead of silently doing nothing.

## How it stays connected

One long-lived MQTT connection to AWS IoT. Cognito credentials are refreshed in the background
5 minutes before they expire and handed to awscrt through a delegate, so its own reconnects are
always signed with fresh credentials. On resume the shadow subscriptions are re-established and a
fresh `get` closes any gap. If a disconnect lasts more than 5 minutes a new connection is built
before the old one is dropped. Login happens once per HA start (plus once if Hatch invalidates
the session) — not hourly — which keeps the account clear of Hatch's 429 rate limiting.

## Install

**HACS**: add this repo as a custom repository (category *Integration*), install, restart HA.
**Manual**: copy `custom_components/hatch_restore_light` into `<config>/custom_components/`, restart.

Then *Settings → Devices & Services → Add Integration → Hatch Restore Light* and sign in with your
Hatch account. If the password changes later, HA prompts for re-authentication.

Requires Home Assistant 2024.11 or newer.

## Apple Home

*Settings → Devices & Services → HomeKit Bridge → Configure*: include the **Light**, **Sound**
(fan) and **Sleep mode** (switch) entities — and **Next routine step** (button) if you want a
"go to sleep" tile. Leave the diagnostic sensors out. Siri: "set the bedroom sound to 40 %",
"turn on sleep mode".

### Upgrading from 0.1.x

The *Sound Level* light, *Sound* switch and *Sound Media* media player were removed (their registry
rows are purged on first start). Apple Home drops those accessories; anything that referenced them
(scenes, automations) needs to point at the new Sound fan. The Light and Sleep Mode accessories keep
their identities.

## The nightly routine

Hatch app: *Bedtime Routine* = step 1 **light until tapped**, step 2 **sound for N minutes**.
The device runs the steps itself; HA only needs to start it and watch. There is no "tap" event, but
the tap changes `content.step` in the shadow, which is what `sensor.<name>_routine_step` shows.

[`examples/hatch_restore_night.yaml`](examples/hatch_restore_night.yaml) is a complete set of
helpers + automations:

1. 22:15 → `switch.turn_on` sleep mode (or keep this trigger in Apple Home).
2. `routine_step` 1 → 2 (the tap, or the Next-step button) → start `timer.hatch_sound_off` for
   `input_number.hatch_sound_hours`.
3. Timer finished → `switch.turn_off` sleep mode. If the device ended the routine on its own first,
   the timer is cancelled.

## Validating against the real device (one evening)

Two firmware behaviours have not been confirmed on Gen 1 and are behind constants in
`custom_components/hatch_restore_light/hatch_cloud/const.py`:

* `ROUTINE_PRESERVING_WRITES` (default `True`): while a routine plays, light/sound changes are sent
  as bare `color`/`sound` updates so the routine keeps running. If the device ignores those, set it
  to `False` and writes drop into `remote` mode (which ends the routine) as in 0.1.x.
* `ROUTINE_STEP_TWO_PHASE` (default `False`): if the device ignores a `step` change while already in
  a routine, set to `True` to send `playing: none` first.

Run the probe (needs the device in reach; each run is one login, so leave a minute between runs):

```bash
export HATCH_EMAIL=you@example.com HATCH_PASSWORD=...
python scripts/hatch_restore_shadow_probe.py --set-content-playing routine --set-content-step 1 --watch-seconds 20
python scripts/hatch_restore_shadow_probe.py --set-color-enabled on --set-color-intensity 20 --watch-seconds 20
python scripts/hatch_restore_shadow_probe.py --set-content-playing routine --set-content-step 2 --watch-seconds 30
python scripts/hatch_restore_shadow_probe.py --watch-seconds 180   # now tap the device, watch what changes
```

* Run 2: `color.i` changes and `content.playing` stays `routine` → keep `ROUTINE_PRESERVING_WRITES`.
  `playing` flips, or nothing changes → set it `False`.
* Run 3: `step` 2 with light off / sound on → the Next-step button works. Ignored → try
  `ROUTINE_STEP_TWO_PHASE = True`; if that is also ignored, disable the button entity and rely on
  the physical tap.
* Run 4: watch the tap. (Confirmed on a real device: `content.step` goes 0 → 1 → 2 → 0 while
  `playing` stays `routine` after the sound ends; the Sleep mode switch keys off the step.)

Sound ids are opaque numbers (`sound_id` / `last_active_sound_id` attributes on the fan entity).
`--list-sounds` asks Hatch's content endpoint for a Gen 1 catalog — untested for this product:

```bash
python scripts/hatch_restore_shadow_probe.py --list-sounds
```

A longer soak against the cloud proves the credential refresh survives the 1 h expiry:

```bash
python scripts/hatch_session_soak.py --soak-minutes 75 --refresh-margin-minutes 55
```

## Testing without the cloud

The shadow protocol is plain MQTT, so a fake Gen 1 on a local **Mosquitto** (HA add-on, or
`brew install mosquitto`) exercises the connection logic without any Hatch logins:

```bash
python scripts/fake_restore_device.py --thing-name fake-restore --auto-tap-after 8 --step2-seconds 10
python scripts/hatch_session_soak.py --broker localhost:1883 --thing-name fake-restore --soak-minutes 1 --start-routine
```

Stop and restart Mosquitto during a soak to watch the grace period, the resume/resync, and (with
`--grace-seconds 10 --watchdog-seconds 20`) the watchdog rebuild. The fake's `--strict-routine`
flag emulates firmware that ignores bare writes during a routine.

Unit tests (HA test harness, no network):

```bash
uv venv -p 3.12 .venv && VIRTUAL_ENV=.venv uv pip install pytest-homeassistant-custom-component ruff
.venv/bin/python -m pytest
.venv/bin/ruff check . && .venv/bin/ruff format --check .
```

## Troubleshooting

```yaml
logger:
  logs:
    custom_components.hatch_restore_light: debug
```

Look for `Hatch MQTT connection interrupted` / `resumed`, `Hatch cloud session unhealthy`,
`Hatch MQTT connect attempt failed` (a stale-credential 403 shows here) and `Rate limited (429)`.
`homeassistant.update_entity` on any Hatch entity forces a shadow re-read.
