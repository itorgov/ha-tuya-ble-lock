"""Expose the standard Home Assistant unlock action."""

from typing import Any

from homeassistant.components.lock import LockEntity
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TuyaBLELockConfigEntry
from .const import DOMAIN
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
    """An unlock-only lock with deliberately unknown physical state."""

    _attr_name = None
    _attr_is_locked = None
    _attr_supported_features = 0

    def __init__(self, coordinator: GatewayCoordinator, lock: LockConfig) -> None:
        """Initialize the entity."""
        super().__init__(coordinator, lock)
        self._attr_unique_id = lock.device_id

    async def async_unlock(self, **kwargs: Any) -> None:
        """Use an existing code; never register a new key or retry a write."""
        await self._async_unlock(kwargs.get("code"))
        # A later matching DP61 report is not proof of physical movement.
        # Leave state unknown, including for locks that automatically relock.

    async def async_lock(self, **kwargs: Any) -> None:
        """Explain why locking is unavailable instead of inventing a DP write."""
        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="locking_unsupported"
        )
