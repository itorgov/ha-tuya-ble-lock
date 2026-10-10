"""Battery readings from exact-child local status reports."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory
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
    """Create battery entities, which remain unknown without a fresh report."""
    async_add_entities(BatterySensor(entry.runtime_data, lock) for lock in entry.runtime_data.locks)


class BatterySensor(TuyaLockEntity, SensorEntity):
    """The supported profile's battery percentage DP8."""

    _attr_translation_key = "battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: GatewayCoordinator, lock: LockConfig) -> None:
        """Initialize the battery sensor."""
        super().__init__(coordinator, lock)
        self._attr_unique_id = f"{lock.device_id}_battery"

    @property
    def native_value(self) -> int | None:
        """Return the last battery report from the current connection."""
        return self.lock_status.battery if self.lock_status else None
