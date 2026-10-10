"""Credential-free diagnostics using an allowlist of aggregate values."""

from homeassistant.core import HomeAssistant

from . import TuyaBLELockConfigEntry
from .const import CONF_VERSION, DEFAULT_SCAN_INTERVAL


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuyaBLELockConfigEntry
) -> dict:
    """Exclude names, addresses, IDs, codes, keys and raw datapoints."""
    coordinator = entry.runtime_data
    return {
        "protocol_version": entry.data[CONF_VERSION],
        "scan_interval": entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL),
        "configured_locks": len(coordinator.locks),
        "gateway_available": coordinator.last_update_success,
        "online_locks": sum(status.online for status in (coordinator.data or {}).values()),
        "locks_with_battery_report": sum(
            status.battery is not None for status in (coordinator.data or {}).values()
        ),
    }
