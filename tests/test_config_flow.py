"""Exercise the real Home Assistant flows without contacting any devices."""

import asyncio
import base64
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from homeassistant import config_entries
from homeassistant.const import (
    ATTR_AREA_ID as CONF_AREA_ID,
)
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
)
from homeassistant.data_entry_flow import FlowResultType, InvalidData, UnknownFlow
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
    CONF_AREAS,
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
from custom_components.tuya_ble_lock.credentials import UnlockCredential
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
    CONF_HOST: GATEWAY[CONF_HOST],
    CONF_GATEWAY_ID: "gateway-example",
}


def discovered_gateway(*args, **kwargs):
    """Build synthetic metadata for the verified automatic gateway profile."""
    kwargs.setdefault("product_id", "csvxfq3ecmedidkh")
    return GatewayDiscovery(*args, **kwargs)


@pytest.fixture(autouse=True)
def credential_reader(monkeypatch):
    """Keep all command-history requests offline, including existing setup tests."""
    reader = AsyncMock(return_value=None)
    monkeypatch.setattr(config_flow, "async_read_unlock_credential", reader)
    return reader


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
    "product_id", [HX201_PRODUCT_ID, None, "unknown-product", HX201_PRODUCT_ID.upper()]
)
async def test_cloud_state_source_default_requires_exact_verified_product_id(
    hass, monkeypatch, credential_reader, product_id
):
    """Only exact verified product metadata enters automatic setup."""
    credential_reader.return_value = UnlockCredential(2, "00001234")
    lock = DiscoveredLock(
        LOCK[CONF_DEVICE_ID],
        LOCK[CONF_NODE_ID],
        LOCK[CONF_NAME],
        "HX201_BLE_LOCKER",
        True,
        product_id=product_id,
    )
    monkeypatch.setattr(
        config_flow,
        "async_discover_gateway",
        AsyncMock(
            return_value=discovered_gateway(
                GATEWAY[CONF_GATEWAY_ID], GATEWAY[CONF_NAME], GATEWAY[CONF_LOCAL_KEY], [lock]
            )
        ),
    )
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    if product_id != HX201_PRODUCT_ID:
        assert result["step_id"] == "no_locks"
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
        assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_new_locks"
        credential_reader.assert_not_awaited()
        return
    assert result["step_id"] == "select_locks"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCKS: [LOCK[CONF_DEVICE_ID]]}
    )
    assert result["step_id"] == "lock_ready"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: LOCK[CONF_NAME]}
    )
    stored = result["data"][CONF_LOCKS][0]
    assert stored[CONF_STATE_SOURCE] == STATE_SOURCE_HX201_DP40
    assert set(stored) == set(LOCK)
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


async def test_cloud_rediscovery_does_not_replace_a_saved_state_source(
    hass, monkeypatch, credential_reader
):
    """An empty rediscovery can refresh the key while preserving every existing lock setting."""
    config_entry = entry(hass)
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    fresh_key = "abcdef0123456789"
    discovery = discovered_gateway(
        GATEWAY[CONF_GATEWAY_ID],
        "Cloud name",
        fresh_key,
        [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID],
                LOCK[CONF_NODE_ID],
                LOCK[CONF_NAME],
                "HX201",
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
    assert (
        result["data_schema"]({"client_id": "test-client", "client_secret": "test-secret"})[
            CONF_HOST
        ]
        == GATEWAY[CONF_HOST]
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {key: value for key, value in CLOUD.items() if key != CONF_GATEWAY_ID}
    )
    assert result["step_id"] == "gateway_ready" and result["last_step"] is True
    assert config_entry.data[CONF_LOCAL_KEY] == GATEWAY[CONF_LOCAL_KEY]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_LOCKS] == [LOCK]
    assert config_entry.data[CONF_LOCAL_KEY] == fresh_key
    assert config_entry.data[CONF_NAME] == GATEWAY[CONF_NAME]
    credential_reader.assert_not_awaited()


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


async def test_cloud_discovery_keeps_credentials_out_of_config_entry(
    hass, monkeypatch, credential_reader, flow_mocks
):
    """Automatic setup never displays or saves technical cloud fields."""
    credential_reader.return_value = UnlockCredential(1, "12345678")
    discovery = discovered_gateway(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID],
                LOCK[CONF_NODE_ID],
                LOCK[CONF_NAME],
                "HX201",
                True,
                product_id=HX201_PRODUCT_ID,
            ),
            DiscoveredLock("bulb-example", "secondnode", "Bulb", "Example", False),
        ],
    )
    discover_gateway = AsyncMock(return_value=discovery)
    monkeypatch.setattr(config_flow, "async_discover_gateway", discover_gateway)
    result = await start(hass, "cloud")
    assert {str(key.schema) for key in result["data_schema"].schema} == {
        "region",
        "client_id",
        "client_secret",
        CONF_GATEWAY_ID,
        CONF_HOST,
    }
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["step_id"] == "select_locks"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_LOCKS: [LOCK[CONF_DEVICE_ID]]}
    )
    assert result["step_id"] == "lock_ready"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: LOCK[CONF_NAME]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert set(result["data"]) == set(GATEWAY) | {CONF_LOCKS}
    assert "test-secret" not in repr(result["data"]) and "test-client" not in repr(result["data"])
    assert result["data"][CONF_LOCAL_KEY] == GATEWAY[CONF_LOCAL_KEY]
    discover_gateway.assert_awaited_once()
    flow_mocks.probe.assert_called_once()
    flow_mocks.unlock.assert_not_called()
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


@pytest.mark.parametrize(
    "stage,expected",
    [
        ("token", "requesting an access token"),
        ("gateway_details", "reading the gateway details"),
        ("gateway_children", "listing the gateway's locks"),
        ("child_details", "reading a lock's details"),
        ("child_specification", "checking a lock's supported commands"),
    ],
)
async def test_cloud_failure_identifies_stage_without_exposing_response(
    hass, monkeypatch, caplog, stage, expected
):
    error = CloudError(
        "PRIVATE_RESPONSE test-secret test-client gateway-example",
        stage=stage,
        reason="api_error",
        code="1106",
        status=200,
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(side_effect=error))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["errors"] == {"base": "cloud_request_failed"}
    details = result["description_placeholders"]
    assert details["stage"] == expected
    assert "authorized" in details["details"]
    assert details["reference"] == "(Tuya error 1106)"
    for value in ("PRIVATE_RESPONSE", "test-secret", "test-client", "gateway-example"):
        assert value not in repr(result)
        assert value not in caplog.text


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("connection", "DNS"),
        ("timeout", "did not respond in time"),
        ("invalid_json", "unreadable response"),
        ("invalid_response", "incomplete device information"),
        ("invalid_input", "Access ID"),
        ("invalid_token", "usable access token"),
        ("invalid_gateway", "requested gateway"),
        ("missing_local_key", "no Local Key"),
        ("invalid_local_key", "unusable gateway Local Key"),
        ("invalid_children", "invalid gateway device list"),
        ("conflicting_children", "conflicting lock identities"),
        ("invalid_child", "invalid lock identity"),
    ],
)
async def test_cloud_failure_reason_gives_fixed_action(hass, monkeypatch, reason, expected):
    error = CloudError("PRIVATE_BODY", stage="gateway_details", reason=reason)
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(side_effect=error))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["errors"] == {"base": "cloud_request_failed"}
    assert expected in result["description_placeholders"]["details"]
    assert result["description_placeholders"]["reference"] == ""
    assert "PRIVATE_BODY" not in repr(result)


@pytest.mark.parametrize(
    "code,expected",
    [
        ("1004", "Access Secret"),
        ("1010", "new token"),
        ("28841105", "authorized"),
        ("60009001", "authorized"),
        ("28841101", "not subscribed"),
        ("28841002", "expired"),
        ("28841103", "overdue"),
        ("28841004", "quota is exhausted"),
        ("1013", "date and time"),
        ("1114", "public IP"),
        ("2010", "cannot find the device"),
        ("40000901", "cannot find the device"),
        ("1108", "API address"),
        ("1199", "limiting requests"),
    ],
)
async def test_known_tuya_codes_have_actionable_explanations(code, expected):
    details = config_flow._cloud_error_details(
        CloudError("PRIVATE_BODY", stage="gateway_children", reason="api_error", code=code)
    )
    assert expected in details["details"]
    assert details["reference"] == f"(Tuya error {code})"


@pytest.mark.parametrize(
    "status,expected",
    [(403, "authorized API services"), (429, "limiting requests"), (503, "unavailable")],
)
async def test_http_failure_has_safe_status_and_instructions(hass, monkeypatch, status, expected):
    error = CloudError(stage="token", reason="http_error", status=status)
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(side_effect=error))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["errors"] == {"base": "cloud_request_failed"}
    assert expected in result["description_placeholders"]["details"]
    assert result["description_placeholders"]["reference"] == f"(HTTP {status})"


@pytest.mark.parametrize(
    "attributes",
    [
        {"stage": ["PRIVATE_STAGE"], "reason": {"PRIVATE_REASON": 1}},
        {"stage": "PRIVATE_STAGE", "reason": "PRIVATE_REASON"},
        {"stage": None, "reason": None},
    ],
)
async def test_malformed_cloud_metadata_uses_legacy_safe_error(hass, monkeypatch, attributes):
    error = CloudAuthError("PRIVATE_BODY")
    for key, value in {**attributes, "code": "PRIVATE_CODE", "status": "PRIVATE_STATUS"}.items():
        setattr(error, key, value)
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(side_effect=error))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["errors"] == {"base": "invalid_auth"}
    assert not result.get("description_placeholders")
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize(
    "code,status",
    [("12345678", "403"), (True, True), ({"PRIVATE": 1}, [403]), (None, 200)],
)
async def test_cloud_error_reference_never_formats_unknown_codes_or_malformed_status(code, status):
    error = CloudError(stage="token", reason="api_error")
    error.code, error.status = code, status
    details = config_flow._cloud_error_details(error)
    assert details["reference"] == ""
    assert "12345678" not in repr(details)
    assert "PRIVATE" not in repr(details)


async def test_cloud_retry_keeps_only_selected_region_and_does_not_prefill_credentials(
    hass, monkeypatch
):
    discovery = discovered_gateway(
        GATEWAY[CONF_GATEWAY_ID], GATEWAY[CONF_NAME], GATEWAY[CONF_LOCAL_KEY], []
    )
    discover_gateway = AsyncMock(
        side_effect=[CloudError(stage="token", reason="connection"), discovery]
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", discover_gateway)
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**CLOUD, "region": "sg"}
    )
    retry = {key: value for key, value in CLOUD.items() if key not in ("region", CONF_HOST)}
    validated = result["data_schema"](retry)
    assert validated["region"] == "sg" and validated[CONF_HOST] == GATEWAY[CONF_HOST]
    for marker in result["data_schema"].schema:
        if str(marker.schema) in ("client_id", "client_secret"):
            assert marker.default is config_flow.vol.UNDEFINED
            assert not marker.description
    assert "test-secret" not in repr(result) and "test-client" not in repr(result)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], retry)
    assert result["step_id"] == "no_locks"
    assert [call.args[1] for call in discover_gateway.await_args_list] == ["sg", "sg"]
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT
    assert hass.config_entries.async_entries(DOMAIN) == []


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


async def test_stale_cloud_node_is_not_offered_or_saved(hass, monkeypatch):
    discovery = discovered_gateway(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [
            DiscoveredLock(
                "stale-lock", "stalenode", "Stale lock", "HX201", True, product_id=HX201_PRODUCT_ID
            )
        ],
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(return_value=discovery))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["step_id"] == "no_locks"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] is FlowResultType.ABORT and result["reason"] == "no_new_locks"
    assert hass.config_entries.async_entries(DOMAIN) == []


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


@pytest.mark.parametrize("trailer", [b"\x00\x00", b"\x00\x01"])
async def test_imported_payload_preserves_leading_zero_code_and_extracts_member(
    hass, flow_mocks, trailer
):
    raw_payload = b"\x01\x01\x23" + b"00001234" + trailer
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
        base64.b64encode(b"\x01\x00\x01" + b"12345678" + b"\x00\x02").decode(),
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
    hass, monkeypatch, flow_mocks, credential_reader
):
    credential_reader.side_effect = [
        UnlockCredential(2, "12345678"),
        UnlockCredential(3, "87654321"),
    ]
    locks = [
        DiscoveredLock(
            LOCK[CONF_DEVICE_ID],
            LOCK[CONF_NODE_ID],
            LOCK[CONF_NAME],
            "HX201",
            True,
            product_id=HX201_PRODUCT_ID,
        ),
        DiscoveredLock(
            "second-lock", "secondnode", "Second lock", "HX201", True, product_id=HX201_PRODUCT_ID
        ),
    ]
    result = await cloud_to_selected_locks(hass, monkeypatch, locks)
    assert result["step_id"] == "lock_ready" and result["last_step"] is False
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "First configured lock"}
    )
    assert result["step_id"] == "lock_ready" and result["last_step"] is True
    assert hass.config_entries.async_entries(DOMAIN) == []
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: "Second configured lock"}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCKS] == [
        {
            **LOCK,
            CONF_NAME: "First configured lock",
            CONF_MEMBER: 2,
            CONF_STATE_SOURCE: STATE_SOURCE_HX201_DP40,
        },
        {
            CONF_DEVICE_ID: "second-lock",
            CONF_NODE_ID: "secondnode",
            CONF_NAME: "Second configured lock",
            CONF_MEMBER: 3,
            CONF_REMOTE_CODE: "87654321",
            CONF_STATE_SOURCE: STATE_SOURCE_HX201_DP40,
        },
    ]
    flow_mocks.unlock.assert_not_called()
    await hass.async_block_till_done()


async def test_further_discovery_cannot_change_gateway_after_adding_lock(hass, monkeypatch):
    discover_gateway = AsyncMock(
        return_value=discovered_gateway(
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
    assert result["step_id"] == "gateway_ready"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
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


async def cloud_to_selected_locks(hass, monkeypatch, locks=None):
    """Select synthetic verified models without contacting Tuya or the gateway."""
    if locks is None:
        locks = [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID],
                LOCK[CONF_NODE_ID],
                LOCK[CONF_NAME],
                "HX201",
                True,
                product_id=HX201_PRODUCT_ID,
            )
        ]
    monkeypatch.setattr(
        config_flow,
        "async_discover_gateway",
        AsyncMock(
            return_value=discovered_gateway(
                GATEWAY[CONF_GATEWAY_ID],
                GATEWAY[CONF_NAME],
                GATEWAY[CONF_LOCAL_KEY],
                locks,
            )
        ),
    )
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_LOCKS: [lock.device_id for lock in locks]},
    )


async def test_verified_model_imports_minimal_credential_and_clears_cloud_access(
    hass,
    monkeypatch,
    credential_reader,
    flow_mocks,
):
    """Automatic import needs only a name and persists no project or raw-log data."""
    credential_reader.return_value = UnlockCredential(member=291, code="00001234")
    result = await cloud_to_selected_locks(hass, monkeypatch)
    assert result["step_id"] == "lock_ready"
    assert {str(marker.schema) for marker in result["data_schema"].schema} == {
        CONF_NAME,
        CONF_AREA_ID,
    }
    assert result["description_placeholders"] == {"lock_name": LOCK[CONF_NAME]}
    for secret in ("00001234", CLOUD["client_id"], CLOUD["client_secret"]):
        assert secret not in repr(result)
    credential_reader.assert_awaited_once()
    assert credential_reader.await_args.args[1:] == (
        CLOUD["region"],
        CLOUD["client_id"],
        CLOUD["client_secret"],
        LOCK[CONF_DEVICE_ID],
    )
    assert credential_reader.await_args.kwargs == {"since_ms": None}
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._data[CONF_LOCKS] == []
    assert CLOUD["client_secret"] not in repr(flow._cloud_access)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_NAME: " "})
    assert result["errors"] == {CONF_NAME: "invalid_name"}
    credential_reader.assert_awaited_once()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Renamed cabinet"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {
        **GATEWAY,
        CONF_LOCKS: [
            {
                **LOCK,
                CONF_NAME: "Renamed cabinet",
                CONF_MEMBER: 291,
                CONF_REMOTE_CODE: "00001234",
                CONF_STATE_SOURCE: STATE_SOURCE_HX201_DP40,
            }
        ],
    }
    assert flow._cloud_access is None
    assert not flow._auto_lock_ids and not flow._pending
    for key in ("client_id", "client_secret", "region", CONF_PAYLOAD, "product_id"):
        assert key not in repr(result["data"])
    flow_mocks.unlock.assert_not_called()
    await hass.async_block_till_done()


async def test_import_retry_uses_per_lock_cutoff_and_keeps_sibling_credentials_separate(
    hass,
    monkeypatch,
    credential_reader,
    flow_mocks,
):
    """Retries use the first prompt's cutoff; the next lock starts a new history window."""
    now_ms = 1_800_000_000_000
    monkeypatch.setattr(config_flow.time, "time_ns", lambda: now_ms * 1_000_000)
    credential_reader.side_effect = [
        None,
        None,
        UnlockCredential(2, "00001234"),
        UnlockCredential(3, "87654321"),
    ]
    locks = [
        DiscoveredLock(
            LOCK[CONF_DEVICE_ID],
            LOCK[CONF_NODE_ID],
            LOCK[CONF_NAME],
            "HX201",
            True,
            product_id=HX201_PRODUCT_ID,
        ),
        DiscoveredLock(
            "second-lock",
            "secondnode",
            "Second lock",
            "HX201",
            True,
            product_id=HX201_PRODUCT_ID,
        ),
    ]
    result = await cloud_to_selected_locks(hass, monkeypatch, locks)
    assert result["step_id"] == "unlock_import" and not result["errors"]
    assert result["description_placeholders"] == {"lock_name": LOCK[CONF_NAME]}
    now_ms += 20_000
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {},
    )
    assert result["step_id"] == "unlock_import"
    now_ms += 20_000
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {},
    )
    assert result["step_id"] == "lock_ready"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "First cabinet"},
    )
    assert result["step_id"] == "lock_ready"
    assert result["description_placeholders"] == {"lock_name": "Second lock"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Second cabinet"},
    )
    calls = credential_reader.await_args_list
    assert [(call.args[-1], call.kwargs["since_ms"]) for call in calls] == [
        (LOCK[CONF_DEVICE_ID], None),
        (LOCK[CONF_DEVICE_ID], 1_800_000_000_000),
        (LOCK[CONF_DEVICE_ID], 1_800_000_000_000),
        ("second-lock", None),
    ]
    assert [
        (lock[CONF_DEVICE_ID], lock[CONF_NODE_ID], lock[CONF_MEMBER], lock[CONF_REMOTE_CODE])
        for lock in result["data"][CONF_LOCKS]
    ] == [
        (LOCK[CONF_DEVICE_ID], LOCK[CONF_NODE_ID], 2, "00001234"),
        ("second-lock", "secondnode", 3, "87654321"),
    ]
    assert all(
        lock[CONF_STATE_SOURCE] == STATE_SOURCE_HX201_DP40 for lock in result["data"][CONF_LOCKS]
    )
    flow_mocks.unlock.assert_not_called()
    await hass.async_block_till_done()


async def test_automatic_import_retry_has_no_technical_fallback_fields(
    hass, monkeypatch, credential_reader
):
    """Only the explicit advanced entry point exposes manual credentials."""
    credential_reader.side_effect = [None, UnlockCredential(2, "00001234")]
    result = await cloud_to_selected_locks(hass, monkeypatch)
    assert result["step_id"] == "unlock_import" and not result["data_schema"].schema
    for value in (
        {"next_action": "manual"},
        {"next_action": "status_only"},
        {CONF_REMOTE_CODE: "12345678"},
    ):
        with pytest.raises(InvalidData):
            await hass.config_entries.flow.async_configure(result["flow_id"], value)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "lock_ready"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_NAME: LOCK[CONF_NAME]}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()


@pytest.mark.parametrize("structured", [True, False])
async def test_import_error_is_sanitized_and_retains_fallback_choices(
    hass,
    monkeypatch,
    credential_reader,
    structured,
):
    """Neither API failures nor their raw messages reveal credentials in the form."""
    credential_reader.side_effect = CloudError(
        "PRIVATE_RESPONSE",
        **({"stage": "unlock_logs", "reason": "api_error", "code": "1106"} if structured else {}),
    )
    result = await cloud_to_selected_locks(hass, monkeypatch)
    assert result["step_id"] == "unlock_import"
    assert result["errors"] == {
        "base": "cloud_request_failed" if structured else "unlock_import_failed",
    }
    if structured:
        assert result["description_placeholders"]["stage"] == (
            "reading a lock's recent unlock commands"
        )
        assert result["description_placeholders"]["reference"] == "(Tuya error 1106)"
    for secret in ("PRIVATE_RESPONSE", CLOUD["client_id"], CLOUD["client_secret"]):
        assert secret not in repr(result)
    assert result["data_schema"]({}) == {}
    credential_reader.assert_awaited_once()


@pytest.mark.parametrize("imported", [True, False])
async def test_aborting_flow_discards_cloud_access_and_pending_unlock_credentials(
    hass,
    monkeypatch,
    credential_reader,
    imported,
):
    """Both retry and confirmation screens release secrets when their flow is cancelled."""
    credential_reader.return_value = UnlockCredential(2, "00001234") if imported else None
    result = await cloud_to_selected_locks(hass, monkeypatch)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._cloud_access is not None
    assert flow._pending
    hass.config_entries.flow.async_abort(result["flow_id"])
    assert flow._cloud_access is None
    assert not flow._pending and not flow._auto_lock_ids and not flow._import_since_ms
    assert not flow._import_errors and not flow._import_placeholders
    assert hass.config_entries.async_entries(DOMAIN) == []


async def test_manual_identification_does_not_import_without_cloud_verification(
    hass,
    credential_reader,
):
    """Selecting the HX201 state profile alone cannot opt a lock into cloud import."""
    result = await manual_to_credentials(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "HX201",
            CONF_PROFILE: True,
            CONF_MEMBER: 1,
            CONF_STATE_SOURCE: STATE_SOURCE_HX201_DP40,
        },
    )
    assert result["step_id"] == "manage"
    credential_reader.assert_not_awaited()


@pytest.mark.parametrize("late_error", [True, False])
async def test_cancelled_import_does_not_restore_secrets_after_a_late_cloud_reply(
    hass,
    monkeypatch,
    credential_reader,
    late_error,
):
    """A cloud request already in flight cannot restore cleared flow state."""
    result = await cloud_to_selected_locks(hass, monkeypatch)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    requested = asyncio.Event()
    finish_request = asyncio.Event()

    async def slow_read(*args, **kwargs):
        requested.set()
        await finish_request.wait()
        if late_error:
            raise CloudError("PRIVATE_RESPONSE", stage="unlock_logs", reason="timeout")
        return UnlockCredential(3, "00001234")

    credential_reader.side_effect = slow_read
    request = asyncio.create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )
    )
    await requested.wait()
    hass.config_entries.flow.async_abort(result["flow_id"])
    finish_request.set()
    with pytest.raises(UnknownFlow):
        await request
    assert flow._cloud_access is None and not flow._pending
    assert not flow._import_errors and not flow._import_placeholders
    assert not flow._auto_lock_ids and not flow._import_since_ms
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("late_error", [True, False])
async def test_cancelled_discovery_does_not_restore_cloud_access_after_a_late_reply(
    hass,
    monkeypatch,
    late_error,
):
    """Cancelling the first cloud step also prevents later retention of API secrets."""
    requested = asyncio.Event()
    finish_request = asyncio.Event()

    async def slow_discovery(*args):
        requested.set()
        await finish_request.wait()
        if late_error:
            raise CloudError("PRIVATE_RESPONSE", stage="token", reason="timeout")
        return discovered_gateway(
            GATEWAY[CONF_GATEWAY_ID],
            GATEWAY[CONF_NAME],
            GATEWAY[CONF_LOCAL_KEY],
            [],
        )

    monkeypatch.setattr(config_flow, "async_discover_gateway", slow_discovery)
    result = await start(hass, "cloud")
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    request = asyncio.create_task(
        hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    )
    await requested.wait()
    hass.config_entries.flow.async_abort(result["flow_id"])
    finish_request.set()
    with pytest.raises(UnknownFlow):
        await request
    assert flow._cloud_access is None and not flow._auto_lock_ids
    assert CONF_LOCAL_KEY not in flow._data


async def test_verified_product_without_supported_model_cannot_trigger_log_import(
    hass,
    monkeypatch,
    credential_reader,
):
    """An exact product ID does not replace validation of its writable DP61 model."""
    discovery = discovered_gateway(
        GATEWAY[CONF_GATEWAY_ID],
        GATEWAY[CONF_NAME],
        GATEWAY[CONF_LOCAL_KEY],
        [
            DiscoveredLock(
                LOCK[CONF_DEVICE_ID],
                LOCK[CONF_NODE_ID],
                "HX201",
                "HX201",
                False,
                product_id=HX201_PRODUCT_ID,
            )
        ],
    )
    monkeypatch.setattr(config_flow, "async_discover_gateway", AsyncMock(return_value=discovery))
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["step_id"] == "no_locks"
    credential_reader.assert_not_awaited()


@pytest.mark.parametrize(
    "host,local_key,reuse_connection",
    [
        (GATEWAY[CONF_HOST], GATEWAY[CONF_LOCAL_KEY], True),
        ("192.0.2.20", GATEWAY[CONF_LOCAL_KEY], False),
        (GATEWAY[CONF_HOST], "abcdef0123456789", False),
    ],
)
async def test_automatic_reconfigure_keeps_saved_protocol_and_serializes_lan_probe(
    hass, monkeypatch, flow_mocks, host, local_key, reuse_connection
):
    """A known stored version wins over product defaults and never opens a competing socket."""
    config_entry = entry(
        hass, **{CONF_VERSION: "3.4", CONF_AREAS: {LOCK[CONF_DEVICE_ID]: "existing-area"}}
    )
    original = deepcopy(dict(config_entry.data))
    config_entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
    coordinator = MagicMock()
    coordinator.async_probe = AsyncMock(return_value=flow_mocks.probe.return_value)
    config_entry.runtime_data = coordinator
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    monkeypatch.setattr(
        config_flow,
        "async_discover_gateway",
        AsyncMock(
            return_value=discovered_gateway(
                GATEWAY[CONF_GATEWAY_ID], "Cloud gateway name", local_key, []
            )
        ),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {**{key: value for key, value in CLOUD.items() if key != CONF_GATEWAY_ID}, CONF_HOST: host},
    )
    assert result["step_id"] == "gateway_ready"
    assert config_entry.data == original
    coordinator.async_probe.assert_awaited_once_with(flow_mocks, reuse_connection=reuse_connection)
    flow_mocks.probe.assert_not_called()
    config_flow.GatewayClient.assert_called_once_with(
        host, GATEWAY[CONF_GATEWAY_ID], local_key, 3.4
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_LOCKS] == original[CONF_LOCKS]
    assert config_entry.data[CONF_AREAS] == original[CONF_AREAS]
    assert config_entry.data[CONF_VERSION] == "3.4"
    assert config_entry.data[CONF_HOST] == host
    assert config_entry.data[CONF_LOCAL_KEY] == local_key


async def test_empty_cloud_search_keeps_staged_last_lock_removal_and_area_cleanup(
    hass, monkeypatch
):
    """Advanced editing must remain saveable after removing the final lock and searching again."""
    config_entry = entry(hass, **{CONF_AREAS: {LOCK[CONF_DEVICE_ID]: "existing-area"}})
    monkeypatch.setattr(hass.config_entries, "async_reload", AsyncMock(return_value=True))
    monkeypatch.setattr(
        config_flow,
        "async_discover_gateway",
        AsyncMock(
            return_value=discovered_gateway(
                GATEWAY[CONF_GATEWAY_ID], GATEWAY[CONF_NAME], GATEWAY[CONF_LOCAL_KEY], []
            )
        ),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "gateway"}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "remove_lock"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_DEVICE_ID: LOCK[CONF_DEVICE_ID]}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {key: value for key, value in CLOUD.items() if key != CONF_GATEWAY_ID}
    )
    assert result["step_id"] == "no_locks_manual" and result["last_step"] is False
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["step_id"] == "manage"
    assert config_entry.data[CONF_LOCKS] == [LOCK]
    assert config_entry.data[CONF_AREAS] == {LOCK[CONF_DEVICE_ID]: "existing-area"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "finish"}
    )
    assert result["reason"] == "reconfigure_successful"
    assert config_entry.data[CONF_LOCKS] == []
    assert config_entry.data[CONF_AREAS] == {}


async def test_automatic_lan_failure_returns_simple_form_without_retaining_cloud_access(
    hass, monkeypatch, flow_mocks
):
    flow_mocks.probe.side_effect = LocalError("PRIVATE_LOCAL_KEY")
    monkeypatch.setattr(
        config_flow,
        "async_discover_gateway",
        AsyncMock(
            return_value=discovered_gateway(
                GATEWAY[CONF_GATEWAY_ID], GATEWAY[CONF_NAME], GATEWAY[CONF_LOCAL_KEY], []
            )
        ),
    )
    result = await start(hass, "cloud")
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD)
    assert result["step_id"] == "cloud"
    assert result["errors"] == {"base": "cannot_connect_automatic"}
    assert "PRIVATE_LOCAL_KEY" not in repr(result)
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    assert flow._cloud_access is None and CONF_LOCAL_KEY not in flow._data
    assert hass.config_entries.async_entries(DOMAIN) == []


@pytest.mark.parametrize("late_error", [True, False])
async def test_cancelled_automatic_lan_probe_cannot_restore_cloud_or_config_state(
    hass, monkeypatch, flow_mocks, late_error
):
    """Cancellation stays final even when the serialized gateway check finishes later."""
    config_entry = entry(hass)
    config_entry.mock_state(hass, config_entries.ConfigEntryState.LOADED)
    original = deepcopy(dict(config_entry.data))
    requested = asyncio.Event()
    finish_request = asyncio.Event()

    async def probe(*args, **kwargs):
        requested.set()
        await finish_request.wait()
        if late_error:
            raise LocalError("PRIVATE_LOCAL_KEY")
        return flow_mocks.probe.return_value

    coordinator = MagicMock()
    coordinator.async_probe = AsyncMock(side_effect=probe)
    config_entry.runtime_data = coordinator
    monkeypatch.setattr(
        config_flow,
        "async_discover_gateway",
        AsyncMock(
            return_value=discovered_gateway(
                GATEWAY[CONF_GATEWAY_ID], "Changed gateway", "abcdef0123456789", []
            )
        ),
    )
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_RECONFIGURE, "entry_id": config_entry.entry_id},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"next_step_id": "cloud"}
    )
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    request = asyncio.create_task(
        hass.config_entries.flow.async_configure(
            result["flow_id"],
            {key: value for key, value in CLOUD.items() if key != CONF_GATEWAY_ID},
        )
    )
    await requested.wait()
    hass.config_entries.flow.async_abort(result["flow_id"])
    finish_request.set()
    with pytest.raises(UnknownFlow):
        await request
    assert flow._cloud_access is None and not flow._pending
    assert flow._data == original and config_entry.data == original
    assert not flow._auto_lock_ids
