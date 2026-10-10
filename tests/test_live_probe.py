"""Reconfiguration must share, or temporarily release, the gateway connection."""

from unittest.mock import Mock

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.tuya_ble_lock.const import DOMAIN
from custom_components.tuya_ble_lock.coordinator import GatewayCoordinator
from custom_components.tuya_ble_lock.local import CannotConnect, LockConfig, LockStatus, Topology


@pytest.mark.parametrize("reuse_connection", [False, True])
@pytest.mark.parametrize("failed_candidate", [False, True])
async def test_reconfigure_probe_owns_connection_and_restores_reads(
    hass, entry_data, reuse_connection, failed_candidate
):
    """Candidate failures restore the configured connection without any unlock."""
    entry = MockConfigEntry(domain=DOMAIN, data=entry_data)
    entry.add_to_hass(hass)
    current, candidate = Mock(), Mock()
    lock = LockConfig(**entry_data["locks"][0])
    coordinator = GatewayCoordinator(hass, entry, current, [lock])
    statuses = {"test_node": LockStatus(True, 72, True)}
    current.poll.return_value = statuses
    expected = Topology(frozenset({"test_node", "new_node"}), frozenset())
    sequence = []

    def probe():
        sequence.append("probe")
        if failed_candidate:
            raise CannotConnect("Cannot read gateway")
        return expected

    current.close.side_effect = lambda: sequence.append("close")
    current.poll.side_effect = lambda locks: sequence.append("restore") or statuses
    current.probe_live.side_effect = probe
    candidate.probe.side_effect = probe
    try:
        if failed_candidate:
            with pytest.raises(CannotConnect):
                await coordinator.async_probe(candidate, reuse_connection=reuse_connection)
        else:
            assert (
                await coordinator.async_probe(candidate, reuse_connection=reuse_connection)
                == expected
            )
        if reuse_connection:
            assert sequence == ["probe"]
            current.probe_live.assert_called_once()
            candidate.probe.assert_not_called()
        else:
            assert sequence == ["close", "probe", "restore"]
            current.probe_live.assert_not_called()
            candidate.probe.assert_called_once()
            assert coordinator.last_update_success
            assert coordinator.data == statuses
        current.unlock.assert_not_called()
        candidate.unlock.assert_not_called()
    finally:
        await coordinator.async_close()
