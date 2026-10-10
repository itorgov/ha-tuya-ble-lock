"""Shared Home Assistant test fixtures; real device I/O is forbidden."""

from unittest.mock import patch

import pytest

from custom_components.tuya_ble_lock.local import LockStatus, Topology


@pytest.fixture(autouse=True)
def _enable_custom_integrations(enable_custom_integrations):
    """Permit this repository's custom integration in Home Assistant tests."""
    yield


@pytest.fixture
def entry_data():
    """Return synthetic local configuration."""
    return {
        "name": "Test gateway",
        "host": "192.0.2.10",
        "gateway_id": "test_gateway",
        "local_key": "0123456789abcdef",
        "protocol_version": "3.5",
        "locks": [
            {
                "device_id": "test_lock",
                "node_id": "test_node",
                "name": "Test lock",
                "member": 1,
                "remote_unlock_code": "12345678",
            },
        ],
    }


@pytest.fixture
def mock_gateway():
    """Replace network operations while keeping the real HA lifecycle."""
    with (
        patch(
            "custom_components.tuya_ble_lock.local.GatewayClient.probe",
            return_value=Topology(frozenset({"test_node"}), frozenset()),
        ),
        patch(
            "custom_components.tuya_ble_lock.local.GatewayClient.poll",
            return_value={"test_node": LockStatus(True, 72)},
        ) as poll,
        patch("custom_components.tuya_ble_lock.local.GatewayClient.unlock") as unlock,
        patch("custom_components.tuya_ble_lock.local.GatewayClient.listen") as listen,
        patch("custom_components.tuya_ble_lock.local.GatewayClient.close") as close,
    ):
        yield {"poll": poll, "unlock": unlock, "listen": listen, "close": close}
