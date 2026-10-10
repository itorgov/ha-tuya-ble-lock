"""Offline LAN contract tests; no test opens a connection to real hardware."""

import base64
import json
import logging
import socket
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import tinytuya
from tinytuya.core.XenonDevice import XenonDevice

from custom_components.tuya_ble_lock import local


@pytest.fixture
def lock_config():
    """Use synthetic identifiers and a synthetic remote code only."""
    return local.LockConfig("lock_example", "node_example", "Example lock", "12345678")


@pytest.fixture
def client():
    return local.GatewayClient("192.0.2.1", "gateway_example", "0123456789abcdef", 3.5)


@pytest.fixture
def devices(monkeypatch):
    """Replace every vendor Device before a client can connect."""
    gateway = SimpleNamespace(
        subdev_query=Mock(
            return_value={
                "reqType": "subdev_online_stat_report",
                "data": {"online": ["node_example"], "offline": []},
            }
        ),
        set_socketRetryLimit=Mock(),
        socket=Mock(),
        cmd_retcode=0,
        close=Mock(),
        _receive=Mock(side_effect=socket.timeout),
        _decode_payload=Mock(),
    )
    child = SimpleNamespace(
        generate_payload=Mock(return_value=b"synthetic_packet"),
        _send_receive_quick=Mock(return_value=True),
    )
    factory = Mock(side_effect=lambda *args, **kwargs: child if "parent" in kwargs else gateway)
    monkeypatch.setattr(local.tinytuya, "Device", factory)
    monkeypatch.setattr(local.select, "select", Mock(return_value=([], [], [])))
    return gateway, child, factory


def responses(gateway, *values):
    gateway._receive.side_effect = [
        *[SimpleNamespace(payload=b"synthetic_response", retcode=0) for _ in values],
        TimeoutError(),
    ]
    gateway._decode_payload.side_effect = values


def test_exact_dp61_packet(lock_config):
    assert local.build_unlock(lock_config.member, lock_config.remote_unlock_code) == bytes.fromhex(
        "01000131323334353637380000"
    )
    assert "12345678" not in repr(lock_config)


def test_unverified_vendor_version_rejected_before_device_construction(monkeypatch):
    monkeypatch.setattr(local.tinytuya, "__version__", "0.0.0")
    factory = Mock()
    monkeypatch.setattr(local.tinytuya, "Device", factory)
    with pytest.raises(local.LocalError, match="pinned TinyTuya"):
        local.GatewayClient("192.0.2.1", "gateway_example", "0123456789abcdef", 3.5)
    factory.assert_not_called()


@pytest.mark.parametrize(
    "member,code",
    [
        (True, "12345678"),
        (0, "12345678"),
        (65535, "12345678"),
        (1, None),
        (1, "１２３４５６７８"),
        (1, "1234567"),
        (1, "secret!!"),
    ],
)
def test_invalid_code_or_member_never_opens_session(client, devices, member, code):
    _, child, factory = devices
    with pytest.raises(local.UnlockError):
        client.unlock(local.LockConfig("lock_example", "node_example", "Lock", code, member))
    factory.assert_not_called()
    child._send_receive_quick.assert_not_called()


def test_probe_is_read_only_and_closes(client, devices):
    gateway, child, _ = devices
    assert client.probe() == local.Topology(frozenset({"node_example"}), frozenset())
    gateway.subdev_query.assert_called_once_with()
    gateway.set_socketRetryLimit.assert_called_once_with(0)
    child._send_receive_quick.assert_not_called()
    gateway.close.assert_called_once_with()
    client.close()
    gateway.close.assert_called_once_with()


@pytest.mark.parametrize(
    "report",
    [
        None,
        {"Error": "private detail"},
        {
            "reqType": "subdev_online_stat_report",
            "data": {"online": ["node_example"], "offline": ["node_example"]},
        },
        {
            "reqType": "subdev_online_stat_report",
            "data": {"online": [{"cid": "node_example"}]},
        },
        {"reqType": "wrong", "data": {"online": ["node_example"]}},
    ],
)
def test_malformed_topology_prevents_unlock(client, devices, lock_config, report):
    gateway, child, _ = devices
    gateway.subdev_query.return_value = report
    with pytest.raises(local.CannotConnect):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()
    gateway.close.assert_called_once()


def test_wrong_child_prevents_unlock(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.subdev_query.return_value["data"] = {"online": ["another_node"]}
    with pytest.raises(local.NodeUnavailable):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()


def test_disconnected_after_topology_prevents_unlock(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.socket = None
    with pytest.raises(local.UnlockError, match="no command was sent"):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()


def test_queued_frames_are_drained_before_single_unlock(client, devices, lock_config, monkeypatch):
    gateway, child, _ = devices
    # An old success frame must be discarded and cannot satisfy this command.
    monkeypatch.setattr(
        local.select,
        "select",
        Mock(side_effect=[([gateway.socket], [], []), ([], [], [])]),
    )
    gateway._receive.side_effect = [
        SimpleNamespace(payload=b"old", retcode=0),
        TimeoutError(),
    ]
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    gateway._decode_payload.assert_not_called()
    child._send_receive_quick.assert_called_once_with(b"synthetic_packet", recv_retries=0)


@pytest.mark.parametrize("send_result", [None, False, "True"])
def test_ambiguous_send_never_retries(client, devices, lock_config, send_result):
    gateway, child, _ = devices
    child._send_receive_quick.return_value = send_result
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once_with(b"synthetic_packet", recv_retries=0)
    gateway._receive.assert_not_called()
    gateway.close.assert_called_once()


def test_send_exception_sanitized_and_never_retried(client, devices, lock_config):
    _, child, _ = devices
    child._send_receive_quick.side_effect = RuntimeError("12345678 PRIVATE KEY")
    with pytest.raises(local.UnlockOutcomeUnknown) as exc:
        client.unlock(lock_config)
    assert "12345678" not in str(exc.value)
    assert "PRIVATE" not in str(exc.value)
    assert exc.value.__suppress_context__ is False
    child._send_receive_quick.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        {"cid": "different_node", "dps": {"61": "AAAB"}},
        {
            "cid": "node_example",
            "data": {"cid": "different_node"},
            "dps": {"61": "AAAB"},
        },
        {"dps": {"61": "AAAB"}},
        {"cid": "node_example", "dps": {"40": True, "47": True}},
        {"cid": "node_example", "Error": "error", "dps": {"61": "AAAB"}},
    ],
)
def test_wrong_child_or_motor_status_never_confirms_unlock(client, devices, lock_config, response):
    gateway, child, _ = devices
    responses(gateway, response)
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once()


def test_matching_response_returns_without_physical_state(client, devices, lock_config):
    gateway, child, _ = devices
    responses(gateway, {"data": {"cid": "node_example", "dps": {"61": "AAAB"}}})
    assert client.unlock(lock_config) is None
    child.generate_payload.assert_called_once_with(tinytuya.CONTROL, {"61": "AQABMTIzNDU2NzgAAA=="})
    child._send_receive_quick.assert_called_once()
    gateway.close.assert_called_once()


@pytest.mark.parametrize(
    "value,exception",
    [
        ("AQAB", local.UnlockRejected),
        ("not base64!", local.UnlockOutcomeUnknown),
        (123, local.UnlockOutcomeUnknown),
    ],
)
def test_unexpected_dp61_never_retries(client, devices, lock_config, value, exception):
    gateway, child, _ = devices
    responses(gateway, {"cid": "node_example", "dps": {"61": value}})
    with pytest.raises(exception):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once()


def test_multiple_children_and_partial_status(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.subdev_query.return_value["data"] = {
        "online": ["node_example", "second_node"],
        "offline": ["offline_node"],
    }
    responses(
        gateway,
        {"cid": "second_node", "dps": {"8": 72}},
        {"cid": "node_example", "dps": {"47": True}},
        {"cid": "node_example", "dps": {"8": 63}},
        {"cid": "node_example", "dps": {"40": False}},
        {"cid": "unconfigured_node", "dps": {"8": 99}},
    )
    result = client.poll(
        [
            lock_config,
            local.LockConfig("lock_two", "second_node", "Second"),
            local.LockConfig("lock_offline", "offline_node", "Offline"),
            local.LockConfig("lock_unknown", "unknown_node", "Unknown"),
        ]
    )
    assert result == {
        "node_example": local.LockStatus(True, 63),
        "second_node": local.LockStatus(True, 72),
        "offline_node": local.LockStatus(False),
        "unknown_node": local.LockStatus(False),
    }
    assert child._send_receive_quick.call_count == 2
    assert all(call.args[0] == tinytuya.DP_QUERY for call in child.generate_payload.call_args_list)


@pytest.mark.parametrize(
    "response",
    [
        {"Error": "not supported"},
        {"cid": "node_example", "dps": {"8": True}},
        {"cid": "node_example", "dps": {"8": 101}},
        {"cid": "node_example", "dps": {"8": "50"}},
        {"cid": "node_example", "data": {"cid": "different_node"}, "dps": {"8": 50}},
    ],
)
def test_unsupported_or_invalid_status_preserves_online_topology(
    client, devices, lock_config, response
):
    gateway, _, _ = devices
    responses(gateway, response)
    assert client.poll([lock_config]) == {"node_example": local.LockStatus(True)}


def test_gateway_exception_sanitized(client, devices):
    gateway, _, _ = devices
    gateway.subdev_query.side_effect = RuntimeError("sensitive_identifier secret_local_key")
    with pytest.raises(local.CannotConnect) as exc:
        client.probe()
    assert "sensitive" not in str(exc.value)
    assert "secret" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_vendor_logs_suppressed_only_in_our_session(client, devices, caplog):
    gateway, _, _ = devices
    report = gateway.subdev_query.return_value
    vendor_logger = logging.getLogger("tinytuya.core.XenonDevice")

    def query():
        vendor_logger.warning("PRIVATE_CODE_12345678")
        return report

    gateway.subdev_query.side_effect = query
    with caplog.at_level(logging.DEBUG):
        client.probe()
        vendor_logger.warning("Other integration log")
    assert "PRIVATE_CODE" not in caplog.text
    assert "Other integration log" in caplog.text


def test_partial_frame_timeout_stops_without_resuming(devices):
    gateway, _, _ = devices
    gateway._receive.side_effect = TimeoutError()
    with patch.object(local.time, "monotonic", return_value=100):
        assert list(local.GatewayClient._read_decoded(gateway, 106)) == []
    gateway.socket.settimeout.assert_called_once_with(6)
    gateway._receive.assert_called_once()


def test_pinned_vendor_method_sends_once_even_when_socket_send_fails():
    fake = SimpleNamespace(
        parent=None,
        _get_socket=Mock(return_value=True),
        socket=SimpleNamespace(sendall=Mock(side_effect=OSError("failure"))),
        _check_socket_close=Mock(),
    )
    assert XenonDevice._send_receive_quick(fake, b"synthetic_packet", recv_retries=0) is None
    fake.socket.sendall.assert_called_once_with(b"synthetic_packet")
    fake._check_socket_close.assert_called_once_with(True)


def test_zero_retry_limit_prevents_connection_after_handshake_socket_loss():
    fake = SimpleNamespace(socket=None, socketRetryLimit=0)
    with patch("socket.socket", side_effect=AssertionError("network forbidden")):
        assert XenonDevice._get_socket(fake, False) is not True


@pytest.mark.parametrize("version", [3.3, 3.4, 3.5])
def test_real_vendor_payload_preserves_exact_node_and_only_dp61(version):
    # With explicit address and key the constructor and payload generation are
    # offline. Guard connect as an additional check against vendor regressions.
    with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        gateway = tinytuya.Device(
            "gateway_example", "192.0.2.1", "0123456789abcdef", version=version
        )
        child = tinytuya.Device("lock_example", cid="node_example", parent=gateway)
        raw = base64.b64encode(local.build_unlock(1, "12345678")).decode("ascii")
        payload = child.generate_payload(tinytuya.CONTROL, {"61": raw})
        envelope = json.loads(payload.payload)
        data = envelope["data"] if version >= 3.4 else envelope
        assert data["cid"] == "node_example"
        assert data["dps"] == {"61": "AQABMTIzNDU2NzgAAA=="}
        gateway.close()
