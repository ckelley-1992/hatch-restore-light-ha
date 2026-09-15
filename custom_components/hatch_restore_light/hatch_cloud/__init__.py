"""Home-Assistant-free client for Hatch's cloud (REST + AWS IoT shadow).

Importable from the standalone scripts in ``scripts/`` without ``homeassistant`` installed.
"""

from .errors import HatchAuthError, HatchCloudError, HatchNotReadyError, NotInRoutineError
from .legacy_restore_device import LegacyRestoreDevice
from .session import ConnectionParams, HatchCloudSession

__all__ = [
    "ConnectionParams",
    "HatchAuthError",
    "HatchCloudError",
    "HatchCloudSession",
    "HatchNotReadyError",
    "LegacyRestoreDevice",
    "NotInRoutineError",
]
