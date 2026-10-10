"""Credential-free diagnostics with anonymous report counters."""

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant

from . import TuyaBLELockConfigEntry
from .const import CONF_LOCKS, CONF_VERSION, DEFAULT_SCAN_INTERVAL


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: TuyaBLELockConfigEntry
) -> dict:
    """Exclude names, addresses, device IDs, codes, keys and raw payloads."""
    diagnostics = {
        "setup_state": entry.state.value,
        "protocol_version": entry.data[CONF_VERSION],
        "scan_interval": entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL),
        "configured_locks": len(entry.data[CONF_LOCKS]),
        "gateway_available": False,
        "online_locks": 0,
        "locks_with_battery_report": 0,
        "locks_with_state_report": 0,
        "transport": None,
    }
    coordinator = getattr(entry, "runtime_data", None)
    if coordinator is None or entry.state is not ConfigEntryState.LOADED:
        return diagnostics
    diagnostics.update(
        {
            "gateway_available": coordinator.last_update_success,
            "online_locks": sum(status.online for status in (coordinator.data or {}).values()),
            "locks_with_battery_report": sum(
                status.battery is not None for status in (coordinator.data or {}).values()
            ),
            "locks_with_state_report": sum(
                status.is_locked is not None for status in (coordinator.data or {}).values()
            ),
            "transport": await coordinator.async_get_transport_diagnostics(),
        }
    )
    return diagnostics
