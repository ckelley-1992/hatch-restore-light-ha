"""Constants for the Hatch Restore Light integration."""

from homeassistant.const import Platform

DOMAIN = "hatch_restore_light"
PLATFORMS: list[Platform] = [Platform.LIGHT, Platform.SWITCH, Platform.NUMBER]
CONFIG_FLOW_VERSION = 1
PREFERRED_API_BASE = "https://prod-sleep.hatchbaby.com/"
