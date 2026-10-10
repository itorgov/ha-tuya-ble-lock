"""Expose the standard Home Assistant unlock action."""

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TuyaBLELockConfigEntry
from .const import DOMAIN, EVENT_TUYA_BLE_LOCK
from .coordinator import GatewayCoordinator
from .entity import TuyaLockEntity
from .local import LockConfig

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaBLELockConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create one lock entity per configured Bluetooth lock."""
    async_add_entities(TuyaBLELock(entry.runtime_data, lock) for lock in entry.runtime_data.locks)


class TuyaBLELock(TuyaLockEntity, LockEntity):
    """An unlock-only lock whose state follows reports from the gateway."""

    _attr_name = None
    _attr_supported_features = 0

    def __init__(self, coordinator: GatewayCoordinator, lock: LockConfig) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, lock)
        self._attr_unique_id = lock.device_id

    @property
    def is_locked(self) -> bool | None:
        """Return the reported state, unknown until a valid report arrives."""
        return self.lock_status.is_locked if self.lock_status else None

    async def async_unlock(self, **kwargs: Any) -> None:
        """Use an existing code; never register a new key or retry a write."""
        context = self._context
        await self._async_unlock(kwargs.get("code"))
        # Only state reports update is_locked; an acknowledgement is not state.
        event_data = {"entity_id": self.entity_id, "type": "unlock_acknowledged"}
        if self.device_entry is not None:
            event_data["device_id"] = self.device_entry.id
        self.hass.bus.async_fire(EVENT_TUYA_BLE_LOCK, event_data, context=context)

    async def async_lock(self, **kwargs: Any) -> None:
        """Explain why locking is unavailable instead of inventing a DP write."""
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="locking_unsupported"
        )
