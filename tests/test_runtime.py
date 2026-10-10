"""Exercise real Home Assistant setup, entities, actions and cleanup offline."""

import asyncio
import json
import threading
from copy import deepcopy
from unittest.mock import Mock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.update_coordinator import UpdateFailed
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_ble_lock.const import DOMAIN
from custom_components.tuya_ble_lock.coordinator import GatewayCoordinator
from custom_components.tuya_ble_lock.diagnostics import async_get_config_entry_diagnostics
from custom_components.tuya_ble_lock.local import (
    CannotConnect,
    LockConfig,
    LockStatus,
    UnlockOutcomeUnknown,
)


async def setup_entry(hass, entry_data):
    """Load the actual integration with synthetic gateway responses."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test gateway", unique_id="test_gateway", data=entry_data
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_setup_registry_unlock_and_unload(hass, entry_data, mock_gateway):
    """A child has a stable ID and parent gateway; unlock does not fake state."""
    entry = await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("lock", DOMAIN, "test_lock")
    assert entity_id
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    devices = dr.async_get(hass)
    gateway = devices.async_get_device_by_identifier((DOMAIN, "test_gateway"), entry.entry_id)
    lock = devices.async_get_device_by_identifier((DOMAIN, "test_lock"), entry.entry_id)
    assert gateway and lock and lock.via_device_id == gateway.id
    await hass.services.async_call("lock", "unlock", {"entity_id": entity_id}, blocking=True)
    assert mock_gateway["unlock"].call_count == 1
    assert mock_gateway["unlock"].call_args.args[0].node_id == "test_node"
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("lock", "lock", {"entity_id": entity_id}, blocking=True)
    assert mock_gateway["unlock"].call_count == 1
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert mock_gateway["close"].call_count == 1


async def test_missing_code_and_action_code(hass, entry_data, mock_gateway):
    """Users can provide a code on the standard action without saving it."""
    entry_data["locks"][0]["remote_unlock_code"] = None
    await setup_entry(hass, entry_data)
    entity_id = er.async_get(hass).async_get_entity_id("lock", DOMAIN, "test_lock")
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("lock", "unlock", {"entity_id": entity_id}, blocking=True)
    mock_gateway["unlock"].assert_not_called()
    await hass.services.async_call(
        "lock", "unlock", {"entity_id": entity_id, "code": "87654321"}, blocking=True
    )
    assert mock_gateway["unlock"].call_args.args[0].remote_unlock_code == "87654321"
    assert entry_data["locks"][0]["remote_unlock_code"] is None


async def test_unlock_error_does_not_retry_or_publish_secrets(
    hass, entry_data, mock_gateway, caplog
):
    """Ambiguous sends fail once and do not change lock state."""
    entry = await setup_entry(hass, entry_data)
    entity_id = er.async_get(hass).async_get_entity_id("lock", DOMAIN, "test_lock")
    mock_gateway["unlock"].side_effect = UnlockOutcomeUnknown("private-vendor-value")
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call("lock", "unlock", {"entity_id": entity_id}, blocking=True)
    assert "private-vendor-value" not in str(error.value) + caplog.text
    assert mock_gateway["unlock"].call_count == 1
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    diagnostic = json.dumps(await async_get_config_entry_diagnostics(hass, entry))
    for private in (
        "Test gateway",
        "test_gateway",
        "test_lock",
        "test_node",
        "192.0.2.10",
        "0123456789abcdef",
        "12345678",
    ):
        assert private not in diagnostic
    assert json.loads(diagnostic)["configured_locks"] == 1


async def test_children_have_independent_availability(hass, entry_data, mock_gateway):
    """An offline child does not make the other lock unavailable."""
    entry_data["locks"].append(
        {
            "device_id": "other_lock",
            "node_id": "other_node",
            "name": "Other lock",
            "member": 2,
            "remote_unlock_code": None,
        }
    )
    mock_gateway["poll"].return_value = {
        "test_node": LockStatus(True),
        "other_node": LockStatus(False),
    }
    entry = await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    first = registry.async_get_entity_id("lock", DOMAIN, "test_lock")
    second = registry.async_get_entity_id("lock", DOMAIN, "other_lock")
    assert hass.states.get(first).state == STATE_UNKNOWN
    assert hass.states.get(second).state == STATE_UNAVAILABLE
    mock_gateway["poll"].return_value = {
        "test_node": LockStatus(False),
        "other_node": LockStatus(True),
    }
    await entry.runtime_data.async_refresh()
    assert hass.states.get(first).state == STATE_UNAVAILABLE
    assert hass.states.get(second).state == STATE_UNKNOWN


async def test_gateway_failure_and_recovery(hass, entry_data, mock_gateway):
    """Poll failures mark the entity unavailable and the next good poll recovers."""
    entry = await setup_entry(hass, entry_data)
    entity_id = er.async_get(hass).async_get_entity_id("lock", DOMAIN, "test_lock")
    mock_gateway["poll"].side_effect = CannotConnect("connection failed")
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    mock_gateway["poll"].side_effect = None
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_initial_failure_retries_setup(hass, entry_data, mock_gateway):
    """A gateway unavailable at startup uses Home Assistant's retry mechanism."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data, unique_id="test_gateway")
    entry.add_to_hass(hass)
    mock_gateway["poll"].side_effect = CannotConnect("private-vendor-value")
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_RETRY
    mock_gateway["close"].assert_called_once()


async def test_remove_child_cleans_registry_without_affecting_gateway(
    hass, entry_data, mock_gateway
):
    """Saving an empty gateway removes its old child registry entries."""
    entry = await setup_entry(hass, entry_data)
    new_data = deepcopy(entry_data)
    new_data["locks"] = []
    hass.config_entries.async_update_entry(entry, data=new_data)
    mock_gateway["poll"].return_value = {}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert er.async_get(hass).async_get_entity_id("lock", DOMAIN, "test_lock") is None
    assert (
        dr.async_get(hass).async_get_device_by_identifier((DOMAIN, "test_gateway"), entry.entry_id)
        is not None
    )


async def test_battery_disabled_by_default_and_fresh(hass, entry_data, mock_gateway):
    """Optional battery never falls back to a stale retained percentage."""
    entry = await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("sensor", DOMAIN, "test_lock_battery")
    assert registry.async_get(entity_id).disabled_by is er.RegistryEntryDisabler.INTEGRATION
    assert hass.states.get(entity_id) is None
    from custom_components.tuya_ble_lock.sensor import BatterySensor

    sensor = BatterySensor(entry.runtime_data, entry.runtime_data.locks[0])
    assert sensor.native_value == 72
    entry.runtime_data.async_set_updated_data({"test_node": LockStatus(True)})
    assert sensor.native_value is None


async def test_poll_and_unlock_are_serialized(hass, entry_data):
    """Concurrent requests never access one gateway socket from two threads."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    client = Mock()
    started = asyncio.Event()
    release = threading.Event()

    def poll(locks):
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return {"test_node": LockStatus(True)}

    client.poll = poll
    lock = LockConfig(**entry_data["locks"][0])
    coordinator = GatewayCoordinator(hass, entry, client, [lock])
    polling = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(started.wait(), 5)
    unlocking = asyncio.create_task(coordinator.async_unlock(lock))
    await asyncio.sleep(0)
    client.unlock.assert_not_called()
    release.set()
    await asyncio.gather(polling, unlocking)
    client.unlock.assert_called_once_with(lock)
    await coordinator.async_close()
    with pytest.raises(UpdateFailed):
        await coordinator._async_update_data()


async def test_cancelled_poll_keeps_gateway_serialized(hass, entry_data):
    """Cancellation waits for the running executor operation before closing."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    client = Mock()
    started = asyncio.Event()
    release = threading.Event()

    def poll(locks):
        hass.loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return {}

    client.poll = poll
    coordinator = GatewayCoordinator(hass, entry, client, [])
    polling = asyncio.create_task(coordinator._async_update_data())
    await asyncio.wait_for(started.wait(), 5)
    polling.cancel()
    closing = asyncio.create_task(coordinator.async_close())
    await asyncio.sleep(0)
    polling.cancel()
    await asyncio.sleep(0)
    client.close.assert_not_called()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await polling
    await closing
    client.close.assert_called_once()
