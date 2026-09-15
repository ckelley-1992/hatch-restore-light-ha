"""Exceptions raised by the Hatch cloud session and device models."""


class HatchCloudError(Exception):
    """Base class for Hatch cloud failures."""


class HatchAuthError(HatchCloudError):
    """Hatch rejected the stored credentials; the user must re-authenticate."""


class HatchNotReadyError(HatchCloudError):
    """A command was issued while the MQTT connection was not usable."""


class NotInRoutineError(HatchCloudError):
    """A routine-step command was issued while no routine is playing."""
