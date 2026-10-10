"""Common lock device information."""

from dataclasses import replace

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import GatewayCoordinator
from .local import LocalError, LockConfig, LockStatus


class TuyaLockEntity(CoordinatorEntity[GatewayCoordinator]):
    """Represent an individual Bluetooth child of the gateway."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: GatewayCoordinator, lock: LockConfig) -> None:
        """Associate an entity with a stable device ID."""
        super().__init__(coordinator)
        self.lock_config = lock
        assert coordinator.config_entry is not None
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, lock.device_id)},
            name=lock.name,
            manufacturer="Tuya",
            model="BLE lock (DP61)",
            via_device_id=coordinator.gateway_device_id,
        )

    @property
    def lock_status(self) -> LockStatus | None:
        """Return the latest exact-node observation."""
        return self.coordinator.data.get(self.lock_config.node_id)

    @property
    def available(self) -> bool:
        """Distinguish child reachability from gateway reachability."""
        return super().available and bool(self.lock_status and self.lock_status.online)

    async def _async_unlock(self, code: str | None = None) -> None:
        """Send one unlock command through the shared gateway queue."""
        lock = self.lock_config
        if code is not None:
            lock = replace(lock, remote_unlock_code=code)
        if not lock.remote_unlock_code:
            raise ServiceValidationError(translation_domain=DOMAIN, translation_key="missing_code")
        try:
            await self.coordinator.async_unlock(lock)
        except LocalError:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="unlock_failed"
            ) from None
