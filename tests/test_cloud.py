"""Tests for discovery using synthetic metadata and no cloud connections."""

from __future__ import annotations

import traceback
from copy import deepcopy
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import ClientError, ClientSession

from custom_components.tuya_ble_lock import cloud


def response(result=None, *, payload=None, status=200):
    """Make the async context manager returned by aiohttp session.get."""
    result_response = MagicMock()
    result_response.status = status
    result_response.json = AsyncMock(
        return_value=payload if payload is not None else {"success": True, "result": result}
    )
    result_response.__aenter__ = AsyncMock(return_value=result_response)
    result_response.__aexit__ = AsyncMock(return_value=False)
    return result_response


def session_with(*responses):
    """Create a session that only permits the expected GET requests."""
    session = MagicMock(spec=ClientSession)
    session.get.side_effect = list(responses)
    return session


def gateway(**changes):
    """Return synthetic gateway information."""
    return {
        "id": "gateway-example",
        "name": "Example gateway",
        "local_key": "0123456789abcdef",
        "sub": False,
        **changes,
    }


def child(**changes):
    """Return a synthetic child with an independent LAN node ID."""
    return {
        "id": "lock-example",
        "node_id": "abcdef012345",
        "name": "Example lock",
        "product_name": "Example product",
        "product_id": "example-product",
        **changes,
    }


def specification(**changes):
    """Return a supported writable DP profile."""
    return {"functions": [{"dp_id": 61, "code": "remote_no_dp_key", "type": "Raw", **changes}]}


async def discover(session, **changes):
    """Discover only synthetic cloud devices."""
    arguments = {
        "session": session,
        "region": "eu",
        "client_id": "test-client",
        "client_secret": "test-secret",
        "gateway_id": "gateway-example",
        **changes,
    }
    return await cloud.async_discover_gateway(**arguments)


@pytest.mark.asyncio
async def test_discovery_signs_read_only_requests_and_preserves_node_id(monkeypatch):
    """Sign the exact path, include the token only after auth, and keep LAN IDs."""
    monkeypatch.setattr(cloud.time, "time_ns", lambda: 1_700_000_000_000_000_000)
    session = session_with(
        response({"access_token": "test-token", "refresh_token": "discard-me"}),
        response(gateway()),
        response([child()]),
        response(specification()),
    )

    result = await discover(session)

    assert result.gateway_id == "gateway-example"
    assert result.local_key == "0123456789abcdef"
    assert result.name == "Example gateway"
    assert result.locks[0].device_id == "lock-example"
    assert result.locks[0].node_id == "abcdef012345"
    assert result.locks[0].supported is True
    assert result.locks[0].unsupported_reason is None
    assert result.locks[0].product_name == "Example product"
    assert result.locks[0].product_id == "example-product"
    requests = session.get.call_args_list
    assert [request.args[0] for request in requests] == [
        "https://openapi.tuyaeu.com/v1.0/token?grant_type=1",
        "https://openapi.tuyaeu.com/v1.1/iot-03/devices/gateway-example",
        "https://openapi.tuyaeu.com/v1.0/iot-03/devices/gateway-example/sub-devices",
        "https://openapi.tuyaeu.com/v1.1/devices/lock-example/specifications",
    ]
    assert requests[0].kwargs["headers"] == {
        "client_id": "test-client",
        "sign": "1A2AD60FCA3C359C4A43D7B8AD76791238DE5D8D11BEFA6C6BFCD07440693BF4",
        "sign_method": "HMAC-SHA256",
        "t": "1700000000000",
    }
    assert requests[1].kwargs["headers"]["sign"] == (
        "735143E03A361564B246A09599D4A73B4668FF718B3FFA5131FC8805B8F2E0D3"
    )
    assert requests[1].kwargs["headers"]["access_token"] == "test-token"
    assert all(request.kwargs["allow_redirects"] is False for request in requests)
    assert all(request.kwargs["timeout"].total == 15 for request in requests)
    session.post.assert_not_called()
    session.put.assert_not_called()
    session.delete.assert_not_called()
    assert "0123456789abcdef" not in repr(result)
    assert "gateway-example" not in repr(result)
    assert "lock-example" not in repr(result.locks[0])
    assert not hasattr(result, "access_token")
    assert not hasattr(result, "client_secret")


@pytest.mark.asyncio
@pytest.mark.parametrize("region,endpoint", list(cloud.REGION_ENDPOINTS.items()))
async def test_regions_are_fixed_https_endpoints(region, endpoint):
    """Do not send project credentials to an arbitrary user-provided URL."""
    session = session_with(
        response({"access_token": "test-token"}), response(gateway()), response([])
    )
    result = await discover(session, region=region)
    assert result.locks == []
    assert all(call.args[0].startswith(endpoint + "/") for call in session.get.call_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,exception",
    [
        ({"region": "https://example.invalid"}, cloud.CloudError),
        ({"gateway_id": "../another-device"}, cloud.CloudError),
        ({"gateway_id": "device?overwrite=true"}, cloud.CloudError),
        ({"client_id": "test\r\nheader"}, cloud.CloudAuthError),
        ({"client_secret": ""}, cloud.CloudAuthError),
    ],
)
async def test_invalid_input_makes_no_requests(changes, exception):
    session = session_with()
    with pytest.raises(exception):
        await discover(session, **changes)
    session.get.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1001, 1004, 1005, 1010, 1011, 1106, 2406, 28841105])
async def test_auth_errors_are_sanitized(code):
    session = session_with(
        response(payload={"success": False, "code": code, "msg": "private-account-value"})
    )
    with pytest.raises(cloud.CloudAuthError) as error:
        await discover(session)
    assert "private-account-value" not in str(error.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
async def test_http_failure_never_reads_response_body(status):
    raw_response = response(payload={"private": "private-account-value"}, status=status)
    session = session_with(raw_response)
    expected = cloud.CloudAuthError if status in (401, 403) else cloud.CloudError
    with pytest.raises(expected):
        await discover(session)
    raw_response.json.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("exception", [ClientError, TimeoutError, ValueError])
async def test_network_exceptions_do_not_leak_client_headers(exception):
    session = session_with()
    session.get.side_effect = exception("private-account-value")
    with pytest.raises(cloud.CloudError) as error:
        await discover(session)
    assert "private-account-value" not in "".join(traceback.format_exception(error.value))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [[], {"success": "true", "result": {}}, {"success": True}, {"success": True, "result": {}}],
)
async def test_malformed_auth_metadata_fails_closed(payload):
    session = session_with(response(payload=payload))
    with pytest.raises(cloud.CloudError):
        await discover(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"local_key": None},
        {"local_key": "too-short"},
        {"local_key": "0123456789abcde\n"},
        {"id": "different-gateway"},
        {"sub": True},
    ],
)
async def test_invalid_gateway_metadata_fails_closed(changes):
    session = session_with(response({"access_token": "test-token"}), response(gateway(**changes)))
    with pytest.raises(cloud.CloudError):
        await discover(session)


@pytest.mark.asyncio
async def test_missing_node_id_is_not_replaced_with_cloud_device_id():
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(node_id=None)]),
    )
    result = await discover(session)
    assert result.locks[0].node_id == ""
    assert result.locks[0].supported is False
    assert result.locks[0].unsupported_reason == "missing_node_id"
    assert session.get.call_count == 3


@pytest.mark.asyncio
async def test_other_children_are_not_automatically_classified_as_supported_locks():
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(category="dj", name="Example bulb")]),
        response({"functions": [{"dp_id": 61, "code": "switch", "type": "Boolean"}]}),
    )
    result = await discover(session)
    assert result.locks[0].supported is False
    assert result.locks[0].unsupported_reason == "unsupported_profile"


@pytest.mark.parametrize("stage", ["metadata", "specification"])
@pytest.mark.parametrize("failure", ["http", "api", "network"])
async def test_child_lookup_failure_preserves_verified_siblings(stage, failure):
    """A stale child cannot prevent setup of verified locks on the same gateway."""
    failed_child = child(id="unverified-child", node_id="secondnode")
    if stage == "metadata":
        failed_child["name"] = None
    failed_response = {
        "http": response(status=500),
        "api": response(payload={"success": False, "code": "unexpected", "msg": "private"}),
        "network": ClientError("private"),
    }[failure]
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(), failed_child, child(id="last-lock", node_id="thirdnode")]),
        response(specification()),
        failed_response,
        response(specification()),
    )

    result = await discover(session)

    assert result.local_key == "0123456789abcdef"
    assert [(lock.device_id, lock.node_id, lock.supported) for lock in result.locks] == [
        ("lock-example", "abcdef012345", True),
        ("unverified-child", "secondnode", False),
        ("last-lock", "thirdnode", True),
    ]
    assert result.locks[1].unsupported_reason == f"{stage}_unavailable"
    expected_path = (
        "/v1.1/iot-03/devices/unverified-child"
        if stage == "metadata"
        else "/v1.1/devices/unverified-child/specifications"
    )
    assert session.get.call_args_list[4].args[0].endswith(expected_path)
    assert session.get.call_args_list[5].args[0].endswith("/last-lock/specifications")
    assert session.get.call_count == 6


@pytest.mark.parametrize("stage", ["metadata", "specification"])
@pytest.mark.parametrize("failure", ["http", "api"])
async def test_child_auth_failure_still_aborts_discovery(stage, failure):
    """Expired credentials or denied API permission apply to the whole discovery."""
    selected_child = child(name=None) if stage == "metadata" else child()
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([selected_child, child(id="last-lock", node_id="thirdnode")]),
        response(status=403)
        if failure == "http"
        else response(payload={"success": False, "code": 1010, "msg": "private"}),
    )

    with pytest.raises(cloud.CloudAuthError):
        await discover(session)

    assert session.get.call_count == 4


@pytest.mark.parametrize("stage", ["gateway", "topology"])
async def test_gateway_and_topology_lookup_failures_still_abort(stage):
    """Partial child recovery requires a verified gateway and child inventory."""
    responses = [response({"access_token": "test-token"})]
    if stage == "topology":
        responses.append(response(gateway()))
    responses.append(response(status=500))
    session = session_with(*responses)

    with pytest.raises(cloud.CloudError):
        await discover(session)

    assert session.get.call_count == len(responses)


async def test_unverified_child_cannot_hide_a_conflicting_node_id():
    """A failed optional lookup must not bypass routing identity validation."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(name=None), child(id="different-lock")]),
        response(status=500),
    )

    with pytest.raises(cloud.CloudError, match="conflicting child node IDs"):
        await discover(session)

    assert session.get.call_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "children",
    [
        {},
        [None],
        [child(id="../invalid")],
        [child(id="gateway-example")],
        [child(), child()],
        [child(), child(id="another-lock")],
    ],
)
async def test_conflicting_or_malformed_children_fail_closed(children):
    responses = [response({"access_token": "test-token"}), response(gateway()), response(children)]
    if isinstance(children, list) and len(children) > 1:
        responses.append(response(specification()))
    session = session_with(*responses)
    with pytest.raises(cloud.CloudError):
        await discover(session)


@pytest.mark.parametrize(
    "specification_value",
    [
        None,
        [],
        {},
        {"functions": []},
        {"functions": [None]},
        {"status": specification()["functions"]},
        specification(dp_id=None),
        specification(dp_id=49),
        specification(dp_id=61.0),
        specification(dp_id=True),
        specification(code="another_code"),
        specification(type="String"),
        specification(dpId=49),
        {"functions": specification()["functions"] * 2},
    ],
)
def test_unknown_or_conflicting_dp_profiles_are_unsupported(specification_value):
    assert cloud._supports_unlock(specification_value) is False


@pytest.mark.parametrize("dp_field", ["dp_id", "dpId"])
@pytest.mark.parametrize("dp_id", [61, "61"])
def test_documented_dp_id_spellings_are_supported(dp_field, dp_id):
    value = deepcopy(specification())
    value["functions"][0].pop("dp_id")
    value["functions"][0][dp_field] = dp_id
    assert cloud._supports_unlock(value) is True


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_name", [None, "", "   ", 123])
async def test_missing_child_name_is_loaded_from_verified_device_details(missing_name):
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(name=missing_name)]),
        response(
            {
                "id": "lock-example",
                "name": "Hallway lock",
                "product_name": "Example model",
                "node_id": "not-used-for-routing",
                "local_key": "discard-this-secret",
            }
        ),
        response(specification()),
    )
    result = await discover(session)
    assert result.locks[0].name == "Hallway lock"
    assert result.locks[0].product_name == "Example model"
    assert result.locks[0].node_id == "abcdef012345"
    assert result.locks[0].supported is True
    assert session.get.call_args_list[3].args[0].endswith("/v1.1/iot-03/devices/lock-example")
    assert not hasattr(result.locks[0], "local_key")


@pytest.mark.asyncio
@pytest.mark.parametrize("details", [None, [], {}, {"id": "different-lock", "name": "Wrong"}])
async def test_child_details_must_identify_requested_device(details):
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(name=None)]),
        response(details),
    )
    with pytest.raises(cloud.CloudError):
        await discover(session)
    assert session.get.call_count == 4


async def test_named_child_with_missing_product_id_reads_verified_details():
    """A friendly name is insufficient to select a product-specific state mapping."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(product_id=None)]),
        response(
            {
                "id": "lock-example",
                "name": "Example lock",
                "product_id": "ahz7wpy9",
                "product_name": "HX201_BLE_LOCKER",
                "node_id": "not-used-for-routing",
                "local_key": "discard-this-secret",
            }
        ),
        response(specification()),
    )
    result = await discover(session)
    lock = result.locks[0]
    assert lock.product_id == "ahz7wpy9"
    assert lock.product_name == "HX201_BLE_LOCKER"
    assert lock.node_id == "abcdef012345"
    assert lock.supported is True
    assert not hasattr(lock, "local_key")
    assert session.get.call_args_list[3].args[0].endswith("/v1.1/iot-03/devices/lock-example")


@pytest.mark.parametrize("product_id", [None, 123, {}, "", " ahz7wpy9", "../ahz7wpy9"])
async def test_malformed_product_metadata_cannot_select_a_known_profile(product_id):
    """Keep the ordinary state mapping if optional product metadata is unusable."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(product_id=product_id)]),
        response({"id": "lock-example", "product_id": product_id}),
        response(specification()),
    )
    result = await discover(session)
    assert result.locks[0].product_id is None
    assert result.locks[0].supported is True
    assert session.get.call_count == 5


@pytest.mark.asyncio
async def test_child_without_any_name_uses_unambiguous_fallback():
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(name=None)]),
        response({"id": "lock-example"}),
        response(specification()),
    )
    result = await discover(session)
    assert result.locks[0].name == "Tuya device lock-example"
