"""Provide an explicit unlock control independent of the unknown lock state."""

from homeassistant.components.button import ButtonEntity
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TuyaBLELockConfigEntry
from .coordinator import GatewayCoordinator
from .entity import TuyaLockEntity
from .local import LockConfig

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: TuyaBLELockConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a dedicated unlock button for each configured lock."""
    async_add_entities(
        TuyaBLEUnlockButton(entry.runtime_data, lock) for lock in entry.runtime_data.locks
    )


class TuyaBLEUnlockButton(TuyaLockEntity, ButtonEntity):
    """Send an unlock command without relying on a lock/unlock toggle."""

    _attr_translation_key = "unlock"

    def __init__(self, coordinator: GatewayCoordinator, lock: LockConfig) -> None:
        """Keep the button on the same device as its lock entity."""
        super().__init__(coordinator, lock)
        self._attr_unique_id = f"{lock.device_id}_unlock"

    async def async_press(self) -> None:
        """Use the saved remote unlock code without retrying the command."""
        await self._async_unlock()
