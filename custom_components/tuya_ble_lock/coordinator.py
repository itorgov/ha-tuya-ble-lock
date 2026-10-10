"""Serialize gateway I/O and share one poll across all entities."""

import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .local import GatewayClient, LocalError, LockConfig, LockStatus

_LOGGER = logging.getLogger(__name__)


class GatewayCoordinator(DataUpdateCoordinator[dict[str, LockStatus]]):
    """Coordinate one gateway; all blocking work runs in the executor."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: GatewayClient,
        locks: list[LockConfig],
    ) -> None:
        """Initialize gateway state."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(
                seconds=entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
            ),
            always_update=False,
        )
        self.client = client
        self.locks = locks
        self.gateway_device_id: str | None = None
        self._io_lock = asyncio.Lock()
        self._closed = False

    async def _async_update_data(self) -> dict[str, LockStatus]:
        """Fetch fresh online state without any cloud calls."""
        async with self._io_lock:
            if self._closed:
                raise UpdateFailed("Gateway has been unloaded")
            try:
                return await self._async_io(self.client.poll, self.locks)
            except LocalError:
                raise UpdateFailed("Cannot read the local gateway") from None

    async def async_unlock(self, lock: LockConfig) -> None:
        """Send a single command without retrying ambiguous outcomes."""
        async with self._io_lock:
            if self._closed:
                raise LocalError("Gateway has been unloaded")
            await self._async_io(self.client.unlock, lock)

    async def _async_io(self, job: Callable, *args: Any) -> Any:
        """Keep the serialization lock held if an executor caller is cancelled."""
        future = self.hass.async_add_executor_job(job, *args)
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            while not future.done():
                try:
                    await asyncio.shield(future)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if future.done() and not future.cancelled():
                future.exception()
            raise

    async def async_close(self) -> None:
        """Wait for any operation to finish before closing."""
        async with self._io_lock:
            self._closed = True
            await self.hass.async_add_executor_job(self.client.close)
