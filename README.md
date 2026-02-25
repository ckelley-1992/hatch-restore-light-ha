# Hatch Restore Light - Home Assistant Custom Integration

This repository contains a focused Home Assistant custom integration that exposes the **light** controls for Hatch Restore devices.

Note: this integration is configured to use Hatch's `prod-sleep.hatchbaby.com` API host (the same host used by `homebridge-hatch-baby-rest`).

## What it does

- Logs in to your Hatch account with email/password.
- Discovers Hatch devices using a Homebridge-style `iotProducts` query (supports legacy `product=restore`).
- Creates `light` entities for Restore models.
- For legacy `product=restore`:
  - `light` entity toggles light independently (`color.enabled`) using `content.playing=remote`.
  - `switch` entity toggles sound independently (`sound.enabled`) using `content.playing=remote`.
- For IoT Restore models (`restoreIot`/`restoreV4`/`restoreV5`): supports on/off, brightness, and RGBW color.

## Install

### Option A: HACS (Custom Repository)

1. Push this project to your own GitHub repo.
2. In Home Assistant, open **HACS -> Integrations -> 3-dot menu -> Custom repositories**.
3. Add your GitHub repo URL and choose category **Integration**.
4. Search for **Hatch Restore Light** in HACS and install.
5. Restart Home Assistant.
6. Add integration from **Settings -> Devices & Services -> Add Integration**.

### Option B: Manual copy

1. Copy `custom_components/hatch_restore_light` into your Home Assistant config directory:
   - `/config/custom_components/hatch_restore_light`
2. Restart Home Assistant.
3. Go to **Settings -> Devices & Services -> Add Integration**.
4. Search for **Hatch Restore Light**.
5. Enter your Hatch account credentials.

## Expose to Apple Home

If you use Home Assistant HomeKit Bridge:

1. Go to **Settings -> Devices & Services -> HomeKit Bridge**.
2. Add/include the created Hatch light entity.
3. Pair/sync the bridge to Apple Home.

The light should then be controllable directly in Apple Home.
