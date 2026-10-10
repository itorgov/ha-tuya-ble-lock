"""Serialize gateway I/O and publish each reported child-state transition."""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DEFAULT_SCAN_INTERVAL, DOMAIN
from .local import GatewayClient, LocalError, LockConfig, LockStatus, Topology

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
            always_update=False,
        )
        self.client = client
        self.locks = locks
        self.gateway_device_id: str | None = None
        self._io_lock = asyncio.Lock()
        self._closed = False
        self._close_complete = False
        self._refresh_interval = entry.options.get("scan_interval", DEFAULT_SCAN_INTERVAL)
        self._next_refresh = 0.0
        self._listen_task: asyncio.Task[None] | None = None
        self.client.set_update_callback(self._receive_update)

    def _receive_update(self, statuses: dict[str, LockStatus]) -> None:
        """Move a sanitized transport snapshot from the executor to HA."""
        self.hass.loop.call_soon_threadsafe(self._async_receive_update, statuses)

    @callback
    def _async_receive_update(self, statuses: dict[str, LockStatus]) -> None:
        """Publish each change so Recorder can see unlock and auto-relock."""
        if not self._closed and (not self.last_update_success or statuses != self.data):
            self.async_set_updated_data(statuses)

    @callback
    def async_start(self) -> None:
        """Start the receiver after entities have subscribed to updates."""
        if self._closed or self._listen_task is not None or not self.locks:
            return
        assert self.config_entry is not None
        self._listen_task = self.config_entry.async_create_background_task(
            self.hass, self._async_listen(), f"{DOMAIN} gateway reports"
        )

    async def _async_listen(self) -> None:
        """Receive push reports and reconnect using read-only refreshes."""
        while not self._closed:
            if not self.last_update_success:
                await asyncio.sleep(5)
                if not self._closed:
                    await self.async_refresh()
                continue
            # Pushes must not postpone periodic child-status requests.
            if self.hass.loop.time() >= self._next_refresh:
                await self.async_refresh()
                continue
            try:
                async with self._io_lock:
                    if self._closed:
                        return
                    await self._async_io(self.client.listen)
            except LocalError:
                if not self._closed:
                    self.async_set_update_error(UpdateFailed("Cannot read the local gateway"))
            # Yield fairly to polls and user actions, including busy gateways.
            await asyncio.sleep(0.05)

    async def _async_update_data(self) -> dict[str, LockStatus]:
        """Refresh child status on the gateway session without any cloud calls."""
        async with self._io_lock:
            if self._closed:
                raise UpdateFailed("Gateway has been unloaded")
            try:
                statuses = await self._async_io(self.client.poll, self.locks)
            except LocalError:
                raise UpdateFailed("Cannot read the local gateway") from None
            self._next_refresh = self.hass.loop.time() + self._refresh_interval
            return statuses

    async def async_unlock(self, lock: LockConfig) -> None:
        """Send a single command without retrying ambiguous outcomes."""
        async with self._io_lock:
            if self._closed:
                raise LocalError("Gateway has been unloaded")
            await self._async_io(self.client.unlock, lock)

    async def async_get_transport_diagnostics(self) -> dict[str, Any]:
        """Copy sanitized report counters without issuing gateway requests."""
        async with self._io_lock:
            return await self._async_io(self.client.diagnostics)

    async def async_probe(self, candidate: GatewayClient, *, reuse_connection: bool) -> Topology:
        """Validate reconfiguration without competing for the gateway's socket."""
        async with self._io_lock:
            if self._closed:
                raise LocalError("Gateway has been unloaded")
            if reuse_connection:
                try:
                    return await self._async_io(self.client.probe_live)
                except LocalError:
                    self.async_set_update_error(UpdateFailed("Cannot read the local gateway"))
                    raise
            # A gateway may accept only one local TCP connection. Pause the
            # receiver while testing changed credentials, then restore reads.
            await self._async_io(self.client.close)
            try:
                return await self._async_io(candidate.probe)
            finally:
                if not self._closed:
                    try:
                        statuses = await self._async_io(self.client.poll, self.locks)
                    except LocalError:
                        self.async_set_update_error(UpdateFailed("Cannot read the local gateway"))
                    else:
                        self._next_refresh = self.hass.loop.time() + self._refresh_interval
                        self.async_set_updated_data(statuses)

    async def async_stop(self, event: Event) -> None:
        """Close on HA shutdown, which does not unload integration entries."""
        await self.async_close()

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
        self._closed = True
        try:
            if task := self._listen_task:
                self._listen_task = None
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
        finally:
            async with self._io_lock:
                if not self._close_complete:
                    self.client.set_update_callback(None)
                    await self.hass.async_add_executor_job(self.client.close)
                    self._close_complete = True
