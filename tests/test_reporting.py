"""Verify reported physical states and registry upgrades through real HA entities."""

from copy import deepcopy

import pytest
from homeassistant.components.lock import LockState
from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    EVENT_STATE_CHANGED,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_ble_lock.const import DOMAIN
from custom_components.tuya_ble_lock.local import LockStatus

STATE_LOCKED = LockState.LOCKED
STATE_UNLOCKED = LockState.UNLOCKED


async def setup_entry(hass, entry_data):
    """Start all platforms with synthetic transport results."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test gateway", unique_id="test_gateway", data=entry_data
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def entity_ids(hass, device_id="test_lock"):
    """Resolve stable registry IDs without depending on generated names."""
    registry = er.async_get(hass)
    result = {
        "lock": registry.async_get_entity_id("lock", DOMAIN, device_id),
        "battery": registry.async_get_entity_id("sensor", DOMAIN, f"{device_id}_battery"),
    }
    assert all(result.values())
    return result


async def report(hass, entry, statuses):
    """Use the same callback boundary used by executor-based LAN reception."""
    entry.runtime_data._receive_update(statuses)
    await hass.async_block_till_done()


async def test_external_reports_publish_each_lock_transition_to_history_bus(
    hass, entry_data, mock_gateway
):
    """Rapid external unlock/relock reports each produce a recordable state event."""
    entry = await setup_entry(hass, entry_data)
    lock_id = entity_ids(hass)["lock"]
    assert hass.states.get(lock_id).state == STATE_UNKNOWN
    changes = []

    @callback
    def observe(event):
        if event.data["entity_id"] == lock_id:
            changes.append((event.data["old_state"].state, event.data["new_state"].state))

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, observe)
    try:
        # Queue the complete burst before yielding. An update coalesced to its
        # final value would hide the physical unlocking event from Recorder.
        for is_locked in (True, False, True):
            entry.runtime_data._receive_update({"test_node": LockStatus(True, 72, is_locked)})
        await hass.async_block_till_done()
    finally:
        unsubscribe()

    assert changes == [
        (STATE_UNKNOWN, STATE_LOCKED),
        (STATE_LOCKED, STATE_UNLOCKED),
        (STATE_UNLOCKED, STATE_LOCKED),
    ]
    assert hass.states.get(lock_id).state == STATE_LOCKED
    mock_gateway["unlock"].assert_not_called()
    assert mock_gateway["poll"].call_count == 1


async def test_battery_and_lock_are_enabled_independent_entities(hass, entry_data, mock_gateway):
    """Battery updates cannot manufacture a locked bolt or an extra Door sensor."""
    entry = await setup_entry(hass, entry_data)
    ids = entity_ids(hass)
    registry = er.async_get(hass)
    assert len({registry.async_get(entity_id).device_id for entity_id in ids.values()}) == 1
    assert registry.async_get(ids["lock"]).disabled_by is None
    assert registry.async_get(ids["battery"]).disabled_by is None
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, "test_lock_door") is None
    assert hass.states.get(ids["battery"]).attributes["device_class"] == "battery"
    assert hass.states.get(ids["battery"]).attributes["unit_of_measurement"] == "%"
    assert hass.states.get(ids["battery"]).state == "72"
    assert hass.states.get(ids["lock"]).state == STATE_UNKNOWN

    await report(hass, entry, {"test_node": LockStatus(True, 41, None)})
    assert hass.states.get(ids["battery"]).state == "41"
    assert hass.states.get(ids["lock"]).state == STATE_UNKNOWN

    await report(hass, entry, {"test_node": LockStatus(True, 41, True)})
    assert hass.states.get(ids["lock"]).state == STATE_LOCKED

    await report(hass, entry, {"test_node": LockStatus(True, 18, True)})
    assert hass.states.get(ids["battery"]).state == "18"
    assert hass.states.get(ids["lock"]).state == STATE_LOCKED

    await report(hass, entry, {"test_node": LockStatus(True, None, None)})
    assert all(hass.states.get(entity_id).state == STATE_UNKNOWN for entity_id in ids.values())
    mock_gateway["unlock"].assert_not_called()


async def test_hx201_dp40_hardware_sequence_reaches_home_assistant(hass, entry_data, mock_gateway):
    """Replay the observed DP40 cycle through parsing, coordination and entities."""
    entry_data["locks"][0]["state_source"] = "hx201_dp40"
    entry = await setup_entry(hass, entry_data)
    ids = entity_ids(hass)
    client = entry.runtime_data.client
    client._locks = {lock.node_id: lock for lock in entry.runtime_data.locks}
    transitions = []

    @callback
    def observe(event):
        if event.data["entity_id"] == ids["lock"]:
            transitions.append(event.data["new_state"].state)

    unsubscribe = hass.bus.async_listen(EVENT_STATE_CHANGED, observe)
    try:
        for dps in (
            {"8": 100, "40": "closed"},
            {"61": "AAAB"},
            {"40": "open"},
            {"40": "open"},
            {"40": "closed"},
            {"40": "closed"},
        ):
            client._handle_response({"cid": "test_node", "dps": dps})
        await hass.async_block_till_done()
    finally:
        unsubscribe()
    assert transitions == [STATE_LOCKED, STATE_UNLOCKED, STATE_LOCKED]
    assert hass.states.get(ids["battery"]).state == "100"
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)) == 2
    mock_gateway["unlock"].assert_not_called()


async def test_reporting_keeps_siblings_and_availability_separate(hass, entry_data, mock_gateway):
    """One child's battery and bolt reports do not update another child."""
    entry_data["locks"].append(
        {
            "device_id": "second_lock",
            "node_id": "second_node",
            "name": "Second lock",
            "member": 2,
            "remote_unlock_code": "87654321",
        }
    )
    mock_gateway["poll"].return_value = {
        "test_node": LockStatus(True, 72, True),
        "second_node": LockStatus(True, 18, False),
    }
    entry = await setup_entry(hass, entry_data)
    first = entity_ids(hass)
    second = entity_ids(hass, "second_lock")
    assert hass.states.get(first["lock"]).state == STATE_LOCKED
    assert hass.states.get(second["lock"]).state == STATE_UNLOCKED
    await report(
        hass,
        entry,
        {
            "test_node": LockStatus(False),
            "second_node": LockStatus(True, 17, True),
        },
    )
    assert all(
        hass.states.get(entity_id).state == STATE_UNAVAILABLE for entity_id in first.values()
    )
    assert hass.states.get(second["lock"]).state == STATE_LOCKED
    assert hass.states.get(second["battery"]).state == "17"
    await report(
        hass,
        entry,
        {
            "test_node": LockStatus(True),
            "second_node": LockStatus(True, 17, True),
        },
    )
    assert all(hass.states.get(entity_id).state == STATE_UNKNOWN for entity_id in first.values())
    assert hass.states.get(second["lock"]).state == STATE_LOCKED
    mock_gateway["unlock"].assert_not_called()


@pytest.mark.parametrize(
    "disabled_by", [er.RegistryEntryDisabler.INTEGRATION, er.RegistryEntryDisabler.USER]
)
async def test_upgrade_removes_legacy_entities_and_preserves_user_customizations(
    hass, entry_data, mock_gateway, disabled_by
):
    """Remove obsolete button/Door records while preserving current entities and choices."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test gateway", unique_id="test_gateway", data=entry_data
    )
    entry.add_to_hass(hass)
    registry = er.async_get(hass)
    legacy_lock = registry.async_get_or_create(
        "lock", DOMAIN, "test_lock", config_entry=entry, suggested_object_id="old_hallway_lock"
    )
    registry.async_update_entity(
        legacy_lock.entity_id, name="My hallway lock", hidden_by=er.RegistryEntryHider.USER
    )
    legacy_battery = registry.async_get_or_create(
        "sensor", DOMAIN, "test_lock_battery", config_entry=entry, disabled_by=disabled_by
    )
    legacy_button = registry.async_get_or_create(
        "button", DOMAIN, "test_lock_unlock", config_entry=entry
    )
    legacy_door = registry.async_get_or_create(
        "binary_sensor", DOMAIN, "test_lock_door", config_entry=entry
    )
    other_entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id="other_gateway",
        data=deepcopy(entry_data),
        disabled_by=ConfigEntryDisabler.USER,
    )
    other_entry.add_to_hass(hass)
    other_button = registry.async_get_or_create(
        "button", DOMAIN, "unrelated_lock_unlock", config_entry=other_entry
    )
    other_door = registry.async_get_or_create(
        "binary_sensor", DOMAIN, "unrelated_lock_door", config_entry=other_entry
    )

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    ids = entity_ids(hass)
    assert registry.async_get(legacy_button.entity_id) is None
    assert registry.async_get(legacy_door.entity_id) is None
    assert registry.async_get(other_button.entity_id) is not None
    assert registry.async_get(other_door.entity_id) is not None
    assert ids["lock"] == legacy_lock.entity_id
    assert registry.async_get(ids["lock"]).id == legacy_lock.id
    assert registry.async_get(ids["lock"]).name == "My hallway lock"
    assert registry.async_get(ids["lock"]).hidden_by is er.RegistryEntryHider.USER
    assert registry.async_get(ids["lock"]).disabled_by is None
    assert registry.async_get(ids["battery"]).id == legacy_battery.id
    expected_disabled = disabled_by if disabled_by is er.RegistryEntryDisabler.USER else None
    assert registry.async_get(ids["battery"]).disabled_by is expected_disabled
    if expected_disabled is None:
        assert hass.states.get(ids["battery"]).state == "72"
    else:
        assert hass.states.get(ids["battery"]) is None
    assert registry.async_get_entity_id("binary_sensor", DOMAIN, "test_lock_door") is None


async def test_receiver_stops_on_unload_and_late_reports_do_not_restore_entities(
    hass, entry_data, mock_gateway
):
    """An executor callback completing after unload must not resurrect states."""
    entry = await setup_entry(hass, entry_data)
    ids = entity_ids(hass)
    coordinator = entry.runtime_data
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    before = {key: hass.states.get(entity_id) for key, entity_id in ids.items()}
    coordinator._receive_update({"test_node": LockStatus(True, 99, False)})
    await hass.async_block_till_done()
    assert {key: hass.states.get(entity_id) for key, entity_id in ids.items()} == before
    mock_gateway["close"].assert_called_once()


async def test_home_assistant_stop_closes_receiver_without_requiring_entry_unload(
    hass, entry_data, mock_gateway
):
    """The installed shutdown hook closes once even when HA keeps entries loaded."""
    entry = await setup_entry(hass, entry_data)
    coordinator = entry.runtime_data
    receiver = coordinator._listen_task
    assert receiver is not None
    ids = entity_ids(hass)
    before = {key: hass.states.get(entity_id) for key, entity_id in ids.items()}

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert receiver.done()
    assert coordinator._listen_task is None
    assert coordinator._closed
    mock_gateway["close"].assert_called_once()

    coordinator._receive_update({"test_node": LockStatus(True, 99, False)})
    await hass.async_block_till_done()
    assert {key: hass.states.get(entity_id) for key, entity_id in ids.items()} == before
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    mock_gateway["close"].assert_called_once()
    mock_gateway["unlock"].assert_not_called()
