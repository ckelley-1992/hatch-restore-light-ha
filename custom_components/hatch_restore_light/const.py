"""Constants for the Hatch Restore Light integration."""

from homeassistant.const import Platform

from .hatch_cloud.const import PREFERRED_API_BASE  # noqa: F401 - re-exported for config_flow

DOMAIN = "hatch_restore_light"
PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.FAN,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]
CONFIG_FLOW_VERSION = 1

# Mark entities unavailable when the shadow says the device itself is offline. Set to False if the
# legacy ``connected`` flag turns out to lag reality.
AVAILABILITY_REQUIRES_DEVICE_CONNECTED = True
