"""Exercise the automatic setup screens and real registry handoff offline."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant import config_entries
from homeassistant.const import ATTR_AREA_ID as CONF_AREA_ID
from homeassistant.const import CONF_DEVICE_ID, CONF_HOST, CONF_NAME
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr

from custom_components.tuya_ble_lock import config_flow
from custom_components.tuya_ble_lock.cloud import DiscoveredLock, GatewayDiscovery
from custom_components.tuya_ble_lock.const import (
    CONF_AREAS,
    CONF_GATEWAY_ID,
    CONF_LOCAL_KEY,
    CONF_LOCKS,
    CONF_MEMBER,
    CONF_NODE_ID,
    CONF_REMOTE_CODE,
    CONF_STATE_SOURCE,
    CONF_VERSION,
    DOMAIN,
    HX201_PRODUCT_ID,
    STATE_SOURCE_HX201_DP40,
)
from custom_components.tuya_ble_lock.credentials import UnlockCredential
from custom_components.tuya_ble_lock.local import LockStatus, Topology

GATEWAY_ID = "automatic_gateway"
GATEWAY_PRODUCT_ID = "csvxfq3ecmedidkh"
LOCAL_KEY = "0123456789abcdef"
CLOUD_INPUT = {
    "region": "eu",
    "client_id": "synthetic-client",
    "client_secret": "synthetic-secret",
    CONF_GATEWAY_ID: GATEWAY_ID,
    CONF_HOST: "192.0.2.42",
}


@pytest.fixture
def automatic_cloud(monkeypatch, mock_gateway):
    """Supply two supported locks while all cloud and gateway operations remain mocked."""
    locks = [
        DiscoveredLock(
            "first_lock",
            "first_node",
            "First Tuya name",
            "HX201",
            True,
            product_id=HX201_PRODUCT_ID,
        ),
        DiscoveredLock(
            "second_lock",
            "second_node",
            "Second Tuya name",
            "HX201",
            True,
            product_id=HX201_PRODUCT_ID,
        ),
    ]
    discover = AsyncMock(
        return_value=GatewayDiscovery(
            GATEWAY_ID,
            "Automatic gateway",
            LOCAL_KEY,
            locks,
            product_id=GATEWAY_PRODUCT_ID,
        )
    )
    credentials = {
        "first_lock": UnlockCredential(258, "00001234"),
        "second_lock": UnlockCredential(19, "87654321"),
    }

    async def read_credential(session, region, client_id, client_secret, device_id, **kwargs):
        return credentials[device_id]

    read = AsyncMock(side_effect=read_credential)
    monkeypatch.setattr(config_flow, "async_discover_gateway", discover)
    monkeypatch.setattr(config_flow, "async_read_unlock_credential", read)
    config_flow.GatewayClient.probe.return_value = Topology(
        frozenset({"first_node", "second_node"}),
        frozenset(),
    )
    mock_gateway["poll"].return_value = {
        "first_node": LockStatus(True, 100, True),
        "second_node": LockStatus(True, 72, True),
    }
    return SimpleNamespace(discover=discover, read=read, credentials=credentials)


def form_fields(result):
    """Return field names without inspecting secret values."""
    return {str(marker.schema) for marker in result["data_schema"].schema}


async def cloud_form(hass):
    """Choose the automatic route offered by the initial setup menu."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": config_entries.SOURCE_USER},
    )
    assert result["type"] is FlowResultType.MENU
    return await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {"next_step_id": "cloud"},
    )


async def selected_locks(hass, ids):
    """Go directly from cloud connection details to the selected lock's naming screen."""
    result = await cloud_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_INPUT)
    assert result["step_id"] == "select_locks"
    assert form_fields(result) == {CONF_LOCKS}
    return await hass.config_entries.flow.async_configure(result["flow_id"], {CONF_LOCKS: ids})


async def test_initial_cloud_form_requests_only_account_gateway_and_ip(hass, automatic_cloud):
    result = await cloud_form(hass)
    assert result["step_id"] == "cloud"
    assert form_fields(result) == {
        "region",
        "client_id",
        "client_secret",
        CONF_GATEWAY_ID,
        CONF_HOST,
    }
    assert CONF_LOCAL_KEY not in form_fields(result)
    assert CONF_VERSION not in form_fields(result)
    assert result["data_schema"](CLOUD_INPUT) == CLOUD_INPUT
    automatic_cloud.discover.assert_not_awaited()
    automatic_cloud.read.assert_not_awaited()


async def test_two_lock_automatic_setup_saves_directly_with_independent_names_and_areas(
    hass,
    automatic_cloud,
    mock_gateway,
):
    """The final name/area submission creates the entry without a technical or Manage screen."""
    areas = ar.async_get(hass)
    kitchen = areas.async_create("Kitchen")
    garage = areas.async_create("Garage")
    result = await selected_locks(hass, ["first_lock", "second_lock"])
    assert result["step_id"] == "lock_ready"
    assert form_fields(result) == {CONF_NAME, CONF_AREA_ID}
    assert not result["last_step"]
    assert result["description_placeholders"] == {"lock_name": "First Tuya name"}
    for secret in ("00001234", LOCAL_KEY, CLOUD_INPUT["client_secret"]):
        assert secret not in repr(result)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Kitchen cabinet", CONF_AREA_ID: kitchen.id},
    )
    assert result["step_id"] == "lock_ready"
    assert form_fields(result) == {CONF_NAME, CONF_AREA_ID}
    assert result["last_step"]
    assert result["description_placeholders"] == {"lock_name": "Second Tuya name"}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not dr.async_get(hass).devices
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Garage cabinet", CONF_AREA_ID: garage.id},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_AREAS] == {"first_lock": kitchen.id, "second_lock": garage.id}
    assert result["data"][CONF_LOCKS] == [
        {
            CONF_DEVICE_ID: "first_lock",
            CONF_NODE_ID: "first_node",
            CONF_NAME: "Kitchen cabinet",
            CONF_MEMBER: 258,
            CONF_REMOTE_CODE: "00001234",
            CONF_STATE_SOURCE: STATE_SOURCE_HX201_DP40,
        },
        {
            CONF_DEVICE_ID: "second_lock",
            CONF_NODE_ID: "second_node",
            CONF_NAME: "Garage cabinet",
            CONF_MEMBER: 19,
            CONF_REMOTE_CODE: "87654321",
            CONF_STATE_SOURCE: STATE_SOURCE_HX201_DP40,
        },
    ]
    assert result["data"][CONF_VERSION] == "3.5"
    assert result["data"][CONF_LOCAL_KEY] == LOCAL_KEY
    assert "client_id" not in result["data"] and "client_secret" not in result["data"]
    assert [call.args[-1] for call in automatic_cloud.read.await_args_list] == [
        "first_lock",
        "second_lock",
    ]
    assert all(call.kwargs == {"since_ms": None} for call in automatic_cloud.read.await_args_list)
    await hass.async_block_till_done()
    entry = result["result"]
    assert entry.state is config_entries.ConfigEntryState.LOADED
    devices = dr.async_get(hass)
    for lock_id, area_id in result["data"][CONF_AREAS].items():
        device = devices.async_get_device_by_identifier((DOMAIN, lock_id), entry.entry_id)
        assert device is not None and device.area_id == area_id
    mock_gateway["unlock"].assert_not_called()


@pytest.mark.parametrize(
    "name,area_id,errors",
    [
        ("   ", None, {CONF_NAME: "invalid_name"}),
        ("Cabinet", "deleted-area", {CONF_AREA_ID: "invalid_area"}),
        ("   ", "deleted-area", {CONF_NAME: "invalid_name", CONF_AREA_ID: "invalid_area"}),
    ],
)
async def test_name_and_area_errors_retry_without_importing_or_saving_again(
    hass,
    automatic_cloud,
    mock_gateway,
    name,
    area_id,
    errors,
):
    result = await selected_locks(hass, ["first_lock"])
    values = {CONF_NAME: name}
    if area_id is not None:
        values[CONF_AREA_ID] = area_id
    result = await hass.config_entries.flow.async_configure(result["flow_id"], values)
    assert result["step_id"] == "lock_ready" and result["errors"] == errors
    assert form_fields(result) == {CONF_NAME, CONF_AREA_ID}
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not dr.async_get(hass).devices
    automatic_cloud.read.assert_awaited_once()
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Corrected cabinet"},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LOCKS][0][CONF_NAME] == "Corrected cabinet"
    assert result["data"][CONF_LOCKS][0][CONF_REMOTE_CODE] == "00001234"
    assert not result["data"].get(CONF_AREAS)
    automatic_cloud.read.assert_awaited_once()
    mock_gateway["unlock"].assert_not_called()
    await hass.async_block_till_done()


async def test_cancelling_after_first_named_lock_has_no_registry_or_area_side_effects(
    hass,
    automatic_cloud,
    mock_gateway,
):
    areas = ar.async_get(hass)
    room = areas.async_create("Existing room")
    original_areas = {area.id: area for area in areas.async_list_areas()}
    result = await selected_locks(hass, ["first_lock", "second_lock"])
    flow = hass.config_entries.flow._progress[result["flow_id"]]
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_NAME: "Staged cabinet", CONF_AREA_ID: room.id},
    )
    assert result["step_id"] == "lock_ready"
    assert len(flow._data[CONF_LOCKS]) == 1
    assert flow._data[CONF_AREAS] == {"first_lock": room.id}
    hass.config_entries.flow.async_abort(result["flow_id"])
    await hass.async_block_till_done()
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not dr.async_get(hass).devices
    assert {area.id: area for area in areas.async_list_areas()} == original_areas
    assert flow._cloud_access is None and not flow._pending
    mock_gateway["poll"].assert_not_called()
    mock_gateway["unlock"].assert_not_called()


@pytest.mark.parametrize("product_id", [None, "unknown-gateway-product"])
async def test_unknown_gateway_requires_manual_setup_before_any_local_probe(
    hass,
    automatic_cloud,
    mock_gateway,
    product_id,
):
    """An unsupported gateway cannot acquire a guessed LAN protocol version."""
    automatic_cloud.discover.return_value = GatewayDiscovery(
        GATEWAY_ID,
        "Other gateway",
        LOCAL_KEY,
        [],
        product_id=product_id,
    )
    result = await cloud_form(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], CLOUD_INPUT)
    assert result["step_id"] == "cloud"
    assert result["errors"] == {"base": "automatic_gateway_unsupported"}
    assert form_fields(result) == {
        "region",
        "client_id",
        "client_secret",
        CONF_GATEWAY_ID,
        CONF_HOST,
    }
    config_flow.GatewayClient.probe.assert_not_called()
    automatic_cloud.read.assert_not_awaited()
    mock_gateway["unlock"].assert_not_called()
    assert not hass.config_entries.async_entries(DOMAIN)
    assert not dr.async_get(hass).devices
