"""Verify truthful command Activity through Home Assistant's Logbook pipeline."""

import pytest
from homeassistant.components import logbook
from homeassistant.components.logbook.helpers import (
    async_determine_event_types,
    async_subscribe_events,
    event_forwarder_filtered,
)
from homeassistant.components.logbook.models import LogbookConfig, async_event_to_row
from homeassistant.components.logbook.processor import EventProcessor
from homeassistant.const import EVENT_STATE_CHANGED, STATE_UNKNOWN
from homeassistant.core import Context, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.integration_platform import async_process_integration_platforms
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_capture_events

from custom_components.tuya_ble_lock.const import DOMAIN, EVENT_TUYA_BLE_LOCK
from custom_components.tuya_ble_lock.local import (
    CannotConnect,
    UnlockOutcomeUnknown,
    UnlockRejected,
)


async def setup_lock(hass, entry_data):
    """Load the real lock platform with the caller's mocked gateway."""
    entry = MockConfigEntry(
        domain=DOMAIN, title="Test gateway", unique_id="test_gateway", data=entry_data
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    registry = er.async_get(hass)
    entity_id = registry.async_get_entity_id("lock", DOMAIN, "test_lock")
    registry_entry = registry.async_get(entity_id)
    assert registry_entry.device_id is not None
    return entity_id, registry_entry.device_id


@pytest.mark.parametrize("filter_by", ["device", "entity"])
async def test_acknowledgements_render_in_device_and_entity_activity(
    hass, hass_admin_user, entry_data, mock_gateway, filter_by
):
    """Every ACK is visible in Activity even while the physical state stays unknown."""
    # Use HA's integration-platform discovery and the same event processor as
    # Activity, without starting an unrelated HTTP frontend or Recorder database.
    hass.data[logbook.DOMAIN] = LogbookConfig({})
    await async_process_integration_platforms(
        hass, logbook.DOMAIN, logbook._process_logbook_platform, wait_for_platforms=True
    )
    entity_id, device_id = await setup_lock(hass, entry_data)
    assert EVENT_TUYA_BLE_LOCK in hass.data[logbook.DOMAIN].external_events
    entity_ids = [entity_id] if filter_by == "entity" else None
    device_ids = [device_id] if filter_by == "device" else None
    event_types = async_determine_event_types(hass, entity_ids, device_ids)
    assert EVENT_TUYA_BLE_LOCK in event_types
    activity_events = []
    subscriptions = []

    @callback
    def receive_activity(event):
        activity_events.append(event)

    async_subscribe_events(
        hass, subscriptions, receive_activity, event_types, None, entity_ids, device_ids
    )
    state_events = async_capture_events(hass, EVENT_STATE_CHANGED)
    contexts = [Context(user_id=hass_admin_user.id), Context(parent_id=Context().id)]
    try:
        for context in contexts:
            await hass.services.async_call(
                "lock", "unlock", {"entity_id": entity_id}, blocking=True, context=context
            )
        await hass.async_block_till_done()
    finally:
        for unsubscribe in subscriptions:
            unsubscribe()

    assert len(activity_events) == 2
    for event, context in zip(activity_events, contexts, strict=True):
        assert event.event_type == EVENT_TUYA_BLE_LOCK
        assert event.data == {
            "type": "unlock_acknowledged",
            "entity_id": entity_id,
            "device_id": device_id,
        }
        assert event.context is context
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    assert not [event for event in state_events if event.data["entity_id"] == entity_id]
    assert mock_gateway["unlock"].call_count == 2
    entry_id = hass.config_entries.async_entries(DOMAIN)[0].entry_id
    assert len(er.async_entries_for_config_entry(er.async_get(hass), entry_id)) == 2

    processor = EventProcessor(hass, event_types, entity_ids=entity_ids, device_ids=device_ids)
    rendered = processor.humanify([async_event_to_row(event) for event in activity_events])
    assert len(rendered) == 2
    for item, context in zip(rendered, contexts, strict=True):
        assert item["name"] == hass.states.get(entity_id).name
        assert item["message"] == "Unlock command acknowledged"
        assert item["entity_id"] == entity_id
        assert item["domain"] == DOMAIN
        assert item.get("context_user_id") == context.user_id
        assert "state" not in item

    unrelated = []
    forward = event_forwarder_filtered(
        unrelated.append, None, ["lock.other_lock"], ["unrelated-device"]
    )
    forward(activity_events[0])
    assert unrelated == []


@pytest.mark.parametrize("failure", [None, CannotConnect, UnlockRejected, UnlockOutcomeUnknown])
async def test_failed_or_invalid_unlock_does_not_publish_acknowledgement(
    hass, entry_data, mock_gateway, failure
):
    """Validation failures, rejected commands and timeouts must not become success Activity."""
    if failure is None:
        entry_data["locks"][0]["remote_unlock_code"] = None
    else:
        mock_gateway["unlock"].side_effect = failure("private-vendor-error")
    entity_id, _ = await setup_lock(hass, entry_data)
    events = async_capture_events(hass, EVENT_TUYA_BLE_LOCK)
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call("lock", "unlock", {"entity_id": entity_id}, blocking=True)
    await hass.async_block_till_done()
    assert events == []
    assert hass.states.get(entity_id).state == STATE_UNKNOWN
    assert mock_gateway["unlock"].call_count == (0 if failure is None else 1)
