"""Verify explicit unlock buttons through real Home Assistant service calls."""

import traceback
from copy import deepcopy

import pytest
from homeassistant.const import STATE_UNAVAILABLE, STATE_UNKNOWN
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_ble_lock.const import DOMAIN
from custom_components.tuya_ble_lock.local import LockConfig, LockStatus, UnlockOutcomeUnknown


async def setup_entry(hass, entry_data):
    """Load all production platforms using the mocked gateway fixture."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test gateway", unique_id="test_gateway", data=entry_data
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_unlock_button_is_visible_enabled_on_existing_child_device(
    hass, entry_data, mock_gateway
):
    """The explicit action is usable while the standard lock API remains intact."""
    entry = await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    button_id = registry.async_get_entity_id("button", DOMAIN, "test_lock_unlock")
    lock_id = registry.async_get_entity_id("lock", DOMAIN, "test_lock")
    assert button_id and lock_id
    button_entry = registry.async_get(button_id)
    lock_entry = registry.async_get(lock_id)
    assert button_entry.unique_id == "test_lock_unlock"
    assert button_entry.hidden_by is None
    assert button_entry.disabled_by is None
    assert button_entry.entity_category is None
    assert button_entry.device_id == lock_entry.device_id
    assert lock_entry.hidden_by is None
    assert lock_entry.disabled_by is None
    assert hass.states.get(button_id).state == STATE_UNKNOWN
    assert hass.states.get(lock_id).state == STATE_UNKNOWN
    device = dr.async_get(hass).async_get_device_by_identifier(
        (DOMAIN, "test_lock"), entry.entry_id
    )
    assert device and button_entry.device_id == device.id

    expected = LockConfig(**entry_data["locks"][0])
    await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    mock_gateway["unlock"].assert_called_once_with(expected)
    assert hass.states.get(lock_id).state == STATE_UNKNOWN

    mock_gateway["unlock"].reset_mock()
    await hass.services.async_call("lock", "unlock", {"entity_id": lock_id}, blocking=True)
    mock_gateway["unlock"].assert_called_once_with(expected)
    assert hass.states.get(lock_id).state == STATE_UNKNOWN
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call("lock", "lock", {"entity_id": lock_id}, blocking=True)
    mock_gateway["unlock"].assert_called_once_with(expected)


async def test_button_missing_code_rejects_without_gateway_io(hass, entry_data, mock_gateway):
    """A configured button cannot unlock until an existing code is supplied."""
    entry_data["locks"][0]["remote_unlock_code"] = None
    await setup_entry(hass, entry_data)
    button_id = er.async_get(hass).async_get_entity_id("button", DOMAIN, "test_lock_unlock")
    polls_before = mock_gateway["poll"].call_count
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    assert error.value.translation_key == "missing_code"
    mock_gateway["unlock"].assert_not_called()
    assert mock_gateway["poll"].call_count == polls_before
    assert entry_data["locks"][0]["remote_unlock_code"] is None


async def test_button_failure_is_redacted_and_never_retried(hass, entry_data, mock_gateway, caplog):
    """An ambiguous command outcome cannot cause a second unlock attempt."""
    await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    button_id = registry.async_get_entity_id("button", DOMAIN, "test_lock_unlock")
    lock_id = registry.async_get_entity_id("lock", DOMAIN, "test_lock")
    mock_gateway["unlock"].side_effect = UnlockOutcomeUnknown("private-vendor-value")
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call("button", "press", {"entity_id": button_id}, blocking=True)
    assert error.value.translation_key == "unlock_failed"
    assert "private-vendor-value" not in "".join(traceback.format_exception(error.value))
    assert "private-vendor-value" not in caplog.text
    mock_gateway["unlock"].assert_called_once_with(LockConfig(**entry_data["locks"][0]))
    assert hass.states.get(lock_id).state == STATE_UNKNOWN


async def test_buttons_use_only_their_own_child_and_independent_availability(
    hass, entry_data, mock_gateway
):
    """Each button follows its node and retains its own member and credential."""
    entry_data["locks"].append(
        {
            "device_id": "other_lock",
            "node_id": "other_node",
            "name": "Other lock",
            "member": 23,
            "remote_unlock_code": "00004321",
        }
    )
    mock_gateway["poll"].return_value = {
        "test_node": LockStatus(True),
        "other_node": LockStatus(False),
    }
    entry = await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    first_id = registry.async_get_entity_id("button", DOMAIN, "test_lock_unlock")
    second_id = registry.async_get_entity_id("button", DOMAIN, "other_lock_unlock")
    assert registry.async_get(first_id).device_id != registry.async_get(second_id).device_id
    assert hass.states.get(first_id).state == STATE_UNKNOWN
    assert hass.states.get(second_id).state == STATE_UNAVAILABLE
    await hass.services.async_call("button", "press", {"entity_id": first_id}, blocking=True)
    mock_gateway["unlock"].assert_called_once_with(LockConfig(**entry_data["locks"][0]))

    mock_gateway["poll"].return_value = {
        "test_node": LockStatus(False),
        "other_node": LockStatus(True),
    }
    await entry.runtime_data.async_refresh()
    assert hass.states.get(first_id).state == STATE_UNAVAILABLE
    assert hass.states.get(second_id).state == STATE_UNKNOWN
    mock_gateway["unlock"].reset_mock()
    await hass.services.async_call("button", "press", {"entity_id": second_id}, blocking=True)
    mock_gateway["unlock"].assert_called_once_with(LockConfig(**entry_data["locks"][1]))


async def test_button_registry_customizations_survive_reload_and_removal_cleans_up(
    hass, entry_data, mock_gateway
):
    """Reload must not recreate the button or lose a user's name and entity ID."""
    entry = await setup_entry(hass, entry_data)
    registry = er.async_get(hass)
    button_id = registry.async_get_entity_id("button", DOMAIN, "test_lock_unlock")
    before = registry.async_get(button_id)
    registry.async_update_entity(
        button_id,
        new_entity_id="button.hallway_unlock",
        name="Hallway unlock",
        hidden_by=er.RegistryEntryHider.USER,
    )
    await hass.async_block_till_done()
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    after_id = registry.async_get_entity_id("button", DOMAIN, "test_lock_unlock")
    after = registry.async_get(after_id)
    assert after.id == before.id
    assert after.entity_id == "button.hallway_unlock"
    assert after.name == "Hallway unlock"
    assert after.hidden_by is er.RegistryEntryHider.USER
    assert after.device_id == before.device_id
    assert hass.states.get(after_id) is not None
    await hass.services.async_call("button", "press", {"entity_id": after_id}, blocking=True)
    mock_gateway["unlock"].assert_called_once_with(LockConfig(**entry_data["locks"][0]))

    new_data = deepcopy(entry_data)
    new_data["locks"] = []
    hass.config_entries.async_update_entry(entry, data=new_data)
    mock_gateway["poll"].return_value = {}
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert registry.async_get_entity_id("button", DOMAIN, "test_lock_unlock") is None
    assert registry.async_get_entity_id("lock", DOMAIN, "test_lock") is None
    assert hass.states.get(after_id) is None
    devices = dr.async_get(hass)
    assert devices.async_get_device_by_identifier((DOMAIN, "test_lock"), entry.entry_id) is None
    assert devices.async_get_device_by_identifier((DOMAIN, "test_gateway"), entry.entry_id)
