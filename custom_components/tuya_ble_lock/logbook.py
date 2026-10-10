"""Describe acknowledged commands without claiming a physical lock transition."""

from collections.abc import Callable

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_TUYA_BLE_LOCK


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Register command activity when Home Assistant enables Logbook."""

    @callback
    def describe_unlock_acknowledgement(event: Event) -> dict[str, str]:
        """Keep the message distinct from reported physical lock state."""
        entity_id = event.data["entity_id"]
        state = hass.states.get(entity_id)
        return {
            LOGBOOK_ENTRY_NAME: state.name if state is not None else "Tuya BLE Lock",
            LOGBOOK_ENTRY_MESSAGE: "Unlock command acknowledged",
            LOGBOOK_ENTRY_ENTITY_ID: entity_id,
        }

    async_describe_event(DOMAIN, EVENT_TUYA_BLE_LOCK, describe_unlock_acknowledgement)
