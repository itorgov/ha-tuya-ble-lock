"""Exercise push reception, periodic reads, recovery, and shutdown without I/O."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components import tuya_ble_lock
from custom_components.tuya_ble_lock import coordinator as coordinator_module
from custom_components.tuya_ble_lock.const import DOMAIN
from custom_components.tuya_ble_lock.coordinator import GatewayCoordinator
from custom_components.tuya_ble_lock.local import (
    CannotConnect,
    GatewayClient,
    LockConfig,
    LockStatus,
)


async def make_coordinator(hass, entry_data):
    """Initialize one gateway and its first read without setting up platforms."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, options={"scan_interval": 60})
    entry.add_to_hass(hass)
    client = Mock(spec=GatewayClient)
    client.poll.return_value = {"test_node": LockStatus(True, 72, True)}
    client.listen.return_value = None
    lock = LockConfig(**entry_data["locks"][0])
    coordinator = GatewayCoordinator(hass, entry, client, [lock])
    await coordinator.async_refresh()
    return coordinator, client, lock


async def test_receiver_is_started_once_and_serialized_with_unlock_and_poll(hass, entry_data):
    """A slow receiver must finish before either a command or poll uses its socket."""
    coordinator, client, lock = await make_coordinator(hass, entry_data)
    started = asyncio.Event()
    release = threading.Event()
    sequence = []

    def listen():
        sequence.append("listen start")
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        sequence.append("listen end")

    def unlock(_lock):
        assert sequence[-1] != "listen start"
        sequence.append("unlock")

    def poll(_locks):
        assert sequence[-1] != "listen start"
        sequence.append("poll")
        return {"test_node": LockStatus(True, 71, False)}

    client.listen = listen
    client.unlock.side_effect = unlock
    client.poll.side_effect = poll
    client.poll.reset_mock()
    coordinator.async_start()
    first_task = coordinator._listen_task
    coordinator.async_start()
    assert coordinator._listen_task is first_task
    try:
        await asyncio.wait_for(started.wait(), 2)
        unlocking = asyncio.create_task(coordinator.async_unlock(lock))
        polling = asyncio.create_task(coordinator.async_refresh())
        await asyncio.sleep(0)
        client.unlock.assert_not_called()
        client.poll.assert_not_called()
        client.close.assert_not_called()
        release.set()
        await asyncio.wait_for(asyncio.gather(unlocking, polling), 2)
        assert sequence[:2] == ["listen start", "listen end"]
        client.unlock.assert_called_once_with(lock)
        client.poll.assert_called_once_with([lock])
    finally:
        release.set()
        await coordinator.async_close()
    assert first_task.done()
    assert coordinator._listen_task is None
    client.close.assert_called_once()
    client.set_update_callback.assert_called_with(None)


async def test_close_waits_for_receiver_before_closing_socket(hass, entry_data):
    """Cancelling receive cannot close the shared socket underneath its executor."""
    coordinator, client, _ = await make_coordinator(hass, entry_data)
    started = asyncio.Event()
    release = threading.Event()

    def listen():
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)

    client.listen = listen
    coordinator.async_start()
    await asyncio.wait_for(started.wait(), 2)
    closing = asyncio.create_task(coordinator.async_close())
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        client.close.assert_not_called()
        assert not closing.done()
    finally:
        release.set()
        await asyncio.wait_for(closing, 2)
    client.close.assert_called_once()
    client.set_update_callback.assert_called_with(None)
    client.unlock.assert_not_called()
    coordinator.async_start()
    assert coordinator._listen_task is None


async def test_diagnostics_waits_for_receive_without_requesting_device_data(hass, entry_data):
    """A diagnostic download snapshots counters after in-flight reports finish."""
    coordinator, client, _ = await make_coordinator(hass, entry_data)
    started = asyncio.Event()
    release = threading.Event()
    counters = {"locks": [{"dp47_report_count": 0}]}

    def listen():
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        counters["locks"][0]["dp47_report_count"] = 1

    client.listen = listen
    client.diagnostics.side_effect = lambda: counters
    client.poll.reset_mock()
    coordinator.async_start()
    try:
        await asyncio.wait_for(started.wait(), 2)
        download = asyncio.create_task(coordinator.async_get_transport_diagnostics())
        await asyncio.sleep(0)
        client.diagnostics.assert_not_called()
        release.set()
        assert await asyncio.wait_for(download, 2) == {"locks": [{"dp47_report_count": 1}]}
        client.poll.assert_not_called()
        client.unlock.assert_not_called()
    finally:
        release.set()
        await coordinator.async_close()


async def test_cancelled_setup_waits_for_first_poll_and_closes_client(
    hass, entry_data, monkeypatch
):
    """Cancelling initial setup cannot leak or prematurely close a live socket."""
    entry = MockConfigEntry(
        domain=DOMAIN, data=entry_data, state=ConfigEntryState.SETUP_IN_PROGRESS
    )
    entry.add_to_hass(hass)
    client = Mock(spec=GatewayClient)
    started = asyncio.Event()
    release = threading.Event()
    sequence = []

    def poll(_locks):
        sequence.append("poll start")
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        sequence.append("poll end")
        return {"test_node": LockStatus(True, 72, True)}

    # Plain functions use HA's real executor; Mock jobs run inline in its tests.
    client.poll = poll
    client.close.side_effect = lambda: sequence.append("close")
    monkeypatch.setattr(tuya_ble_lock, "GatewayClient", Mock(return_value=client))
    setup = asyncio.create_task(tuya_ble_lock.async_setup_entry(hass, entry))
    try:
        await asyncio.wait_for(started.wait(), 2)
        setup.cancel()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not setup.done()
        client.close.assert_not_called()
        client.listen.assert_not_called()
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(setup, 2)
        entry.mock_state(hass, ConfigEntryState.NOT_LOADED)
    assert sequence == ["poll start", "poll end", "close"]
    client.close.assert_called_once()
    client.set_update_callback.assert_called_with(None)
    client.unlock.assert_not_called()
    assert not hass.states.async_all()


async def test_receiver_recovers_by_read_only_poll_after_sanitized_error(
    hass, entry_data, monkeypatch, caplog
):
    """A read failure causes one bounded reconnect read and never a command replay."""
    coordinator, client, _ = await make_coordinator(hass, entry_data)
    resumed = asyncio.Event()
    hold_receiver = asyncio.Event()
    delays = []

    def listen():
        if client.listen.call_count == 1:
            raise CannotConnect("private-vendor-error")
        hass.loop.call_soon_threadsafe(resumed.set)

    async def controlled_sleep(delay):
        delays.append(delay)
        if client.listen.call_count >= 2:
            await hold_receiver.wait()
        else:
            await asyncio.sleep(0)

    # Replace only this module's asyncio view, leaving HA and test time alone.
    monkeypatch.setattr(
        coordinator_module,
        "asyncio",
        SimpleNamespace(
            sleep=controlled_sleep,
            shield=asyncio.shield,
            CancelledError=asyncio.CancelledError,
        ),
    )
    client.listen.side_effect = listen
    coordinator.async_start()
    try:
        await asyncio.wait_for(resumed.wait(), 2)
        assert coordinator.last_update_success
        assert coordinator.data == client.poll.return_value
        assert 5 in delays
        assert client.poll.call_count == 2
        assert client.listen.call_count == 2
        client.unlock.assert_not_called()
        assert "private-vendor-error" not in caplog.text
    finally:
        await coordinator.async_close()
    client.close.assert_called_once()


async def test_push_reports_do_not_postpone_periodic_status_poll(hass, entry_data):
    """Push activity leaves the independent periodic read deadline unchanged."""
    coordinator, client, _ = await make_coordinator(hass, entry_data)
    original_deadline = coordinator._next_refresh
    for is_locked in (False, True, False):
        coordinator._receive_update({"test_node": LockStatus(True, 72, is_locked)})
    await hass.async_block_till_done()
    assert coordinator._next_refresh == original_deadline
    assert coordinator.update_interval is None

    polled = asyncio.Event()
    listening = asyncio.Event()

    def poll(_locks):
        hass.loop.call_soon_threadsafe(polled.set)
        return {"test_node": LockStatus(True, 71, True)}

    def listen():
        hass.loop.call_soon_threadsafe(listening.set)

    client.poll.side_effect = poll
    client.listen.side_effect = listen
    # Represent an elapsed deadline without changing HA's event-loop clock.
    coordinator._next_refresh = hass.loop.time() - 1
    coordinator.async_start()
    try:
        await asyncio.wait_for(polled.wait(), 2)
        await asyncio.wait_for(listening.wait(), 2)
        assert client.poll.call_count == 2
        assert coordinator._next_refresh > hass.loop.time()
        assert coordinator.data == {"test_node": LockStatus(True, 71, True)}
        client.unlock.assert_not_called()
    finally:
        await coordinator.async_close()
