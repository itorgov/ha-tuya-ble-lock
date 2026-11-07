"""Tests for discovery using synthetic metadata and no cloud connections."""

from __future__ import annotations

import base64
import json
import traceback
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs, urlsplit

import pytest
from aiohttp import ClientError, ClientSession, ContentTypeError

from custom_components.tuya_ble_lock import cloud

_LOG_NOW_MS = 1_700_000_000_000


def log_command(member=258, code="00001234", trailer=b"\x00\x01"):
    """Encode an existing synthetic app command, never a live credential."""
    return base64.b64encode(b"\x01" + member.to_bytes(2, "big") + code.encode() + trailer).decode()


def command_log(**changes):
    """Represent an outgoing command in the operation-log response."""
    return {
        "code": "remote_no_dp_key",
        "event_id": 5,
        "event_time": _LOG_NOW_MS - 1000,
        "event_from": "2",
        "status": "1",
        "value": log_command(),
        **changes,
    }


def log_result(*records, **changes):
    """Return a complete synthetic log page for exactly one lock."""
    return {"device_id": "lock-example", "has_next": False, "logs": list(records), **changes}


@pytest.fixture
def unlock_log_time(monkeypatch):
    """Keep credential freshness checks independent of the real clock."""
    monkeypatch.setattr(cloud.time, "time_ns", lambda: _LOG_NOW_MS * 1_000_000)


async def read_credential(session, **changes):
    """Read synthetic command logs without cloud requests."""
    return await cloud.async_read_unlock_credential(
        **{
            "session": session,
            "region": "eu",
            "client_id": "test-client",
            "client_secret": "test-secret",
            "device_id": "lock-example",
            **changes,
        }
    )


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


def model_property(**changes):
    """Return a synthetic writable DP61 property in the modern model format."""
    return {
        "abilityId": 61,
        "code": "remote_no_dp_key",
        "accessMode": "rw",
        "typeSpec": {"type": "raw"},
        **changes,
    }


def model_result(properties=None, **service_changes):
    """Return the JSON-string model envelope used by IoT Core."""
    return {
        "model": json.dumps(
            {
                "services": [
                    {
                        "code": "",
                        "properties": properties if properties is not None else [model_property()],
                        **service_changes,
                    }
                ]
            }
        )
    }


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
        response(model_result()),
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
        "https://openapi.tuyaeu.com/v2.0/cloud/thing/gateway-example",
        "https://openapi.tuyaeu.com/v1.0/devices/gateway-example/sub-devices",
        "https://openapi.tuyaeu.com/v2.0/cloud/thing/lock-example/model",
    ]
    assert requests[0].kwargs["headers"] == {
        "client_id": "test-client",
        "sign": "1A2AD60FCA3C359C4A43D7B8AD76791238DE5D8D11BEFA6C6BFCD07440693BF4",
        "sign_method": "HMAC-SHA256",
        "t": "1700000000000",
    }
    assert requests[1].kwargs["headers"]["sign"] == (
        "DD6DEF7210E87845DEA1EE0CC3D5B20B0E946F8629E656292B6062799533D41A"
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


async def test_smart_home_children_and_iot_core_models_discover_all_four_locks():
    """Follow the verified cloud schemas without requesting legacy Industry APIs."""
    children = [
        {
            "active_time": 1_700_000_000,
            "category": "ms",
            "icon": "smart/example.png",
            "id": f"example-lock-{index}",
            "name": f"Example lock {index}",
            "node_id": f"example-node-{index}",
            "online": index % 2 == 0,
            "owner_id": "synthetic-owner",
            "product_id": "ahz7wpy9",
            "update_time": 1_700_000_100,
        }
        for index in range(4)
    ]
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway(sub=True)),
        response(children),
        *(
            response(model_result([model_property(typeSpec={"type": "raw", "maxlen": 128})]))
            for _ in children
        ),
    )

    result = await discover(session)

    assert [(lock.device_id, lock.node_id, lock.name) for lock in result.locks] == [
        (child["id"], child["node_id"], child["name"]) for child in children
    ]
    assert all(lock.supported and lock.product_id == "ahz7wpy9" for lock in result.locks)
    urls = [call.args[0] for call in session.get.call_args_list]
    assert urls == [
        "https://openapi.tuyaeu.com/v1.0/token?grant_type=1",
        "https://openapi.tuyaeu.com/v2.0/cloud/thing/gateway-example",
        "https://openapi.tuyaeu.com/v1.0/devices/gateway-example/sub-devices",
        *(f"https://openapi.tuyaeu.com/v2.0/cloud/thing/{child['id']}/model" for child in children),
    ]
    assert all("iot-03" not in url and "/specifications" not in url for url in urls)
    session.post.assert_not_called()
    session.put.assert_not_called()
    session.delete.assert_not_called()


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
    with pytest.raises(exception) as error:
        await discover(session, **changes)
    session.get.assert_not_called()
    assert error.value.reason == "invalid_input"
    assert error.value.code is None
    assert error.value.status is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [1001, 1004, 1005, 1010, 1011, 1106, 2406, 28841105])
async def test_auth_errors_are_sanitized(code):
    session = session_with(
        response(payload={"success": False, "code": code, "msg": "private-account-value"})
    )
    with pytest.raises(cloud.CloudAuthError) as error:
        await discover(session)
    assert "private-account-value" not in str(error.value)
    assert (error.value.stage, error.value.reason, error.value.code, error.value.status) == (
        "token",
        "api_error",
        str(code),
        200,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [301, 401, 403, 429, 500])
async def test_http_failure_never_reads_response_body(status):
    raw_response = response(payload={"private": "private-account-value"}, status=status)
    session = session_with(raw_response)
    expected = cloud.CloudAuthError if status in (401, 403) else cloud.CloudError
    with pytest.raises(expected) as error:
        await discover(session)
    raw_response.json.assert_not_called()
    assert (error.value.stage, error.value.reason, error.value.code, error.value.status) == (
        "token",
        "http_error",
        None,
        status,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exception,reason",
    [(ClientError, "connection"), (TimeoutError, "timeout"), (ValueError, "invalid_response")],
)
async def test_network_exceptions_do_not_leak_client_headers(exception, reason):
    session = session_with()
    session.get.side_effect = exception("private-account-value")
    with pytest.raises(cloud.CloudError) as error:
        await discover(session)
    assert "private-account-value" not in "".join(traceback.format_exception(error.value))
    assert (error.value.stage, error.value.reason, error.value.code, error.value.status) == (
        "token",
        reason,
        None,
        None,
    )


@pytest.mark.parametrize("exception", [ValueError, ContentTypeError])
async def test_json_errors_keep_safe_stage_and_http_status(exception):
    raw_response = response()
    raw_response.json.side_effect = (
        ContentTypeError(request_info=MagicMock(), history=(), message="private-account-value")
        if exception is ContentTypeError
        else exception("private-account-value")
    )
    session = session_with(response({"access_token": "test-token"}), raw_response)

    with pytest.raises(cloud.CloudError) as error:
        await discover(session)

    assert (error.value.stage, error.value.reason, error.value.code, error.value.status) == (
        "gateway_details",
        "invalid_json",
        None,
        200,
    )
    assert "private-account-value" not in "".join(traceback.format_exception(error.value))


@pytest.mark.parametrize(
    "stage",
    ["token", "gateway_details", "gateway_children", "child_details", "child_specification"],
)
async def test_api_error_identifies_the_actual_discovery_stage(stage):
    """Use static operation labels without exposing URL paths or child identities."""
    responses = []
    if stage != "token":
        responses.append(response({"access_token": "test-token"}))
    if stage in ("gateway_children", "child_details", "child_specification"):
        responses.append(response(gateway()))
    if stage in ("child_details", "child_specification"):
        responses.append(response([child(name=None) if stage == "child_details" else child()]))
    responses.append(
        response(
            payload={
                "success": False,
                "code": 1106,
                "msg": "private-account-value",
                "result": {"local_key": "another-private-value"},
            }
        )
    )
    session = session_with(*responses)

    with pytest.raises(cloud.CloudAuthError) as error:
        await discover(session)

    assert vars(error.value) == {
        "stage": stage,
        "reason": "api_error",
        "code": "1106",
        "status": 200,
    }
    assert "private" not in repr(error.value)
    assert session.get.call_count == len(responses)


@pytest.mark.parametrize(
    "code",
    [
        28841001,
        "28841002",
        28841003,
        28841004,
        28841101,
        28841102,
        28841103,
        28841104,
        28841106,
        60009001,
        1013,
        1108,
        2010,
        40000901,
        1114,
        1199,
    ],
)
async def test_known_non_auth_error_codes_are_preserved_without_reclassification(code):
    session = session_with(
        response({"access_token": "test-token"}),
        response(payload={"success": False, "code": code, "msg": "private-account-value"}),
    )

    with pytest.raises(cloud.CloudError) as error:
        await discover(session)

    assert type(error.value) is cloud.CloudError
    assert error.value.stage == "gateway_details"
    assert error.value.reason == "api_error"
    assert error.value.code == str(code)
    assert "private-account-value" not in repr(error.value)


@pytest.mark.parametrize(
    "untrusted",
    [
        "private-account-value",
        "1106\n",
        " 1106",
        "１１０６",
        "99999999",
        1106.0,
        True,
        {},
        [],
        10**100,
    ],
)
async def test_untrusted_api_code_is_never_exposed_or_interpreted_as_auth(untrusted):
    session = session_with(
        response(payload={"success": False, "code": untrusted, "msg": "private-account-value"})
    )

    with pytest.raises(cloud.CloudError) as error:
        await discover(session)

    assert type(error.value) is cloud.CloudError
    assert vars(error.value) == {
        "stage": "token",
        "reason": "api_error",
        "code": None,
        "status": 200,
    }
    assert "private-account-value" not in repr(error.value)


@pytest.mark.parametrize("untrusted", ["private-account-value", {}, [], True, 200.0, 99, 600])
def test_exception_diagnostic_fields_reject_untrusted_values(untrusted):
    """Legacy message construction remains valid, while only attributes are UI-safe."""
    error = cloud.CloudError(
        "private-value", stage=untrusted, reason=untrusted, code=untrusted, status=untrusted
    )
    assert vars(error) == {"stage": "unknown", "reason": "unknown", "code": None, "status": None}


def test_legacy_exception_construction_has_safe_unknown_metadata():
    error = cloud.CloudError("private-value")
    assert vars(error) == {"stage": "unknown", "reason": "unknown", "code": None, "status": None}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,reason",
    [
        ([], "invalid_response"),
        ({"success": "true", "result": {}}, "api_error"),
        ({"success": True}, "invalid_response"),
        ({"success": True, "result": {}}, "invalid_token"),
    ],
)
async def test_malformed_auth_metadata_fails_closed(payload, reason):
    session = session_with(response(payload=payload))
    with pytest.raises(cloud.CloudError) as error:
        await discover(session)
    assert error.value.stage == "token"
    assert error.value.reason == reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"local_key": None}, "missing_local_key"),
        ({"local_key": ""}, "missing_local_key"),
        ({"local_key": "too-short"}, "invalid_local_key"),
        ({"local_key": "0123456789abcde\n"}, "invalid_local_key"),
        ({"id": "different-gateway"}, "invalid_gateway"),
    ],
)
async def test_invalid_gateway_metadata_fails_closed(changes, reason):
    session = session_with(response({"access_token": "test-token"}), response(gateway(**changes)))
    with pytest.raises(cloud.CloudError) as error:
        await discover(session)
    assert error.value.stage == "gateway_details"
    assert error.value.reason == reason
    assert error.value.code is None


async def test_gateway_reporting_sub_true_can_proceed_to_local_verification():
    """A cloud classification flag cannot disqualify a functioning gateway."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway(sub=True)),
        response([child()]),
        response(model_result()),
    )

    result = await discover(session)

    assert result.gateway_id == "gateway-example"
    assert result.local_key == "0123456789abcdef"
    assert result.locks[0].supported is True
    assert result.locks[0].node_id == "abcdef012345"
    assert session.get.call_count == 4


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
        response(model_result([model_property(code="switch", typeSpec={"type": "bool"})])),
    )
    result = await discover(session)
    assert result.locks[0].supported is False
    assert result.locks[0].unsupported_reason == "unsupported_profile"


@pytest.mark.parametrize("stage", ["metadata", "specification"])
@pytest.mark.parametrize(
    "failure", ["http", "api", "network", "missing", "offline", "busy", "ambiguous", "malformed"]
)
async def test_child_lookup_failure_preserves_verified_siblings(stage, failure):
    """A stale child cannot prevent setup of verified locks on the same gateway."""
    failed_child = child(id="unverified-child", node_id="secondnode")
    if stage == "metadata":
        failed_child["name"] = None
    failed_response = {
        "http": response(status=500),
        "api": response(payload={"success": False, "code": "unexpected", "msg": "private"}),
        "network": ClientError("private"),
        "missing": response(status=404),
        "offline": response(payload={"success": False, "code": 2001, "msg": "private"}),
        "busy": response(payload={"success": False, "code": 1111, "msg": "private"}),
        "ambiguous": response(payload={"success": False, "code": 2007, "msg": "private"}),
        "malformed": response(payload=[]),
    }[failure]
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(), failed_child, child(id="last-lock", node_id="thirdnode")]),
        response(model_result()),
        failed_response,
        response(model_result()),
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
        "/v2.0/cloud/thing/unverified-child"
        if stage == "metadata"
        else "/v2.0/cloud/thing/unverified-child/model"
    )
    assert session.get.call_args_list[4].args[0].endswith(expected_path)
    assert session.get.call_args_list[5].args[0].endswith("/last-lock/model")
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


@pytest.mark.parametrize("stage", ["child_details", "child_specification"])
@pytest.mark.parametrize(
    "code",
    [
        28841001,
        28841002,
        28841003,
        28841004,
        28841101,
        28841102,
        28841103,
        28841104,
        28841106,
        60009001,
        1108,
        1013,
        1400,
        1114,
    ],
)
async def test_child_service_error_reaches_setup_with_stage_and_code(stage, code):
    """Project/API failures must not masquerade as unsupported lock profiles."""
    selected_child = child(name=None) if stage == "child_details" else child()
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([selected_child, child(id="last-lock", node_id="thirdnode")]),
        response(payload={"success": False, "code": code, "msg": "private-account-value"}),
    )

    with pytest.raises(cloud.CloudError) as error:
        await discover(session)

    assert type(error.value) is cloud.CloudError
    assert vars(error.value) == {
        "stage": stage,
        "reason": "api_error",
        "code": str(code),
        "status": 200,
    }
    assert "private-account-value" not in repr(error.value)
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
    "children,reason",
    [
        ({}, "invalid_children"),
        ([None], "invalid_child"),
        ([child(id="../invalid")], "invalid_child"),
        ([child(id="gateway-example")], "conflicting_children"),
        ([child(), child()], "conflicting_children"),
        ([child(), child(id="another-lock")], "conflicting_children"),
    ],
)
async def test_conflicting_or_malformed_children_fail_closed(children, reason):
    responses = [response({"access_token": "test-token"}), response(gateway()), response(children)]
    if isinstance(children, list) and len(children) > 1:
        responses.append(response(model_result()))
    session = session_with(*responses)
    with pytest.raises(cloud.CloudError) as error:
        await discover(session)
    assert error.value.stage == "gateway_children"
    assert error.value.reason == reason


@pytest.mark.parametrize("access_mode", ["rw", "wr"])
def test_modern_model_supports_exact_writable_raw_dp61(access_mode):
    result = model_result(
        [
            {"abilityId": 8, "code": "battery", "accessMode": "ro", "typeSpec": {"type": "value"}},
            model_property(accessMode=access_mode),
        ]
    )
    assert cloud._supports_model(result) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"abilityId": None},
        {"abilityId": 62},
        {"abilityId": "61"},
        {"abilityId": 61.0},
        {"abilityId": True},
        {"code": "another_code"},
        {"code": None},
        {"accessMode": "ro"},
        {"accessMode": "RW"},
        {"accessMode": None},
        {"typeSpec": None},
        {"typeSpec": "raw"},
        {"typeSpec": {}},
        {"typeSpec": {"type": "Raw"}},
        {"typeSpec": {"type": "string"}},
    ],
)
def test_modern_model_requires_the_exact_property_identity_and_write_access(changes):
    assert cloud._supports_model(model_result([model_property(**changes)])) is False


@pytest.mark.parametrize(
    "properties",
    [
        [],
        {},
        [None],
        [model_property(), None],
        [model_property(), model_property()],
        [model_property(), model_property(abilityId=62)],
        [model_property(), model_property(code="different_property")],
        [model_property(), model_property(abilityId="61", code="different_property")],
    ],
)
def test_modern_model_rejects_invalid_or_conflicting_properties(properties):
    assert cloud._supports_model(model_result(properties)) is False


@pytest.mark.parametrize(
    "services",
    [
        None,
        {},
        [],
        [None],
        [{"properties": [model_property()]}],
        [{"code": "named_service", "properties": [model_property()]}],
        [{"code": "", "properties": None}],
        [{"code": "", "actions": [model_property()], "properties": []}],
        [{"code": "", "events": [model_property()], "properties": []}],
        [
            {"code": "", "properties": [model_property()]},
            {"code": "", "properties": []},
        ],
    ],
)
def test_modern_model_requires_one_default_service_with_properties(services):
    assert cloud._supports_model({"model": json.dumps({"services": services})}) is False


@pytest.mark.parametrize(
    "result",
    [
        None,
        [],
        {},
        {"model": None},
        {"model": {}},
        {"model": "not-json"},
        {"model": "null"},
        {"model": "[]"},
        {"model": "{}"},
        {"model": '{"services": [], "services": []}'},
        {"model": "[" * 2000 + "]" * 2000},
    ],
)
def test_modern_model_rejects_malformed_or_ambiguous_json(result):
    assert cloud._supports_model(result) is False


def test_modern_model_rejects_duplicate_property_fields_in_json():
    result = model_result()
    result["model"] = result["model"].replace('"abilityId": 61', '"abilityId": 62, "abilityId": 61')
    assert cloud._supports_model(result) is False


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
        response(model_result()),
    )
    result = await discover(session)
    assert result.locks[0].name == "Hallway lock"
    assert result.locks[0].product_name == "Example model"
    assert result.locks[0].node_id == "abcdef012345"
    assert result.locks[0].supported is True
    assert session.get.call_args_list[3].args[0].endswith("/v2.0/cloud/thing/lock-example")
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
    with pytest.raises(cloud.CloudError) as error:
        await discover(session)
    assert session.get.call_count == 4
    assert error.value.stage == "child_details"
    assert error.value.reason == "invalid_child"


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
        response(model_result()),
    )
    result = await discover(session)
    lock = result.locks[0]
    assert lock.product_id == "ahz7wpy9"
    assert lock.product_name == "HX201_BLE_LOCKER"
    assert lock.node_id == "abcdef012345"
    assert lock.supported is True
    assert not hasattr(lock, "local_key")
    assert session.get.call_args_list[3].args[0].endswith("/v2.0/cloud/thing/lock-example")


@pytest.mark.parametrize("product_id", [None, 123, {}, "", " ahz7wpy9", "../ahz7wpy9"])
async def test_malformed_product_metadata_cannot_select_a_known_profile(product_id):
    """Keep the ordinary state mapping if optional product metadata is unusable."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway()),
        response([child(product_id=product_id)]),
        response({"id": "lock-example", "product_id": product_id}),
        response(model_result()),
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
        response(model_result()),
    )
    result = await discover(session)
    assert result.locks[0].name == "Tuya device lock-example"


@pytest.mark.parametrize("product_id", ["csvxfq3ecmedidkh", "another-gateway-product"])
async def test_gateway_product_id_is_retained_without_extra_requests_or_protocol_guessing(
    product_id,
):
    """Expose the exact product ID while leaving verified protocol selection to the flow."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway(product_id=product_id, version="untrusted-version")),
        response([]),
    )
    result = await discover(session)
    assert result.product_id == product_id
    assert not hasattr(result, "version")
    assert "untrusted-version" not in repr(result)
    assert session.get.call_count == 3


@pytest.mark.parametrize("product_id", [None, 123, {}, "", " csvxfq3ecmedidkh", "../gateway"])
async def test_invalid_gateway_product_metadata_remains_unknown(product_id):
    """Never normalize malformed metadata into a supposedly known gateway model."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(gateway(product_id=product_id)),
        response([]),
    )
    assert (await discover(session)).product_id is None


def test_gateway_discovery_product_id_is_optional_for_existing_callers():
    assert (
        cloud.GatewayDiscovery("gateway-example", "Gateway", "0123456789abcdef", []).product_id
        is None
    )


async def test_unlock_logs_use_one_signed_read_only_iot_core_request(unlock_log_time, caplog):
    """Read a bounded command-only window and discard all raw response metadata."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(log_result(command_log(), current_row_key="private-cursor")),
    )
    result = await read_credential(session)
    assert result == cloud.UnlockCredential(member=258, code="00001234")
    assert [call.args[0] for call in session.get.call_args_list] == [
        "https://openapi.tuyaeu.com/v1.0/token?grant_type=1",
        "https://openapi.tuyaeu.com/v2.0/cloud/thing/lock-example/logs"
        "?codes=remote_no_dp_key&end_time=1700000000000&query_type=1&size=100"
        "&start_time=1699998200000&type=5",
    ]
    assert session.get.call_args.kwargs["headers"]["access_token"] == "test-token"
    assert session.get.call_args.kwargs["allow_redirects"] is False
    session.post.assert_not_called()
    session.put.assert_not_called()
    session.delete.assert_not_called()
    for private in ("00001234", log_command(), "private-cursor", "test-token", "test-secret"):
        assert private not in repr(result)
        assert private not in caplog.text
    assert not hasattr(result, "payload")
    assert not hasattr(result, "client_secret")


@pytest.mark.parametrize(
    "since_ms,expected_start",
    [
        (None, _LOG_NOW_MS - 1_800_000),
        (0, _LOG_NOW_MS - 1_800_000),
        (_LOG_NOW_MS - 3_600_000, _LOG_NOW_MS - 1_800_000),
        (_LOG_NOW_MS - 30_000, _LOG_NOW_MS - 30_000),
        (_LOG_NOW_MS, _LOG_NOW_MS),
    ],
)
async def test_unlock_log_window_is_bounded_and_retry_cutoff_is_respected(
    unlock_log_time, since_ms, expected_start
):
    session = session_with(response({"access_token": "test-token"}), response(log_result()))
    assert await read_credential(session, since_ms=since_ms) is None
    query = parse_qs(urlsplit(session.get.call_args.args[0]).query)
    assert query["start_time"] == [str(expected_start)]
    assert query["end_time"] == [str(_LOG_NOW_MS)]
    assert session.get.call_count == 2


@pytest.mark.parametrize("since_ms", [-1, True, "1700000000000", 1.5, _LOG_NOW_MS + 1])
async def test_invalid_unlock_log_cutoff_never_contacts_cloud(unlock_log_time, since_ms):
    session = session_with()
    with pytest.raises(cloud.CloudError) as error:
        await read_credential(session, since_ms=since_ms)
    assert error.value.stage == "unlock_logs"
    assert error.value.reason == "invalid_input"
    session.get.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    [
        {"region": "https://example.invalid"},
        {"region": []},
        {"device_id": "../other-lock"},
        {"device_id": "lock?code=private"},
        {"device_id": None},
        {"client_id": ""},
        {"client_secret": "secret\nheader"},
    ],
)
async def test_unlock_log_inputs_are_validated_before_authentication(unlock_log_time, arguments):
    session = session_with()
    with pytest.raises(cloud.CloudError) as error:
        await read_credential(session, **arguments)
    assert error.value.reason == "invalid_input"
    session.get.assert_not_called()


async def test_unlock_log_newest_command_is_selected_without_assuming_order(unlock_log_time):
    """A newer report or unrelated DP must not replace a command credential."""
    session = session_with(
        response({"access_token": "test-token"}),
        response(
            log_result(
                command_log(value=log_command(1, "11111111"), event_time=_LOG_NOW_MS - 3000),
                command_log(event_time=_LOG_NOW_MS - 1000),
                command_log(value=log_command(2, "22222222"), event_time=_LOG_NOW_MS - 2000),
                command_log(event_id=7, value="AAEC", event_time=_LOG_NOW_MS),
                command_log(code="remote_unlock", value="private-record", event_time=_LOG_NOW_MS),
            )
        ),
    )
    assert await read_credential(session) == cloud.UnlockCredential(258, "00001234")


@pytest.mark.parametrize("event_id", [7, "7", 5.0, True, None, "05"])
async def test_only_exact_command_events_can_supply_credentials(unlock_log_time, event_id):
    session = session_with(
        response({"access_token": "test-token"}),
        response(log_result(command_log(event_id=event_id))),
    )
    assert await read_credential(session) is None


@pytest.mark.parametrize("event_id,status", [(5, "1"), ("5", "1"), (5, 1)])
async def test_documented_numeric_and_text_event_fields_are_supported(
    unlock_log_time, event_id, status
):
    session = session_with(
        response({"access_token": "test-token"}),
        response(log_result(command_log(event_id=event_id, status=status))),
    )
    assert await read_credential(session) == cloud.UnlockCredential(258, "00001234")


@pytest.mark.parametrize(
    "newest_changes",
    [
        {"value": "[masked]"},
        {"value": "AAEC"},
        {"value": None},
        {"value": {"private": "00001234"}},
        {"value": log_command(trailer=b"\x01\x00")},
        {"value": log_command(member=0)},
        {"status": "0"},
        {"status": None},
        {"status": True},
        {"status": 1.0},
        {"event_id": 5.0},
        {"event_id": None},
        {"event_id": "05"},
        {"event_time": str(_LOG_NOW_MS - 1000)},
        {"event_time": float(_LOG_NOW_MS - 1000)},
        {"event_time": True},
        {"event_time": None},
        {"event_time": -1},
    ],
)
async def test_newest_invalid_command_never_falls_back_to_older_credential(
    unlock_log_time, newest_changes, caplog
):
    session = session_with(
        response({"access_token": "test-token"}),
        response(
            log_result(
                command_log(event_time=_LOG_NOW_MS - 10_000),
                command_log(**{"event_time": _LOG_NOW_MS - 1000, **newest_changes}),
            )
        ),
    )
    assert await read_credential(session) is None
    assert "00001234" not in caplog.text


async def test_unlock_credentials_remain_independent_for_each_selected_device(unlock_log_time):
    """Never reuse one lock's command or the previous result for another lock."""
    first_session = session_with(
        response({"access_token": "first-token"}),
        response(log_result(command_log(device_id="lock-example"))),
    )
    second_session = session_with(
        response({"access_token": "second-token"}),
        response(
            log_result(
                command_log(device_id="second-lock", value=log_command(17, "12345678")),
                device_id="second-lock",
            )
        ),
    )
    assert await read_credential(first_session) == cloud.UnlockCredential(258, "00001234")
    assert await read_credential(second_session, device_id="second-lock") == cloud.UnlockCredential(
        17, "12345678"
    )
    assert "/second-lock/logs?" in second_session.get.call_args.args[0]
    assert second_session.get.call_args.kwargs["headers"]["access_token"] == "second-token"


@pytest.mark.parametrize("code,member", [("87654321", 258), ("00001234", 259)])
async def test_same_time_conflicting_credentials_are_not_guessed(unlock_log_time, code, member):
    session = session_with(
        response({"access_token": "test-token"}),
        response(log_result(command_log(), command_log(value=log_command(member, code)))),
    )
    assert await read_credential(session) is None


async def test_duplicate_credentials_and_both_source_trailers_are_unambiguous(unlock_log_time):
    session = session_with(
        response({"access_token": "test-token"}),
        response(
            log_result(
                command_log(),
                command_log(value=log_command(trailer=b"\x00\x00")),
                command_log(value="[masked]", event_time=_LOG_NOW_MS - 10_000),
            )
        ),
    )
    assert await read_credential(session) == cloud.UnlockCredential(258, "00001234")


@pytest.mark.parametrize("event_time", [_LOG_NOW_MS - 1_800_001, _LOG_NOW_MS + 1])
async def test_commands_outside_requested_window_are_not_imported(unlock_log_time, event_time):
    session = session_with(
        response({"access_token": "test-token"}),
        response(log_result(command_log(event_time=event_time))),
    )
    assert await read_credential(session) is None


async def test_retry_does_not_reuse_command_before_its_cutoff(unlock_log_time):
    session = session_with(
        response({"access_token": "test-token"}),
        response(log_result(command_log(event_time=_LOG_NOW_MS - 1000))),
    )
    assert await read_credential(session, since_ms=_LOG_NOW_MS - 500) is None


@pytest.mark.parametrize(
    "result,reason",
    [
        (None, "invalid_response"),
        ([], "invalid_response"),
        (log_result(command_log(), device_id="other-lock"), "invalid_response"),
        (log_result(command_log(), device_id=None), "invalid_response"),
        (log_result(command_log(device_id="other-lock")), "invalid_response"),
        (log_result(command_log(device_id=None)), "invalid_response"),
        (log_result(logs={}), "invalid_response"),
        (log_result(logs=None), "invalid_response"),
        (log_result(has_next=None), "invalid_response"),
        (log_result(has_next=0), "invalid_response"),
        (log_result(has_next="false"), "invalid_response"),
        (log_result(command_log(), has_next=True), "incomplete_logs"),
        (log_result(*[command_log()] * 101), "invalid_response"),
        (log_result(command_log(), "private-record"), "invalid_response"),
    ],
)
async def test_invalid_or_truncated_log_page_never_supplies_credentials(
    unlock_log_time, result, reason
):
    session = session_with(response({"access_token": "test-token"}), response(result))
    with pytest.raises(cloud.CloudError) as error:
        await read_credential(session)
    assert error.value.stage == "unlock_logs"
    assert error.value.reason == reason
    assert session.get.call_count == 2
    for private in ("00001234", log_command(), "private-record", "other-lock"):
        assert private not in str(error.value)
        assert private not in repr(error.value)


@pytest.mark.parametrize(
    "code,error_class", [("1106", cloud.CloudAuthError), ("28841002", cloud.CloudError)]
)
async def test_unlock_log_api_failures_preserve_only_safe_diagnostics(
    unlock_log_time, code, error_class, caplog
):
    session = session_with(
        response({"access_token": "test-token"}),
        response(payload={"success": False, "code": code, "msg": "private-code-00001234"}),
    )
    with pytest.raises(error_class) as error:
        await read_credential(session)
    assert error.value.stage == "unlock_logs"
    assert error.value.reason == "api_error"
    assert error.value.code == code
    assert "00001234" not in str(error.value)
    assert "00001234" not in caplog.text
