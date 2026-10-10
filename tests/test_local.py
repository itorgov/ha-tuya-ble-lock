"""Offline LAN contract tests; no test opens a connection to real hardware."""

import base64
import json
import logging
from collections import deque
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
    """Replace all network calls with a queue fed only after a request is sent."""
    queue = deque()
    gateway = SimpleNamespace(
        topology={
            "reqType": "subdev_online_stat_report",
            "data": {"online": ["node_example"], "offline": []},
        },
        status_reports=[],
        unlock_reports=[],
        topology_reports=[],
        queue=queue,
        set_socketRetryLimit=Mock(),
        socket=Mock(),
        _get_socket=Mock(return_value=True),
        close=Mock(),
        generate_payload=Mock(side_effect=lambda command, **kwargs: command),
        _decode_payload=Mock(side_effect=lambda payload: payload),
    )

    def receive():
        response = queue.popleft()
        if isinstance(response, Exception):
            raise response
        if isinstance(response, SimpleNamespace):
            return response
        return SimpleNamespace(payload=response, retcode=0, cmd=tinytuya.STATUS)

    def gateway_send(payload, recv_retries):
        assert recv_retries == 0
        if payload == tinytuya.LAN_EXT_STREAM:
            queue.extend(gateway.topology_reports)
            queue.append(gateway.topology)
        elif payload == tinytuya.HEART_BEAT:
            queue.append(SimpleNamespace(payload=b"", retcode=0, cmd=tinytuya.HEART_BEAT))
        return True

    def child_send(payload, recv_retries):
        assert recv_retries == 0
        queue.extend(
            gateway.unlock_reports if payload[0] == tinytuya.CONTROL else gateway.status_reports
        )
        return True

    gateway._receive = Mock(side_effect=receive)
    gateway._send_receive_quick = Mock(side_effect=gateway_send)
    child = SimpleNamespace(
        generate_payload=Mock(side_effect=lambda command, *args: (command, args)),
        _send_receive_quick=Mock(side_effect=child_send),
    )
    factory = Mock(side_effect=lambda *args, **kwargs: child if "parent" in kwargs else gateway)
    monkeypatch.setattr(local.tinytuya, "Device", factory)
    monkeypatch.setattr(
        local.select,
        "select",
        Mock(side_effect=lambda *args: ([gateway.socket], [], []) if queue else ([], [], [])),
    )
    return gateway, child, factory


def report(**dps):
    return {"cid": "node_example", "dps": dps}


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
    gateway.generate_payload.assert_called_once_with(
        tinytuya.LAN_EXT_STREAM, rawData={"cids": []}, reqType="subdev_online_stat_query"
    )
    gateway.set_socketRetryLimit.assert_called_once_with(0)
    child._send_receive_quick.assert_not_called()
    gateway.close.assert_called_once_with()
    client.close()
    gateway.close.assert_called_once_with()


def test_live_probe_reconnects_and_restores_status_without_repeating_subscription(
    client, devices, lock_config
):
    gateway, child, factory = devices
    client.poll([lock_config])
    factory.reset_mock()
    child._send_receive_quick.reset_mock()
    gateway._send_receive_quick.reset_mock()
    updates = []
    client.set_update_callback(updates.append)
    gateway.topology["data"] = {
        "online": ["node_example", "new_unconfigured_node"],
        "offline": ["another_unconfigured_node"],
    }
    gateway.topology_reports = [report(**{"47": True}), report(**{"47": False})]

    assert client.probe_live() == local.Topology(
        frozenset({"node_example", "new_unconfigured_node"}),
        frozenset({"another_unconfigured_node"}),
    )
    assert [snapshot["node_example"].is_locked for snapshot in updates] == [None, False, True]
    assert all(set(snapshot) == {"node_example"} for snapshot in updates)
    assert gateway._get_socket.call_count == 2
    gateway._send_receive_quick.assert_called_once_with(tinytuya.LAN_EXT_STREAM, recv_retries=0)
    gateway.close.assert_called_once()
    assert factory.call_count == 2
    child._send_receive_quick.assert_called_once()
    assert child._send_receive_quick.call_args.args[0][0] == tinytuya.DP_QUERY


@pytest.mark.parametrize(
    "topology",
    [
        {},
        {"Error": "private detail"},
        {
            "reqType": "subdev_online_stat_report",
            "data": {"online": ["node_example"], "offline": ["node_example"]},
        },
        {"reqType": "subdev_online_stat_report", "data": {"online": [{"cid": "node_example"}]}},
        {"reqType": "wrong", "data": {"online": ["node_example"]}},
    ],
)
def test_malformed_topology_prevents_unlock(client, devices, lock_config, topology):
    gateway, child, _ = devices
    gateway.topology = topology
    with pytest.raises(local.CannotConnect):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()
    gateway.close.assert_called_once()


def test_wrong_child_prevents_unlock(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.topology["data"] = {"online": ["another_node"]}
    with pytest.raises(local.NodeUnavailable):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()


def test_connection_failure_does_not_send_unlock(client, devices, lock_config):
    gateway, child, _ = devices
    gateway._get_socket.return_value = False
    with pytest.raises(local.CannotConnect):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()


def test_queued_ack_is_not_accepted_but_physical_report_is_applied(client, devices, lock_config):
    gateway, child, _ = devices
    updates = []
    client.set_update_callback(updates.append)
    gateway.queue.append(report(**{"61": "AAAB", "47": False}))
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    assert any(snapshot["node_example"].is_locked is True for snapshot in updates)
    child._send_receive_quick.assert_called_once()
    assert updates[-1]["node_example"] == local.LockStatus(False)


@pytest.mark.parametrize("send_result", [None, False, "True"])
def test_ambiguous_send_never_retries(client, devices, lock_config, send_result):
    gateway, child, _ = devices
    child._send_receive_quick.side_effect = None
    child._send_receive_quick.return_value = send_result
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once()
    gateway.close.assert_called_once()


def test_send_exception_sanitized_and_never_retried(client, devices, lock_config):
    _, child, _ = devices
    child._send_receive_quick.side_effect = RuntimeError("12345678 PRIVATE KEY")
    with pytest.raises(local.UnlockOutcomeUnknown) as exc:
        client.unlock(lock_config)
    assert "12345678" not in str(exc.value)
    assert "PRIVATE" not in str(exc.value)
    child._send_receive_quick.assert_called_once()


@pytest.mark.parametrize(
    "response",
    [
        {"cid": "different_node", "dps": {"61": "AAAB"}},
        {"cid": "node_example", "data": {"cid": "different_node"}, "dps": {"61": "AAAB"}},
        {"dps": {"61": "AAAB"}},
        {"cid": "node_example", "dps": {"40": "open", "47": True}},
        {"cid": "node_example", "Error": "error", "dps": {"61": "AAAB"}},
    ],
)
def test_wrong_child_or_motor_status_never_confirms_command(client, devices, lock_config, response):
    gateway, child, _ = devices
    gateway.unlock_reports = [response]
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once()


def test_matching_ack_never_changes_physical_state(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.unlock_reports = [{"data": {"cid": "node_example", "dps": {"61": "AAAB"}}}]
    assert client.unlock(lock_config) is None
    child.generate_payload.assert_called_once_with(tinytuya.CONTROL, {"61": "AQABMTIzNDU2NzgAAA=="})
    child._send_receive_quick.assert_called_once()
    gateway.close.assert_not_called()
    assert client._statuses["node_example"].is_locked is None


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
    gateway.unlock_reports = [report(**{"61": value})]
    with pytest.raises(exception):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once()


def test_all_transitions_during_and_after_unlock_are_delivered(client, devices, lock_config):
    gateway, child, _ = devices
    updates = []
    client.set_update_callback(updates.append)
    gateway.status_reports = [report(**{"8": 63, "47": False, "40": "closed"})]
    client.poll([lock_config])
    updates.clear()
    gateway.unlock_reports = [report(**{"47": True}), report(**{"61": "AAAB"})]
    client.unlock(lock_config)
    gateway.queue.extend([report(**{"40": "open"}), report(**{"47": False})])
    client.listen()
    assert [snapshot["node_example"] for snapshot in updates] == [
        local.LockStatus(True, 63, False),
        local.LockStatus(True, 63, True),
    ]
    assert (
        sum(
            call.args[0][0] == tinytuya.CONTROL for call in child._send_receive_quick.call_args_list
        )
        == 1
    )
    gateway._get_socket.assert_called_once_with(False)
    gateway.close.assert_not_called()


def test_multi_child_exact_identity_and_partial_reports(client, devices, lock_config):
    gateway, _, _ = devices
    gateway.topology["data"] = {"online": ["node_example", "second_node"], "offline": []}
    gateway.status_reports = [
        {"cid": "second_node", "dps": {"8": 72, "47": True}},
        report(**{"8": 63, "47": False, "40": "closed"}),
        {"cid": "node_example", "data": {"cid": "different_node"}, "dps": {"8": 99}},
        {"cid": "unconfigured_node", "dps": {"8": 99}},
    ]
    locks = [lock_config, local.LockConfig("lock_two", "second_node", "Second")]
    assert client.poll(locks) == {
        "node_example": local.LockStatus(True, 63, True),
        "second_node": local.LockStatus(True, 72, False),
    }
    gateway.status_reports = [report(**{"62": "unused"})]
    assert client.poll(locks)["node_example"] == local.LockStatus(True, 63, True)
    gateway.close.assert_not_called()


@pytest.mark.parametrize(
    "dps,expected",
    [
        ({"8": -1}, local.LockStatus(True, None, True)),
        ({"8": True}, local.LockStatus(True, None, True)),
        ({"8": 101}, local.LockStatus(True, None, True)),
        ({"8": "50"}, local.LockStatus(True, None, True)),
        ({"47": 0}, local.LockStatus(True, 63, None)),
    ],
)
def test_explicit_invalid_value_clears_only_its_field(client, devices, lock_config, dps, expected):
    gateway, _, _ = devices
    gateway.status_reports = [report(**{"8": 63, "47": False, "40": "closed"})]
    client.poll([lock_config])
    gateway.queue.append(report(**dps))
    client.listen()
    assert client._statuses["node_example"] == expected


@pytest.mark.parametrize("value", ["open", "closed", "unknown", True, False, 0, None])
def test_dp40_is_ignored_without_changing_battery_or_lock(client, devices, lock_config, value):
    gateway, _, _ = devices
    gateway.status_reports = [report(**{"8": 63, "47": False})]
    client.poll([lock_config])
    callback = Mock()
    client.set_update_callback(callback)
    gateway.queue.append(report(**{"40": value}))
    client.listen()
    assert client._statuses["node_example"] == local.LockStatus(True, 63, True)
    callback.assert_not_called()

    gateway.queue.append(report(**{"40": value, "47": True, "8": 62}))
    client.listen()
    assert client._statuses["node_example"] == local.LockStatus(True, 62, False)
    callback.assert_called_once_with({"node_example": local.LockStatus(True, 62, False)})


def test_hx201_profile_replays_observed_unlock_and_relock_trace(client, devices):
    gateway, child, _ = devices
    lock = local.LockConfig(
        "lock_example", "node_example", "Example lock", "12345678", state_source="hx201_dp40"
    )
    updates = []
    client.set_update_callback(updates.append)
    gateway.topology_reports = [report(**{"8": 100, "40": "closed"})]
    # Sanitized physical trace: ACK first, two open reports, then two closed
    # reports. No DP47, timing estimate, or command acknowledgement supplies state.
    gateway.unlock_reports = [
        report(**{"61": "AAAB"}),
        report(**{"40": "open"}),
        report(**{"40": "open"}),
        report(**{"40": "closed"}),
        report(**{"40": "closed"}),
    ]
    client.unlock(lock)
    assert [snapshot["node_example"] for snapshot in updates] == [local.LockStatus(True, 100, True)]
    client.listen()
    assert [snapshot["node_example"] for snapshot in updates] == [
        local.LockStatus(True, 100, True),
        local.LockStatus(True, 100, False),
        local.LockStatus(True, 100, True),
    ]
    child._send_receive_quick.assert_called_once()
    assert client.diagnostics()["locks"][0]["state_source"] == "hx201_dp40"
    assert client.diagnostics()["locks"][0]["dp47_report_count"] == 0


@pytest.mark.parametrize("value", ["unknown", True, False, 0, None, {"private": "value"}])
def test_hx201_explicit_invalid_state_clears_state_only(client, devices, value):
    gateway, _, _ = devices
    lock = local.LockConfig("lock_example", "node_example", "Lock", state_source="hx201_dp40")
    gateway.status_reports = [report(**{"8": 100, "40": "closed"})]
    client.poll([lock])
    gateway.queue.append(report(**{"40": value}))
    client.listen()
    assert client._statuses["node_example"] == local.LockStatus(True, 100)


@pytest.mark.parametrize(
    "source,dps,expected",
    [
        ("motor_dp47", {"40": "closed", "47": True}, False),
        ("motor_dp47", {"40": "open", "47": False}, True),
        ("hx201_dp40", {"40": "closed", "47": True}, True),
        ("hx201_dp40", {"40": "open", "47": False}, False),
    ],
)
def test_state_source_selects_only_its_own_report(client, devices, source, dps, expected):
    gateway, _, _ = devices
    lock = local.LockConfig("lock_example", "node_example", "Lock", state_source=source)
    gateway.status_reports = [report(**{"8": 100, **dps})]
    client.poll([lock])
    assert client._statuses["node_example"] == local.LockStatus(True, 100, expected)
    ignored = {"47": not expected} if source == "hx201_dp40" else {"40": "unknown"}
    gateway.queue.append(report(**ignored))
    client.listen()
    assert client._statuses["node_example"] == local.LockStatus(True, 100, expected)


def test_unsupported_source_never_selects_state_or_leaks_diagnostic_value(client, devices):
    gateway, _, _ = devices
    lock = local.LockConfig(
        "lock_example", "node_example", "Lock", state_source="PRIVATE_UNSUPPORTED_SOURCE"
    )
    gateway.status_reports = [report(**{"8": 100, "40": "closed", "47": False})]
    client.poll([lock])
    assert client._statuses["node_example"] == local.LockStatus(True, 100)
    diagnostic = client.diagnostics()
    assert diagnostic["locks"][0]["state_source"] == "unsupported"
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_unsupported_optional_query_preserves_connection_and_observations(
    client, devices, lock_config
):
    gateway, _, _ = devices
    gateway.status_reports = [
        SimpleNamespace(payload=b"", retcode=1, cmd=tinytuya.DP_QUERY_NEW),
        {"Error": "unsupported"},
    ]
    assert client.poll([lock_config]) == {"node_example": local.LockStatus(True)}
    gateway.queue.append(report(**{"47": False}))
    client.listen()
    assert client._statuses["node_example"].is_locked is True
    gateway.close.assert_not_called()


def test_offline_and_reconnect_invalidate_observations(client, devices, lock_config):
    gateway, _, _ = devices
    gateway.status_reports = [report(**{"8": 63, "47": False, "40": "closed"})]
    client.poll([lock_config])
    gateway.queue.append(
        {
            "reqType": "subdev_online_stat_report",
            "data": {"online": [], "offline": ["node_example"]},
        }
    )
    client.listen()
    assert client._statuses["node_example"] == local.LockStatus(False)
    gateway.status_reports = []
    assert client.poll([lock_config]) == {"node_example": local.LockStatus(False)}
    gateway.queue.append(gateway.topology)
    client.listen()
    assert client.poll([lock_config]) == {"node_example": local.LockStatus(True)}
    gateway.queue.append(TimeoutError("PRIVATE"))
    with pytest.raises(local.CannotConnect, match="interrupted"):
        client.listen()
    assert client._statuses["node_example"] == local.LockStatus(False)
    assert client.poll([lock_config]) == {"node_example": local.LockStatus(True)}
    assert gateway._get_socket.call_count == 2


def test_listen_never_connects_and_idle_wait_keeps_socket(client, devices, lock_config):
    gateway, _, factory = devices
    with pytest.raises(local.CannotConnect):
        client.listen()
    factory.assert_not_called()
    client.poll([lock_config])
    gateway._receive.reset_mock()
    client.listen()
    gateway._receive.assert_not_called()
    gateway.close.assert_not_called()
    assert 0 <= local.select.select.call_args.args[3] <= local.LISTEN_TIMEOUT


def test_heartbeat_and_missing_response_disconnect(client, devices, lock_config):
    gateway, _, _ = devices
    with patch.object(local.time, "monotonic", return_value=100):
        client.poll([lock_config])
    with patch.object(local.time, "monotonic", return_value=111):
        client.listen()
    assert gateway.generate_payload.call_args.args == (tinytuya.HEART_BEAT,)
    with patch.object(local.time, "monotonic", return_value=142):
        with pytest.raises(local.CannotConnect, match="stopped responding"):
            client.listen()
    gateway.close.assert_called_once()


def test_interleaved_state_during_topology_is_not_lost(client, devices, lock_config):
    gateway, _, _ = devices
    gateway.topology_reports = [report(**{"47": True}), report(**{"47": False})]
    updates = []
    client.set_update_callback(updates.append)
    client.poll([lock_config])
    assert [snapshot["node_example"].is_locked for snapshot in updates] == [False, True]


def test_poll_and_unlock_subscribe_to_topology_once_per_connection(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.status_reports = [report(**{"8": 63, "47": False})]
    gateway.unlock_reports = [report(**{"61": "AAAB"})]
    client.poll([lock_config])
    client.poll([lock_config])
    client.unlock(lock_config)
    gateway._send_receive_quick.assert_called_once_with(tinytuya.LAN_EXT_STREAM, recv_retries=0)
    assert [call.args[0][0] for call in child._send_receive_quick.call_args_list] == [
        tinytuya.DP_QUERY,
        tinytuya.DP_QUERY,
        tinytuya.CONTROL,
    ]
    gateway._get_socket.assert_called_once_with(False)


def test_newly_configured_child_uses_session_topology_without_overwriting_newer_reports(
    client, devices, lock_config
):
    gateway, child, _ = devices
    gateway.topology = {
        "reqType": "subdev_online_stat_report",
        "data": {"online": ["node_example", "second_node"], "offline": []},
    }
    client.poll([lock_config])
    gateway.queue.extend(
        [
            {
                "reqType": "subdev_online_stat_report",
                "data": {"online": ["second_node"], "offline": ["node_example"]},
            },
            report(**{"8": 63, "47": False}),
        ]
    )
    client.listen()
    child._send_receive_quick.reset_mock()
    result = client.poll([lock_config, local.LockConfig("lock_two", "second_node", "Second")])
    assert result == {
        "node_example": local.LockStatus(True, 63, True),
        "second_node": local.LockStatus(True),
    }
    assert child._send_receive_quick.call_count == 2
    gateway._send_receive_quick.assert_called_once_with(tinytuya.LAN_EXT_STREAM, recv_retries=0)


def test_unsolicited_offline_blocks_unlock_without_repeat_topology(client, devices, lock_config):
    gateway, child, _ = devices
    client.poll([lock_config])
    child._send_receive_quick.reset_mock()
    gateway.queue.append(
        {
            "reqType": "subdev_online_stat_report",
            "data": {"online": [], "offline": ["node_example"]},
        }
    )
    with pytest.raises(local.NodeUnavailable):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()
    gateway._send_receive_quick.assert_called_once_with(tinytuya.LAN_EXT_STREAM, recv_retries=0)


def test_exact_child_report_restores_availability_after_offline(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.topology = {
        "reqType": "subdev_online_stat_report",
        "data": {"online": [], "offline": ["node_example"]},
    }
    client.poll([lock_config])
    gateway.queue.append(report(**{"8": 63, "47": False}))
    gateway.unlock_reports = [report(**{"61": "AAAB"})]
    client.unlock(lock_config)
    assert client._statuses["node_example"] == local.LockStatus(True, 63, True)
    child._send_receive_quick.assert_called_once()
    assert child._send_receive_quick.call_args.args[0][0] == tinytuya.CONTROL
    gateway._send_receive_quick.assert_called_once_with(tinytuya.LAN_EXT_STREAM, recv_retries=0)


def test_initial_topology_requires_report_even_when_ack_and_child_status_arrive(
    client, devices, lock_config
):
    gateway, child, _ = devices

    def ack_and_status(*args, **kwargs):
        gateway.queue.extend(
            [
                SimpleNamespace(payload=b"", retcode=0, cmd=tinytuya.LAN_EXT_STREAM),
                report(**{"8": 63, "47": False}),
            ]
        )
        return True

    gateway._send_receive_quick.side_effect = ack_and_status
    with pytest.raises(local.CannotConnect, match="topology report"):
        client.unlock(lock_config)
    assert client._topology_cache is None
    child._send_receive_quick.assert_not_called()


def test_connection_loss_discards_topology_and_requires_new_subscription(
    client, devices, lock_config
):
    gateway, child, _ = devices
    client.poll([lock_config])
    gateway.queue.append(OSError("PRIVATE"))
    with pytest.raises(local.CannotConnect):
        client.listen()
    assert client._topology_cache is None
    gateway.topology = {"reqType": "subdev_online_stat_report", "data": {"online": ["other_node"]}}
    child._send_receive_quick.reset_mock()
    with pytest.raises(local.NodeUnavailable):
        client.unlock(lock_config)
    assert gateway._send_receive_quick.call_count == 2
    assert gateway._get_socket.call_count == 2
    child._send_receive_quick.assert_not_called()


def test_failed_live_probe_does_not_retain_previous_topology(client, devices, lock_config):
    gateway, _, _ = devices
    client.poll([lock_config])
    gateway._get_socket.return_value = False
    with pytest.raises(local.CannotConnect):
        client.probe_live()
    assert client._topology_cache is None
    assert client._gateway is None
    assert client._statuses["node_example"] == local.LockStatus(False)


def test_gateway_exception_sanitized(client, devices):
    gateway, _, _ = devices
    gateway._get_socket.side_effect = RuntimeError("sensitive_identifier secret_local_key")
    with pytest.raises(local.CannotConnect) as exc:
        client.probe()
    assert "sensitive" not in str(exc.value)
    assert "secret" not in str(exc.value)
    assert exc.value.__suppress_context__


def test_vendor_logs_suppressed_only_in_our_session(client, devices, caplog):
    gateway, _, _ = devices
    vendor_logger = logging.getLogger("tinytuya.core.XenonDevice")

    def connect(*args):
        vendor_logger.warning("PRIVATE_CODE_12345678")
        return True

    gateway._get_socket.side_effect = connect
    with caplog.at_level(logging.DEBUG):
        client.probe()
        vendor_logger.warning("Other integration log")
    assert "PRIVATE_CODE" not in caplog.text
    assert "Other integration log" in caplog.text


def test_partial_frame_timeout_closes_without_resuming(client, devices, lock_config):
    gateway, _, _ = devices
    client.poll([lock_config])
    gateway.queue.append(TimeoutError())
    with pytest.raises(local.CannotConnect):
        client.listen()
    gateway.close.assert_called_once()
    with pytest.raises(local.CannotConnect):
        client.listen()


def test_drain_uses_short_idle_wait_but_full_frame_budget(client, devices, lock_config):
    gateway, _, _ = devices
    gateway.queue.append(report(**{"47": False}))
    with patch.object(local.time, "monotonic", return_value=100):
        client.poll([lock_config])
    assert gateway.socket.settimeout.call_args_list[0].args == (local.CONNECTION_TIMEOUT,)
    assert local.select.select.call_args_list[0].args[3] == 0.05


def test_queued_frame_limit_prevents_topology_and_unlock_send(client, devices, lock_config):
    gateway, child, _ = devices
    gateway.queue.extend([report(**{"61": "AAAB"})] * (local.MAX_FRAMES + 1))
    with pytest.raises(local.CannotConnect, match="Too many queued"):
        client.unlock(lock_config)
    gateway._send_receive_quick.assert_not_called()
    child._send_receive_quick.assert_not_called()
    gateway.close.assert_called_once()


def test_drain_deadline_with_unread_frames_prevents_unlock(client, devices, lock_config):
    gateway, child, _ = devices
    client.poll([lock_config])
    gateway._send_receive_quick.reset_mock()
    child._send_receive_quick.reset_mock()
    gateway.queue.extend([report(**{"61": "AAAB"}), report(**{"61": "AAAB"})])
    with patch.object(local.time, "monotonic", side_effect=[100, 100, 100, 103, 103]):
        with pytest.raises(local.CannotConnect, match="Could not drain"):
            client.unlock(lock_config)
    gateway._send_receive_quick.assert_not_called()
    child._send_receive_quick.assert_not_called()


def test_offline_report_after_fresh_topology_prevents_unlock(client, devices, lock_config):
    gateway, child, _ = devices

    def topology_then_offline(*args, **kwargs):
        gateway.queue.extend(
            [gateway.topology, {"reqType": "subdev_online_stat_report", "data": {"online": []}}]
        )
        return True

    gateway._send_receive_quick.side_effect = topology_then_offline
    with pytest.raises(local.NodeUnavailable):
        client.unlock(lock_config)
    child._send_receive_quick.assert_not_called()


def test_ack_queued_after_topology_cannot_confirm_new_unlock(client, devices, lock_config):
    gateway, child, _ = devices

    def topology_then_old_ack(*args, **kwargs):
        gateway.queue.extend([gateway.topology, report(**{"61": "AAAB"})])
        return True

    gateway._send_receive_quick.side_effect = topology_then_old_ack
    with pytest.raises(local.UnlockOutcomeUnknown):
        client.unlock(lock_config)
    child._send_receive_quick.assert_called_once()


def test_explicit_close_is_idempotent_and_clears_cache(client, devices, lock_config):
    gateway, _, _ = devices
    client.poll([lock_config])
    callback = Mock()
    client.set_update_callback(callback)
    client.close()
    client.close()
    gateway.close.assert_called_once()
    assert client._statuses == {}
    callback.assert_not_called()


def test_diagnostics_snapshot_never_accesses_network(client, devices):
    gateway, child, factory = devices
    assert client.diagnostics() == {
        "connection_count": 0,
        "received_report_count": 0,
        "locks": [],
    }
    factory.assert_not_called()
    gateway._receive.assert_not_called()
    child._send_receive_quick.assert_not_called()


def test_diagnostics_counts_valid_invalid_and_absent_reports(client, devices, lock_config):
    gateway, _, _ = devices
    client.poll([lock_config])
    gateway.queue.extend(
        [
            report(**{"8": 100, "47": False, "40": "closed"}),
            report(**{"47": True, "40": "open"}),
            report(**{"8": -1, "47": "PRIVATE_RAW_STATE", "40": "unknown"}),
            report(**{"47": 0, "40": {"secret": "PRIVATE_VALUE"}}),
            report(**{"62": "PRIVATE_RAW_PAYLOAD"}),
        ]
    )
    client.listen()
    diagnostic = client.diagnostics()
    assert diagnostic["connection_count"] == 1
    assert diagnostic["received_report_count"] == 6
    assert diagnostic["locks"] == [
        {
            "state_source": "motor_dp47",
            "observed_dp_ids": [8, 40, 47, 62],
            "dp8_report_count": 2,
            "dp8_valid_count": 1,
            "dp8_invalid_count": 1,
            "dp47_report_count": 4,
            "dp47_valid_count": 2,
            "dp47_invalid_count": 2,
            "dp47_locked_count": 1,
            "dp47_unlocked_count": 1,
            "dp40_report_count": 4,
            "dp40_open_count": 1,
            "dp40_closed_count": 1,
            "dp40_unknown_count": 1,
            "dp40_invalid_count": 1,
        }
    ]
    assert "PRIVATE" not in json.dumps(diagnostic)


def test_diagnostics_discard_identifiers_arbitrary_keys_and_unmatched_nodes(
    client, devices, lock_config
):
    gateway, _, _ = devices
    client.poll([lock_config])
    gateway.queue.extend(
        [
            report(
                **{
                    "61": "PRIVATE_CODE",
                    "PRIVATE_KEY": "PRIVATE_VALUE",
                    "256": True,
                    "0": True,
                    "-1": True,
                    "047": True,
                    "٤٧": True,
                    "255": False,
                }
            ),
            {"cid": "PRIVATE_OTHER_NODE", "dps": {"47": True}},
            {"cid": "node_example", "data": {"cid": "other_node"}, "dps": {"47": True}},
        ]
    )
    client.listen()
    diagnostic = client.diagnostics()
    counts = diagnostic["locks"][0]
    assert counts["observed_dp_ids"] == [61, 255]
    assert counts["dp47_report_count"] == 0
    serialized = json.dumps(diagnostic)
    for forbidden in ("PRIVATE", "node_example", "lock_example", "Example lock", "12345678"):
        assert forbidden not in serialized
    assert "PRIVATE" not in repr(client._observations)


def test_diagnostics_preserve_order_and_counters_across_close(client, devices, lock_config):
    gateway, _, _ = devices
    second = local.LockConfig("lock_two", "second_node", "Private name")
    gateway.topology["data"]["online"].append("second_node")
    client.poll([second, lock_config])
    gateway.queue.append(report(**{"47": False}))
    client.listen()
    before = client.diagnostics()
    assert before["locks"][0]["dp47_report_count"] == 0
    assert before["locks"][1]["dp47_report_count"] == 1
    client.close()
    assert client.diagnostics() == before
    client.poll([second, lock_config])
    after = client.diagnostics()
    assert after["connection_count"] == 2
    assert after["locks"] == before["locks"]
    after["locks"][1]["observed_dp_ids"].clear()
    after["locks"][1]["dp47_report_count"] = 0
    assert client.diagnostics()["locks"][1]["dp47_report_count"] == 1
    assert client.diagnostics()["locks"][1]["observed_dp_ids"] == [47]


def test_diagnostic_counters_and_observed_ids_are_bounded(client, devices, lock_config):
    gateway, _, _ = devices
    client.poll([lock_config])
    gateway.queue.append(report(**{str(key): True for key in range(300)}))
    client.listen()
    counts = client._observations[lock_config.node_id]
    counts.counts = dict.fromkeys(local._DIAGNOSTIC_COUNTERS, local.MAX_DIAGNOSTIC_COUNT)
    client._connection_count = local.MAX_DIAGNOSTIC_COUNT
    client._received_report_count = local.MAX_DIAGNOSTIC_COUNT
    client.close()
    gateway.status_reports = [report(**{"8": 100, "47": False, "40": "closed"})]
    client.poll([lock_config])
    diagnostic = client.diagnostics()
    assert diagnostic["connection_count"] == local.MAX_DIAGNOSTIC_COUNT
    assert diagnostic["received_report_count"] == local.MAX_DIAGNOSTIC_COUNT
    assert diagnostic["locks"][0]["observed_dp_ids"] == list(range(1, 256))
    assert all(value == local.MAX_DIAGNOSTIC_COUNT for value in counts.counts.values())


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
