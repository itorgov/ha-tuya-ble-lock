"""Local Tuya BLE locks connected through a Wi-Fi gateway."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er

from .const import CONF_GATEWAY_ID, CONF_LOCAL_KEY, CONF_LOCKS, CONF_VERSION, DOMAIN
from .coordinator import GatewayCoordinator
from .local import GatewayClient, LockConfig

PLATFORMS = [Platform.LOCK, Platform.SENSOR]
type TuyaBLELockConfigEntry = ConfigEntry[GatewayCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: TuyaBLELockConfigEntry) -> bool:
    """Connect locally and set up entities for the configured locks."""
    client = GatewayClient(
        entry.data[CONF_HOST],
        entry.data[CONF_GATEWAY_ID],
        entry.data[CONF_LOCAL_KEY],
        float(entry.data[CONF_VERSION]),
    )
    coordinator = GatewayCoordinator(
        hass, entry, client, [LockConfig(**lock) for lock in entry.data[CONF_LOCKS]]
    )
    try:
        await coordinator.async_config_entry_first_refresh()
    except Exception:
        await coordinator.async_close()
        raise
    entry.runtime_data = coordinator
    gateway_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data[CONF_GATEWAY_ID])},
        name=entry.title,
        manufacturer="Tuya",
        model="Bluetooth gateway",
    )
    coordinator.gateway_device_id = gateway_device.id
    _remove_stale_devices(hass, entry)
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except Exception:
        await coordinator.async_close()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: TuyaBLELockConfigEntry) -> bool:
    """Unload entities and release gateway resources."""
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_close()
    return unloaded


def _remove_stale_devices(hass: HomeAssistant, entry: TuyaBLELockConfigEntry) -> None:
    """Remove only this integration's registry records for removed children."""
    valid_ids = {lock.device_id for lock in entry.runtime_data.locks}
    entity_registry = er.async_get(hass)
    for entity in er.async_entries_for_config_entry(entity_registry, entry.entry_id):
        if entity.unique_id not in valid_ids | {f"{device_id}_battery" for device_id in valid_ids}:
            entity_registry.async_remove(entity.entity_id)
    device_registry = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(device_registry, entry.entry_id):
        ids = {identifier for domain, identifier in device.identifiers if domain == DOMAIN}
        if ids and not ids.intersection(valid_ids | {entry.data[CONF_GATEWAY_ID]}):
            device_registry.async_remove_device(device.id)
