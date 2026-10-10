"""Exercise the real Home Assistant flows without contacting any devices."""

import base64
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant import config_entries
from homeassistant.const import CONF_DEVICE_ID, CONF_HOST, CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.tuya_ble_lock as integration
from custom_components.tuya_ble_lock import config_flow
from custom_components.tuya_ble_lock.cloud import (
    CloudAuthError,
    CloudError,
    DiscoveredLock,
    GatewayDiscovery,
)
from custom_components.tuya_ble_lock.const import (
    CONF_GATEWAY_ID,
    CONF_LOCAL_KEY,
    CONF_LOCKS,
    CONF_MEMBER,
    CONF_NODE_ID,
    CONF_PAYLOAD,
    CONF_PROFILE,
    CONF_REMOTE_CODE,
    CONF_STATE_SOURCE,
    CONF_VERSION,
    DOMAIN,
    HX201_PRODUCT_ID,
    STATE_SOURCE_HX201_DP40,
    STATE_SOURCE_MOTOR_DP47,
)
from custom_components.tuya_ble_lock.local import LocalError, Topology

pytestmark = pytest.mark.asyncio

GATEWAY = {
    CONF_NAME: "Example gateway",
    CONF_HOST: "192.0.2.10",
    CONF_GATEWAY_ID: "gateway-example",
    CONF_LOCAL_KEY: "0123456789abcdef",
    CONF_VERSION: "3.5",
}
LOCK = {
    CONF_NAME: "Example lock",
    CONF_DEVICE_ID: "lock-example",
    CONF_NODE_ID: "abcdef012345",
    CONF_MEMBER: 1,
    CONF_REMOTE_CODE: "12345678",
    CONF_STATE_SOURCE: STATE_SOURCE_MOTOR_DP47,
}
CLOUD = {
    "region": "eu",
    "client_id": "test-client",
    "client_secret": "test-secret",
    CONF_GATEWAY_ID: "gateway-example",
}


@pytest.fixture(autouse=True)
def flow_mocks(monkeypatch, enable_custom_integrations):
    """Permit custom flows while preventing setup and gateway I/O."""
    local_client = MagicMock()
    local_client.probe.return_value = Topology(
        frozenset({LOCK[CONF_NODE_ID]}), frozenset({"secondnode"})
    )
    monkeypatch.setattr(config_flow, "GatewayClient", MagicMock(return_value=local_client))
    monkeypatch.setattr(integration, "async_setup_entry", AsyncMock(return_value=True))
    return local_client


async def start(hass, next_step="gateway"):
    """Start setup through the same menu shown in the frontend."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": next_step}
    )


async def manual_to_credentials(hass):
    """Read gateway topology, then select an existing child for configuration."""
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GATEWAY)
    assert result["step_id"] == "manage"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual_lock"}
    )
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {key: LOCK[key] for key in (CONF_NAME, CONF_DEVICE_ID, CONF_NODE_ID)},
    )


def entry(hass, **changes):
    """Add a persisted synthetic configuration for duplicate and edit tests."""
    config_entry = MockConfigEntry(
        domain=DOMAIN,
        title=GATEWAY[CONF_NAME],
        unique_id=GATEWAY[CONF_GATEWAY_ID],
        data={**GATEWAY, CONF_LOCKS: [deepcopy(LOCK)], **changes},
    )
    config_entry.add_to_hass(hass)
    return config_entry


async def test_manual_setup_creates_local_entry_without_unlocking(hass, flow_mocks):
    result = await manual_to_credentials(hass)
    assert result["step_id"] == "lock"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: LOCK[CONF_NAME],
            CONF_PROFILE: True,
            CONF_MEMBER: 1,
            CONF_REMOTE_CODE: LOCK[CONF_REMOTE_CODE],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {**GATEWAY, CONF_LOCKS: [LOCK]}
    assert result["result"].unique_id == GATEWAY[CONF_GATEWAY_ID]
    flow_mocks.probe.assert_called_once()
    flow_mocks.unlock.assert_not_called()
    await hass.async_block_till_done()


async def test_setup_can_leave_unlock_code_unconfigured(hass):
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: LOCK[CONF_NAME], CONF_PROFILE: True, CONF_MEMBER: 1},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["data"][CONF_LOCKS][0][CONF_REMOTE_CODE] is None
    await hass.async_block_till_done()


@pytest.mark.parametrize("state_source", [STATE_SOURCE_MOTOR_DP47, STATE_SOURCE_HX201_DP40])
async def test_manual_lock_persists_explicit_state_source(hass, state_source):
    """Manual setup stores the selected state profile independently of unlock credentials."""
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: LOCK[CONF_NAME],
            CONF_PROFILE: True,
            CONF_MEMBER: 1,
            CONF_STATE_SOURCE: state_source,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    stored = result["data"][CONF_LOCKS][0]
    assert stored[CONF_STATE_SOURCE] == state_source
    assert stored[CONF_REMOTE_CODE] is None
    assert set(stored) == set(LOCK)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "product_id,expected_source",
    [
        (HX201_PRODUCT_ID, STATE_SOURCE_HX201_DP40),
        (None, STATE_SOURCE_MOTOR_DP47),
        ("unknown-product", STATE_SOURCE_MOTOR_DP47),
        (HX201_PRODUCT_ID.upper(), STATE_SOURCE_MOTOR_DP47),
    ],
)
async def test_cloud_state_source_default_requires_exact_verified_product_id(
    hass, monkeypatch, product_id, expected_source
):
    """Only the known PID selects DP40; a matching marketing name is insufficient."""
    discovery = GatewayDiscovery(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID],
                LOCK[CONF_NODE_ID],
                LOCK[CONF_NAME],
                "HX201_BLE_LOCKER",
                True,
                product_id=product_id,
            )
        ],
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(return_value=discovery))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {key: GATEWAY[key] for key in (CONF_NAME, CONF_HOST, CONF_VERSION)},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCKS: [LOCK[CONF_DEVICE_ID]]}
    )
    form_values = {CONF_NAME: LOCK[CONF_NAME], CONF_PROFILE: True, CONF_MEMBER: 1}
    assert result["data_schema"](form_values)[CONF_STATE_SOURCE] == expected_source
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    stored = result["data"][CONF_LOCKS][0]
    assert stored[CONF_STATE_SOURCE] == expected_source
    assert set(stored) == set(LOCK)
    assert "product_id" not in stored and "product_name" not in stored
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "initial_source,selection",
    [
        (None, None),
        (STATE_SOURCE_MOTOR_DP47, None),
        (STATE_SOURCE_HX201_DP40, None),
        (None, STATE_SOURCE_HX201_DP40),
        (STATE_SOURCE_HX201_DP40, STATE_SOURCE_MOTOR_DP47),
    ],
)
async def test_edit_preserves_state_source_and_defaults_legacy_entries_to_motor(
    hass, monkeypatch, initial_source, selection
):
    """Editing a name/code keeps the user's profile and treats old entries conservatively."""
    old_lock = deepcopy(LOCK)
    if initial_source is None:
        old_lock.pop(CONF_STATE_SOURCE)
    else:
        old_lock[CONF_STATE_SOURCE] = initial_source
    config_entry = entry(hass, **{CONF_LOCKS: [old_lock]})
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {key: GATEWAY[key] for key in (CONF_NAME, CONF_HOST, CONF_VERSION)},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "edit_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: LOCK[CONF_DEVICE_ID]}
    )
    form_values = {CONF_NAME: "New lock name", CONF_PROFILE: True, CONF_MEMBER: 1}
    expected = initial_source or STATE_SOURCE_MOTOR_DP47
    assert result["data_schema"](form_values)[CONF_STATE_SOURCE] == expected
    if selection is not None:
        form_values[CONF_STATE_SOURCE] = selection
    result = await hass.config_entries.flow.async_configure(result["flow_id"], form_values)
    await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert config_entry.data[CONF_LOCKS][0][CONF_STATE_SOURCE] == (selection or expected)
    assert config_entry.data[CONF_LOCKS][0][CONF_REMOTE_CODE] == LOCK[CONF_REMOTE_CODE]


async def test_cloud_rediscovery_does_not_replace_a_saved_state_source(hass, monkeypatch):
    """Finding the known HX201 PID again does not override a previous user selection."""
    config_entry = entry(hass)
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    discovery = GatewayDiscovery(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID],
                LOCK[CONF_NODE_ID],
                LOCK[CONF_NAME],
                "HX201_BLE_LOCKER",
                True,
                product_id=HX201_PRODUCT_ID,
            )
        ],
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(return_value=discovery))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {key: value for key, value in CLOUD.items() if key != CONF_GATEWAY_ID}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {key: GATEWAY[key] for key in (CONF_NAME, CONF_HOST, CONF_VERSION)},
    )
    assert result["step_id"] == "no_locks"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    assert config_entry.data[CONF_LOCKS] == [LOCK]


@pytest.mark.parametrize("remote_code,confirmed", [("too-short", True), ("12345678", False)])
async def test_bad_credentials_or_unconfirmed_profile_cannot_be_saved(hass, remote_code, confirmed):
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: LOCK[CONF_NAME],
            CONF_PROFILE: confirmed,
            CONF_MEMBER: 1,
            CONF_REMOTE_CODE: remote_code,
        },
    )
    assert result["step_id"] == "lock"
    assert result["errors"] == {"base": "invalid_credentials"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_cloud_discovery_keeps_credentials_out_of_config_entry(hass, monkeypatch):
    discovery = GatewayDiscovery(
        gateway_id=GATEWAY[CONF_GATEWAY_ID],
        name=GATEWAY[CONF_NAME],
        local_key=GATEWAY[CONF_LOCAL_KEY],
        locks=[
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID], LOCK[CONF_NODE_ID], LOCK[CONF_NAME], "Example", True
            ),
            DiscoveredLock("bulb-example", "secondnode", "Example bulb", "Example", False),
        ],
    )
    discover_gateway = AsyncMock(return_value=discovery)
    monkeypatch.setattr(config_flow, "async_discover_gateway", discover_gateway)
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["step_id"] == "gateway"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            key: value
            for key, value in GATEWAY.items()
            if key not in (CONF_LOCAL_KEY, CONF_GATEWAY_ID)
        },
    )
    assert result["step_id"] == "select_locks"
    assert result["description_placeholders"]["unsupported"] == "1"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCKS: [LOCK[CONF_DEVICE_ID]]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: LOCK[CONF_NAME], CONF_PROFILE: True, CONF_MEMBER: 1},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(result["data"]) == set(GATEWAY) | {CONF_LOCKS}
    assert "test-secret" not in repr(result["data"])
    assert "test-client" not in repr(result["data"])
    assert "region" not in result["data"]
    assert result["data"][CONF_LOCAL_KEY] == GATEWAY[CONF_LOCAL_KEY]
    discover_gateway.assert_awaited_once()
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "exception,error", [(CloudAuthError, "invalid_auth"), (CloudError, "cloud_error")]
)
async def test_cloud_errors_are_shown_without_credentials(hass, monkeypatch, exception, error):
    monkeypatch.setattr(
        config_flow, "async_discover_gateway", AsyncMock(side_effect=exception("private-value"))
    )
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["step_id"] == "cloud"
    assert result["errors"] == {"base": error}
    assert "private-value" not in repr(result)
    assert "test-secret" not in repr(result)


async def test_failed_local_connection_does_not_create_entry(hass, flow_mocks):
    flow_mocks.probe.side_effect = LocalError("connection failed")
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GATEWAY)
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "cannot_connect"}
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_duplicate_gateway_aborts_before_network(hass, flow_mocks):
    entry(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GATEWAY)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    flow_mocks.probe.assert_not_called()


async def test_manual_node_must_exist_in_selected_gateway(hass):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], GATEWAY)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Example", CONF_DEVICE_ID: "other-lock", CONF_NODE_ID: "unknownnode"},
    )
    assert result["step_id"] == "manual_lock"
    assert result["errors"] == {"base": "node_not_found"}


async def test_reconfigure_keeps_identity_and_can_replace_code(hass, monkeypatch):
    config_entry = entry(hass)
    reload_entry = AsyncMock(return_value=True)
    monkeypatch.setattr(hass.config_entries, "async_reload", reload_entry)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    assert result["step_id"] == "gateway"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Renamed gateway", CONF_HOST: "192.0.2.20", CONF_VERSION: "3.5"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "edit_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: LOCK[CONF_DEVICE_ID]}
    )
    assert LOCK[CONF_REMOTE_CODE] not in repr(result["data_schema"])
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Renamed lock",
            CONF_PROFILE: True,
            CONF_MEMBER: 2,
            CONF_REMOTE_CODE: "87654321",
            CONF_STATE_SOURCE: STATE_SOURCE_MOTOR_DP47,
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.unique_id == GATEWAY[CONF_GATEWAY_ID]
    assert config_entry.data[CONF_GATEWAY_ID] == GATEWAY[CONF_GATEWAY_ID]
    assert config_entry.data[CONF_LOCAL_KEY] == GATEWAY[CONF_LOCAL_KEY]
    assert config_entry.data[CONF_HOST] == "192.0.2.20"
    assert config_entry.data[CONF_LOCKS][0][CONF_DEVICE_ID] == LOCK[CONF_DEVICE_ID]
    assert config_entry.data[CONF_LOCKS][0][CONF_REMOTE_CODE] == "87654321"
    reload_entry.assert_awaited_once_with(config_entry.entry_id)


@pytest.mark.parametrize(
    "changes,reuse_connection",
    [
        ({}, True),
        ({CONF_NAME: "Renamed gateway"}, True),
        ({CONF_HOST: "192.0.2.20"}, False),
        ({CONF_LOCAL_KEY: "fedcba9876543210"}, False),
        ({CONF_VERSION: "3.4"}, False),
    ],
)
async def test_loaded_reconfigure_routes_gateway_probe_through_connection_owner(
    hass, flow_mocks, changes, reuse_connection
):
    """A running entry coordinates probe traffic instead of opening a competing socket."""
    config_entry = entry(hass)
    config_entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
    coordinator = MagicMock()
    coordinator.async_probe = AsyncMock(return_value=flow_mocks.probe.return_value)
    config_entry.runtime_data = coordinator
    previous = deepcopy(dict(config_entry.data))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    submitted = {
        CONF_NAME: GATEWAY[CONF_NAME],
        CONF_HOST: GATEWAY[CONF_HOST],
        CONF_VERSION: GATEWAY[CONF_VERSION],
        **changes,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], submitted)
    assert result["step_id"] == "manage"
    coordinator.async_probe.assert_awaited_once_with(flow_mocks, reuse_connection=reuse_connection)
    flow_mocks.probe.assert_not_called()
    flow_mocks.unlock.assert_not_called()
    config_flow.GatewayClient.assert_called_once_with(
        submitted[CONF_HOST],
        GATEWAY[CONF_GATEWAY_ID],
        changes.get(CONF_LOCAL_KEY, GATEWAY[CONF_LOCAL_KEY]),
        float(submitted[CONF_VERSION]),
    )
    assert config_entry.data == previous


async def test_loaded_reconfigure_probe_failure_keeps_configuration_and_redacts_error(
    hass, flow_mocks
):
    """A coordinator probe failure is a normal form error with no partial edits."""
    config_entry = entry(hass)
    config_entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
    coordinator = MagicMock()
    coordinator.async_probe = AsyncMock(side_effect=LocalError("private-gateway-error"))
    config_entry.runtime_data = coordinator
    previous = deepcopy(dict(config_entry.data))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "New name", CONF_HOST: "192.0.2.20", CONF_VERSION: "3.5"},
    )
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "cannot_connect"}
    assert "private-gateway-error" not in repr(result)
    assert config_entry.data == previous
    coordinator.async_probe.assert_awaited_once_with(flow_mocks, reuse_connection=False)
    flow_mocks.probe.assert_not_called()


@pytest.mark.parametrize(
    "state,has_runtime",
    [
        (config_entries.ConfigEntryState.LOADED, False),
        (config_entries.ConfigEntryState.SETUP_ERROR, True),
    ],
)
async def test_reconfigure_without_running_coordinator_probes_candidate_directly(
    hass, flow_mocks, state, has_runtime
):
    """An entry that does not own an active connection can use an ordinary probe."""
    config_entry = entry(hass)
    config_entry.mock_state(hass, state)
    coordinator = MagicMock()
    coordinator.async_probe = AsyncMock()
    if has_runtime:
        config_entry.runtime_data = coordinator
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: GATEWAY[CONF_NAME], CONF_HOST: GATEWAY[CONF_HOST], CONF_VERSION: "3.5"},
    )
    assert result["step_id"] == "manage"
    flow_mocks.probe.assert_called_once()
    coordinator.async_probe.assert_not_awaited()


async def test_options_updates_polling_without_changing_credentials(hass, monkeypatch):
    config_entry = entry(hass)
    original = deepcopy(dict(config_entry.data))
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["step_id"] == "init"
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_SCAN_INTERVAL: 120}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert config_entry.options == {CONF_SCAN_INTERVAL: 120}
    assert config_entry.data == original


async def test_stale_cloud_node_is_not_offered_and_gateway_only_setup_is_possible(
    hass, monkeypatch
):
    discovery = GatewayDiscovery(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [DiscoveredLock("stale-lock", "stalenode", "Stale lock", "Example", True)],
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(return_value=discovery))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            key: value
            for key, value in GATEWAY.items()
            if key not in (CONF_LOCAL_KEY, CONF_GATEWAY_ID)
        },
    )
    assert result["step_id"] == "no_locks"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "manage"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCKS] == []
    await hass.async_block_till_done()


async def test_duplicate_lock_cannot_be_added_under_another_gateway(hass):
    entry(hass)
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**GATEWAY, CONF_GATEWAY_ID: "another-gateway"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "manual_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {key: LOCK[key] for key in (CONF_NAME, CONF_DEVICE_ID, CONF_NODE_ID)}
    )
    assert result["errors"] == {"base": "duplicate_lock"}


@pytest.mark.parametrize(
    "clear_code,extra_input",
    [
        (False, {}),
        (True, {}),
        (
            True,
            {
                CONF_PAYLOAD: base64.b64encode(b"\x01\x00\x02" + b"87654321\x00\x00").decode(),
                CONF_REMOTE_CODE: "87654321",
            },
        ),
        (True, {CONF_PAYLOAD: "not-valid-base64", CONF_REMOTE_CODE: "invalid"}),
    ],
    ids=["preserve", "clear", "clear-overrides-valid-import", "clear-ignores-invalid-import"],
)
async def test_reconfigure_preserves_or_explicitly_clears_existing_code(
    hass, monkeypatch, flow_mocks, clear_code, extra_input
):
    config_entry = entry(hass)
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: GATEWAY[CONF_NAME], CONF_HOST: GATEWAY[CONF_HOST], CONF_VERSION: "3.5"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "edit_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: LOCK[CONF_DEVICE_ID]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: LOCK[CONF_NAME],
            CONF_PROFILE: True,
            CONF_MEMBER: 1,
            "clear_code": clear_code,
            **extra_input,
        },
    )
    assert result["step_id"] == "manage"
    # Reconfiguration is transactional until the user selects Save.
    assert config_entry.data[CONF_LOCKS][0][CONF_REMOTE_CODE] == LOCK[CONF_REMOTE_CODE]
    await hass.config_entries.flow.async_configure(result["flow_id"], {"next_step_id": "finish"})
    expected = None if clear_code else LOCK[CONF_REMOTE_CODE]
    assert config_entry.data[CONF_LOCKS][0][CONF_REMOTE_CODE] == expected
    assert config_entry.data[CONF_LOCKS][0][CONF_MEMBER] == 1
    assert CONF_PAYLOAD not in config_entry.data[CONF_LOCKS][0]
    assert "clear_code" not in config_entry.data[CONF_LOCKS][0]
    flow_mocks.unlock.assert_not_called()


async def test_reconfigure_can_remove_last_lock(hass, monkeypatch):
    config_entry = entry(hass)
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: GATEWAY[CONF_NAME], CONF_HOST: GATEWAY[CONF_HOST], CONF_VERSION: "3.5"},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "remove_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: LOCK[CONF_DEVICE_ID]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_LOCKS] == []


@pytest.mark.parametrize("interval", [0, 29, 601])
async def test_options_rejects_out_of_range_polling(hass, interval):
    config_entry = entry(hass)
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    with pytest.raises(InvalidData):
        await hass.config_entries.options.async_configure(
            result["flow_id"], {CONF_SCAN_INTERVAL: interval}
        )
    assert config_entry.options == {}


async def test_imported_payload_preserves_leading_zero_code_and_extracts_member(hass, flow_mocks):
    raw_payload = b"\x01\x01\x23" + b"00001234" + b"\x00\x00"
    payload = base64.b64encode(raw_payload).decode("ascii")
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: LOCK[CONF_NAME], CONF_PROFILE: True, CONF_MEMBER: 1, CONF_PAYLOAD: payload},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    stored_lock = result["data"][CONF_LOCKS][0]
    assert stored_lock[CONF_MEMBER] == 291
    assert stored_lock[CONF_REMOTE_CODE] == "00001234"
    assert stored_lock[CONF_NODE_ID] == LOCK[CONF_NODE_ID]
    assert CONF_PAYLOAD not in stored_lock
    assert payload not in repr(result["data"])
    flow_mocks.unlock.assert_not_called()
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "payload",
    [
        "not-valid-base64!",
        "\N{SNOWMAN}",
        base64.b64encode(b"short").decode(),
        base64.b64encode(b"\x00\x00\x01" + b"12345678" + b"\x00\x00").decode(),
        base64.b64encode(b"\x01\x00\x01" + b"12345678" + b"\x00\x01").decode(),
        base64.b64encode(b"\x01\x00\x00" + b"12345678" + b"\x00\x00").decode(),
        base64.b64encode(b"\x01\xff\xff" + b"12345678" + b"\x00\x00").decode(),
        base64.b64encode(b"\x01\x00\x01" + b"\xff" * 8 + b"\x00\x00").decode(),
        base64.b64encode(b"\x01\x00\x01" + b"abcd1234" + b"\x00\x00").decode(),
    ],
    ids=[
        "base64",
        "unicode",
        "length",
        "operation",
        "suffix",
        "zero-member",
        "reserved-member",
        "non-ascii-code",
        "non-digit-code",
    ],
)
async def test_invalid_imported_payload_is_rejected_without_device_writes(
    hass, flow_mocks, payload
):
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: LOCK[CONF_NAME], CONF_PROFILE: True, CONF_MEMBER: 1, CONF_PAYLOAD: payload},
    )
    assert result["step_id"] == "lock"
    assert result["errors"] == {"base": "invalid_credentials"}
    assert hass.config_entries.async_entries(DOMAIN) == []
    flow_mocks.unlock.assert_not_called()


async def test_multiple_cloud_locks_keep_independent_node_member_and_code(
    hass, monkeypatch, flow_mocks
):
    discovery = GatewayDiscovery(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID], LOCK[CONF_NODE_ID], LOCK[CONF_NAME], "Example", True
            ),
            DiscoveredLock("second-lock", "secondnode", "Second lock", "Example", True),
        ],
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(return_value=discovery))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            key: value
            for key, value in GATEWAY.items()
            if key not in (CONF_LOCAL_KEY, CONF_GATEWAY_ID)
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCKS: [LOCK[CONF_DEVICE_ID], "second-lock"]}
    )
    assert result["step_id"] == "lock"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "First configured lock",
            CONF_PROFILE: True,
            CONF_MEMBER: 2,
            CONF_REMOTE_CODE: "12345678",
        },
    )
    assert result["step_id"] == "lock"
    assert hass.config_entries.async_entries(DOMAIN) == []
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Second configured lock",
            CONF_PROFILE: True,
            CONF_MEMBER: 3,
            CONF_REMOTE_CODE: "87654321",
        },
    )
    assert result["step_id"] == "manage"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["data"][CONF_LOCKS] == [
        {**LOCK, CONF_NAME: "First configured lock", CONF_MEMBER: 2},
        {
            CONF_DEVICE_ID: "second-lock",
            CONF_NODE_ID: "secondnode",
            CONF_NAME: "Second configured lock",
            CONF_MEMBER: 3,
            CONF_REMOTE_CODE: "87654321",
            CONF_STATE_SOURCE: STATE_SOURCE_MOTOR_DP47,
        },
    ]
    flow_mocks.unlock.assert_not_called()
    await hass.async_block_till_done()


async def test_further_discovery_cannot_change_gateway_after_adding_lock(hass, monkeypatch):
    discover_gateway = AsyncMock(
        return_value=GatewayDiscovery(
            GATEWAY[CONF_GATEWAY_ID], GATEWAY[CONF_NAME], GATEWAY[CONF_LOCAL_KEY], []
        )
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", discover_gateway)
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: LOCK[CONF_NAME],
            CONF_PROFILE: True,
            CONF_MEMBER: 1,
            CONF_REMOTE_CODE: LOCK[CONF_REMOTE_CODE],
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**CLOUD, CONF_GATEWAY_ID: "different-gateway"}
        )
    discover_gateway.assert_not_awaited()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {key: value for key, value in CLOUD.items() if key != CONF_GATEWAY_ID}
    )
    assert discover_gateway.await_args.args[-1] == GATEWAY[CONF_GATEWAY_ID]
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(
            result["flow_id"], {**GATEWAY, CONF_GATEWAY_ID: "different-gateway"}
        )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            key: value
            for key, value in GATEWAY.items()
            if key not in (CONF_LOCAL_KEY, CONF_GATEWAY_ID)
        },
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["data"][CONF_GATEWAY_ID] == GATEWAY[CONF_GATEWAY_ID]
    assert result["data"][CONF_LOCKS] == [LOCK]
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "changes",
    [
        {CONF_GATEWAY_ID: "../another-device"},
        {CONF_GATEWAY_ID: ""},
        {CONF_HOST: "https://192.0.2.10"},
        {CONF_HOST: "999.0.2.10"},
        {CONF_LOCAL_KEY: "short"},
        {CONF_LOCAL_KEY: "\N{SNOWMAN}" * 16},
        {CONF_NAME: "   "},
    ],
)
async def test_malformed_gateway_settings_never_probe_network(hass, flow_mocks, changes):
    result = await start(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**GATEWAY, **changes}
    )
    assert result["step_id"] == "gateway"
    assert result["errors"] == {"base": "invalid_input"}
    flow_mocks.probe.assert_not_called()
