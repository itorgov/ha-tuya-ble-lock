"""Synchronous, credential-safe LAN transport for the supported DP61 lock profile.

All methods belong in Home Assistant's executor and must be serialized by the
caller. One persistent session receives device reports between commands. Unlock
sends one control packet at most; its acknowledgement is never a physical state.
"""

# Sanitize every vendor exception at this credential boundary; deliberately do
# not log exceptions or their tracebacks, which may contain decrypted payloads.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import base64
import binascii
import logging
import os
import re
import select
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any

import tinytuya
from tinytuya.core import header as tuya_header
from tinytuya.core.exceptions import DecodeError
from tinytuya.core.message_helper import MessagePayload, TuyaMessage, pack_message

PINNED_TINYTUYA = "1.20.0"
CONNECTION_TIMEOUT = 2.0
STATUS_TIMEOUT = 2.0
UNLOCK_TIMEOUT = 6.0
MAX_FRAMES = 64
LISTEN_TIMEOUT = 1.0
HEARTBEAT_INTERVAL = 10.0
HEARTBEAT_TIMEOUT = 30.0
MAX_DIAGNOSTIC_COUNT = 2_147_483_647
_DIAGNOSTIC_COUNTERS = (
    "dp8_report_count",
    "dp8_valid_count",
    "dp8_invalid_count",
    "dp47_report_count",
    "dp47_valid_count",
    "dp47_invalid_count",
    "dp47_locked_count",
    "dp47_unlocked_count",
    "dp40_report_count",
    "dp40_open_count",
    "dp40_closed_count",
    "dp40_unknown_count",
    "dp40_invalid_count",
)

_PRIVATE_OPERATION: ContextVar[bool] = ContextVar("tuya_ble_lock_private", default=False)


class _PrivateTransportFilter(logging.Filter):
    """Do not let TinyTuya log decrypted codes or identifiers for our sessions."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not _PRIVATE_OPERATION.get()


_FILTER = _PrivateTransportFilter()
# Logger filters do not propagate from an ancestor. These are the loggers used
# by the pinned core transport; other integrations' operations are unaffected.
for _module in (
    "XenonDevice",
    "Device",
    "core",
    "crypto_helper",
    "error_helper",
    "message_helper",
):
    logging.getLogger(f"tinytuya.core.{_module}").addFilter(_FILTER)


class _GatewayDevice(tinytuya.Device):
    """Apply transport security fixes only to this integration's connections."""

    def _encode_message(self, msg: MessagePayload) -> bytes:
        # A child must share the gateway's session key and sequence counter.
        if self.parent:
            return self.parent._encode_message(msg)
        if self.version < 3.5:
            return super()._encode_message(msg)

        payload = msg.payload
        if msg.cmd not in tuya_header.NO_PROTOCOL_HEADER_CMDS:
            payload = self.version_header + payload
        # TinyTuya 1.20.0 selects a fixed GCM IV when debug logging is enabled.
        # Supply our own random IV, including for session negotiation frames.
        message = TuyaMessage(
            self.seqno,
            msg.cmd,
            None,
            payload,
            0,
            True,
            tuya_header.PREFIX_6699_VALUE,
            os.urandom(12),
        )
        self.seqno += 1
        return pack_message(message, hmac_key=self.local_key)

    def _receive(self) -> TuyaMessage:
        message = super()._receive()
        # The pinned vendor rejects bad GCM tags, but returns bad CRC/HMAC
        # frames. Reject those before handshake or application processing.
        if not message.crc_good:
            raise DecodeError("LAN frame integrity verification failed.")
        return message


class LocalError(Exception):
    """A deliberately credential-free local transport error."""


class CannotConnect(LocalError):
    """A fresh read-only gateway handshake could not be established."""


class NodeUnavailable(LocalError):
    """The exact configured child was not unambiguously reported online."""


class UnlockError(LocalError):
    """Unlock could not be completed; no automatic retry is permitted."""


class UnlockOutcomeUnknown(UnlockError):
    """A command may have reached the lock but its outcome is unknown."""


class UnlockRejected(UnlockError):
    """The observed DP61 response did not match the expected result."""


@dataclass(frozen=True)
class Topology:
    """Nodes observed in one complete gateway topology report."""

    online: frozenset[str]
    offline: frozenset[str]


@dataclass(frozen=True, repr=False)
class LockConfig:
    """One explicitly supported lock and its existing remote unlock code."""

    device_id: str
    node_id: str
    name: str
    remote_unlock_code: str | None = None
    member: int = 1
    state_source: str = "motor_dp47"


@dataclass(frozen=True)
class LockStatus:
    """Connectivity and the last values reported on the current connection."""

    online: bool
    battery: int | None = None
    is_locked: bool | None = None


@dataclass
class _ObservationCounters:
    """Bounded protocol observations; never retain raw datapoint values."""

    dp_ids: set[int] = field(default_factory=set)
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_DIAGNOSTIC_COUNTERS, 0))

    def increment(self, key: str) -> None:
        self.counts[key] = min(self.counts[key] + 1, MAX_DIAGNOSTIC_COUNT)

    def observe(self, dps: dict[str, Any]) -> None:
        for dp_id in dps:
            if re.fullmatch(r"[1-9][0-9]{0,2}", dp_id) and int(dp_id) <= 255:
                self.dp_ids.add(int(dp_id))
        if "8" in dps:
            self.increment("dp8_report_count")
            value = dps["8"]
            valid = type(value) is int and 0 <= value <= 100
            self.increment("dp8_valid_count" if valid else "dp8_invalid_count")
        if "47" in dps:
            self.increment("dp47_report_count")
            value = dps["47"]
            if type(value) is bool:
                self.increment("dp47_valid_count")
                self.increment("dp47_unlocked_count" if value else "dp47_locked_count")
            else:
                self.increment("dp47_invalid_count")
        if "40" in dps:
            self.increment("dp40_report_count")
            value = dps["40"]
            category = (
                value
                if type(value) is str and value in ("open", "closed", "unknown")
                else "invalid"
            )
            self.increment(f"dp40_{category}_count")

    def snapshot(self) -> dict[str, Any]:
        return {"observed_dp_ids": sorted(self.dp_ids), **self.counts}


def build_unlock(member: int, code: str | None) -> bytes:
    """Build the 13-byte DP61 payload without registering or changing a code."""
    if type(member) is not int or not 1 <= member <= 65534:
        raise UnlockError("The member number must be an integer from 1 to 65534.")
    if not isinstance(code, str) or re.fullmatch(r"[0-9]{8}", code) is None:
        raise UnlockError("Configure an existing remote unlock code of eight ASCII digits.")
    return b"\x01" + member.to_bytes(2, "big") + code.encode("ascii") + b"\x00\x00"


def matching_dps(response: object, node_id: str) -> dict[str, Any] | None:
    """Accept only an exact, consistent child identity in a fresh response."""
    if not isinstance(response, dict) or "Error" in response or "Err" in response:
        return None
    nested = response.get("data")
    nested = nested if isinstance(nested, dict) else {}
    if "Error" in nested or "Err" in nested:
        return None
    if response.get("cid", nested.get("cid")) != node_id:
        return None
    if "cid" in response and "cid" in nested and response["cid"] != nested["cid"]:
        return None
    dps = response.get("dps", nested.get("dps"))
    if not isinstance(dps, dict):
        return None
    if "dps" in response and "dps" in nested and response["dps"] != nested["dps"]:
        return None
    return {str(key): value for key, value in dps.items()}


def _topology(response: object) -> Topology:
    if (
        not isinstance(response, dict)
        or "Error" in response
        or "Err" in response
        or response.get("reqType") != "subdev_online_stat_report"
        or not isinstance(response.get("data"), dict)
    ):
        raise CannotConnect("The gateway did not return a fresh subdevice topology report.")
    data = response["data"]
    online, offline = data.get("online"), data.get("offline", [])
    if (
        not isinstance(online, list)
        or not isinstance(offline, list)
        or any(not isinstance(node, str) or not node for node in online + offline)
        or "Error" in data
        or "Err" in data
    ):
        raise CannotConnect("The gateway returned an invalid subdevice topology report.")
    if set(online).intersection(offline):
        raise CannotConnect("The gateway returned contradictory child connectivity.")
    return Topology(frozenset(online), frozenset(offline))


class GatewayClient:
    """One persistent gateway connection, exclusively owned by its caller."""

    def __init__(self, host: str, gateway_id: str, local_key: str, version: float):
        if not isinstance(host, str) or not host.strip() or host in ("Auto", "0.0.0.0"):
            raise LocalError("Configure an explicit gateway hostname or IP address.")
        if (
            not isinstance(gateway_id, str)
            or re.fullmatch(r"[A-Za-z0-9_-]{1,128}", gateway_id) is None
        ):
            raise LocalError("Configure the exact gateway device identifier.")
        try:
            valid_key = isinstance(local_key, str) and len(local_key.encode("latin1")) == 16
        except UnicodeEncodeError:
            valid_key = False
        if not valid_key:
            raise LocalError("The gateway local key must contain exactly 16 Latin-1 bytes.")
        if isinstance(version, bool) or version not in (3.3, 3.4, 3.5):
            raise LocalError("The verified gateway LAN version must be 3.3, 3.4 or 3.5.")
        if getattr(tinytuya, "__version__", None) != PINNED_TINYTUYA:
            raise LocalError("The required pinned TinyTuya version is not installed.")
        self._host = host
        self._gateway_id = gateway_id
        self._local_key = local_key
        self._version = version
        self._gateway: Any | None = None
        self._topology_cache: Topology | None = None
        self._locks: dict[str, LockConfig] = {}
        self._poll_cursor = 0
        self._statuses: dict[str, LockStatus] = {}
        self._observations: dict[str, _ObservationCounters] = {}
        self._diagnostic_order: list[str] = []
        self._diagnostic_sources: dict[str, str] = {}
        self._connection_count = 0
        self._received_report_count = 0
        self._callback: Callable[[dict[str, LockStatus]], None] | None = None
        self._last_heartbeat = 0.0
        self._last_received = 0.0

    def set_update_callback(self, callback: Callable[[dict[str, LockStatus]], None] | None) -> None:
        """Set a thread-safe recipient of sanitized, immutable value snapshots."""
        self._callback = callback

    def diagnostics(self) -> dict[str, Any]:
        """Return anonymous counters in configured order, without network I/O."""
        return {
            "connection_count": self._connection_count,
            "received_report_count": self._received_report_count,
            "locks": [
                {
                    "state_source": self._diagnostic_sources.get(node_id, "motor_dp47"),
                    **self._observations.get(node_id, _ObservationCounters()).snapshot(),
                }
                for node_id in self._diagnostic_order
            ],
        }

    def _notify(self) -> None:
        if self._callback is not None:
            self._callback(dict(self._statuses))

    def _set_status(self, node_id: str, status: LockStatus) -> None:
        if self._statuses.get(node_id) != status:
            self._statuses[node_id] = status
            self._notify()

    @contextmanager
    def _operation(self) -> Iterator[None]:
        token = _PRIVATE_OPERATION.set(True)
        try:
            yield
        except NodeUnavailable, UnlockRejected:
            raise
        except LocalError:
            self._disconnect()
            raise
        except Exception:
            self._disconnect()
            raise CannotConnect("The local gateway operation failed.") from None
        finally:
            _PRIVATE_OPERATION.reset(token)

    def _new_gateway(self) -> Any:
        gateway = _GatewayDevice(
            self._gateway_id,
            self._host,
            self._local_key,
            version=self._version,
            persist=True,
            connection_timeout=CONNECTION_TIMEOUT,
            connection_retry_limit=1,
            connection_retry_delay=0,
        )
        gateway.disabledetect = True
        return gateway

    @staticmethod
    def _open_gateway(gateway: Any) -> None:
        if gateway._get_socket(False) is not True:
            raise CannotConnect("Could not connect to the local gateway.")
        # Once opened, no helper may reconnect implicitly or replay a command.
        gateway.set_socketRetryLimit(0)

    def _connect(self) -> Any:
        if self._gateway is None:
            self._gateway = self._new_gateway()
            self._open_gateway(self._gateway)
            self._connection_count = min(self._connection_count + 1, MAX_DIAGNOSTIC_COUNT)
            self._last_heartbeat = self._last_received = time.monotonic()
        if self._gateway.socket is None:
            raise CannotConnect("The local gateway disconnected.")
        return self._gateway

    @staticmethod
    def _close_gateway(gateway: Any | None) -> None:
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                pass

    def _disconnect(self) -> None:
        gateway, self._gateway = self._gateway, None
        self._topology_cache = None
        self._poll_cursor = 0
        self._close_gateway(gateway)
        statuses = {node_id: LockStatus(False) for node_id in self._locks}
        if statuses != self._statuses:
            self._statuses = statuses
            self._notify()

    def close(self) -> None:
        """Release the connection and clear cached observations without callbacks."""
        token = _PRIVATE_OPERATION.set(True)
        try:
            gateway, self._gateway = self._gateway, None
            self._topology_cache = None
            self._poll_cursor = 0
            self._close_gateway(gateway)
            self._locks.clear()
            self._statuses.clear()
        finally:
            _PRIVATE_OPERATION.reset(token)

    @staticmethod
    def _read_decoded(
        gateway: Any, deadline: float, *, idle_timeout: float | None = None
    ) -> Iterator[dict[str, Any]]:
        """Wait before reading so idle timeouts cannot damage partial framing."""
        for _ in range(MAX_FRAMES):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if gateway.socket is None:
                raise CannotConnect("The local gateway disconnected.")
            try:
                wait = remaining if idle_timeout is None else min(remaining, idle_timeout)
                readable, _, _ = select.select([gateway.socket], [], [], wait)
                if not readable:
                    return
                gateway.socket.settimeout(max(0.001, deadline - time.monotonic()))
                message = gateway._receive()
            except Exception:
                # _receive has no resumable frame buffer. An interrupted frame
                # invalidates this connection, even when the cause is a timeout.
                raise CannotConnect("The local gateway response was interrupted.") from None
            if not message:
                raise CannotConnect("The local gateway disconnected.")
            # Some BLE gateways explicitly reject this optional read operation.
            if getattr(message, "retcode", 0) not in (None, 0):
                if getattr(message, "cmd", None) in (tinytuya.DP_QUERY, tinytuya.DP_QUERY_NEW):
                    continue
                raise LocalError("The gateway returned a nonzero protocol result.")
            if not message.payload:
                yield {}
                continue
            try:
                response = gateway._decode_payload(message.payload)
            except Exception:
                raise LocalError("Could not decode a LAN response.") from None
            if isinstance(response, dict):
                yield response
        # A bounded batch must not leave an old ACK or topology queued behind
        # it before a fresh command. Fail closed if the input is still busy.
        try:
            readable, _, _ = select.select([gateway.socket], [], [], 0)
        except Exception:
            raise CannotConnect("Could not inspect the gateway connection.") from None
        if readable:
            raise CannotConnect("Too many queued gateway responses.")

    def _apply_topology(self, topology: Topology) -> None:
        self._topology_cache = topology
        for node_id in self._locks:
            previous = self._statuses.get(node_id, LockStatus(False))
            self._set_status(
                node_id,
                (
                    LockStatus(True, previous.battery, previous.is_locked)
                    if node_id in topology.online
                    else LockStatus(False)
                ),
            )

    def _handle_response(self, response: dict[str, Any]) -> None:
        self._last_received = time.monotonic()
        if response:
            self._received_report_count = min(self._received_report_count + 1, MAX_DIAGNOSTIC_COUNT)
        if response.get("reqType") == "subdev_online_stat_report":
            self._apply_topology(_topology(response))
            return
        for node_id, lock in self._locks.items():
            dps = matching_dps(response, node_id)
            if dps is None:
                continue
            self._observations.setdefault(node_id, _ObservationCounters()).observe(dps)
            previous = self._statuses.get(node_id, LockStatus(False))
            battery, locked = previous.battery, previous.is_locked
            if "8" in dps:
                value = dps["8"]
                battery = value if type(value) is int and 0 <= value <= 100 else None
            # HX201 firmware reports its latch through DP40. Other profiles use
            # motor DP47; never infer physical state from the DP61 acknowledgement.
            if lock.state_source == "hx201_dp40" and "40" in dps:
                value = dps["40"]
                locked = value == "closed" if value in ("open", "closed") else None
            elif lock.state_source == "motor_dp47" and "47" in dps:
                value = dps["47"]
                locked = not value if type(value) is bool else None
            self._set_status(node_id, LockStatus(True, battery, locked))

    def _drain(self, gateway: Any, *, apply: bool = True) -> None:
        """Process queued reports before sending; never consume them as command ACKs."""
        for response in self._read_decoded(
            gateway, time.monotonic() + CONNECTION_TIMEOUT, idle_timeout=0.05
        ):
            if apply:
                self._handle_response(response)
        try:
            readable, _, _ = select.select([gateway.socket], [], [], 0)
        except Exception:
            raise CannotConnect("Could not inspect the gateway connection.") from None
        if readable:
            raise CannotConnect("Could not drain queued gateway responses before sending.")

    def _query_topology(self, gateway: Any, *, apply: bool = True) -> Topology:
        self._drain(gateway, apply=apply)
        payload = gateway.generate_payload(
            tinytuya.LAN_EXT_STREAM,
            rawData={"cids": []},
            reqType="subdev_online_stat_query",
        )
        if gateway._send_receive_quick(payload, recv_retries=0) is not True:
            raise CannotConnect("Could not query the gateway topology.")
        for response in self._read_decoded(gateway, time.monotonic() + CONNECTION_TIMEOUT):
            if apply:
                self._handle_response(response)
            if response.get("reqType") == "subdev_online_stat_report":
                return _topology(response)
        raise CannotConnect("The gateway did not return a fresh subdevice topology report.")

    def _session_topology(self, gateway: Any) -> Topology:
        """Subscribe once; repeated LAN64 queries can stall some BLE gateways."""
        if self._topology_cache is None:
            return self._query_topology(gateway)
        self._drain(gateway)
        return self._topology_cache

    def probe(self) -> Topology:
        """Run a temporary read-only query without changing the live connection."""
        token = _PRIVATE_OPERATION.set(True)
        gateway = None
        try:
            gateway = self._new_gateway()
            self._open_gateway(gateway)
            return self._query_topology(gateway, apply=False)
        except LocalError:
            raise
        except Exception:
            raise CannotConnect("The local gateway operation failed.") from None
        finally:
            self._close_gateway(gateway)
            _PRIVATE_OPERATION.reset(token)

    def probe_live(self) -> Topology:
        """Reconnect for fresh discovery, then restore configured child status."""
        with self._operation():
            locks = list(self._locks.values())
            self._disconnect()
            if locks:
                self.poll(locks)
                assert self._topology_cache is not None
                return self._topology_cache
            return self._session_topology(self._connect())

    def _child(self, gateway: Any, lock: LockConfig) -> Any:
        child = tinytuya.Device(lock.device_id, cid=lock.node_id, parent=gateway)
        child.disabledetect = True
        return child

    def poll(self, locks: list[LockConfig]) -> dict[str, LockStatus]:
        """Initialize session topology once, then request optional child status."""
        with self._operation():
            if list(self._locks) != [lock.node_id for lock in locks]:
                self._poll_cursor = 0
            self._locks = {lock.node_id: lock for lock in locks}
            self._diagnostic_order = list(self._locks)
            self._diagnostic_sources = {
                lock.node_id: (
                    lock.state_source
                    if lock.state_source in ("motor_dp47", "hx201_dp40")
                    else "unsupported"
                )
                for lock in locks
            }
            self._statuses = {
                node_id: self._statuses.get(
                    node_id,
                    LockStatus(
                        self._topology_cache is not None and node_id in self._topology_cache.online
                    ),
                )
                for node_id in self._locks
            }
            gateway = self._connect()
            self._session_topology(gateway)
            deadline = time.monotonic() + STATUS_TIMEOUT
            # Continue where the previous bounded batch stopped, so slow
            # preparation or scheduling cannot indefinitely skip later locks.
            start = self._poll_cursor
            for offset in range(len(locks)):
                index = (start + offset) % len(locks)
                lock = locks[index]
                if not self._statuses[lock.node_id].online:
                    continue
                if time.monotonic() >= deadline:
                    break
                child = self._child(gateway, lock)
                payload = child.generate_payload(tinytuya.DP_QUERY)
                gateway.socket.settimeout(max(0.001, deadline - time.monotonic()))
                if child._send_receive_quick(payload, recv_retries=0) is not True:
                    raise CannotConnect("Could not request the lock status.")
                self._poll_cursor = (index + 1) % len(locks)
            for response in self._read_decoded(gateway, deadline):
                self._handle_response(response)
            return dict(self._statuses)

    def listen(self) -> None:
        """Receive live transitions with bounded waiting; never reconnect here."""
        with self._operation():
            gateway = self._gateway
            if gateway is None or gateway.socket is None:
                raise CannotConnect("The local gateway is not connected.")
            now = time.monotonic()
            if now - self._last_received >= HEARTBEAT_TIMEOUT:
                raise CannotConnect("The local gateway stopped responding.")
            if now - self._last_heartbeat >= HEARTBEAT_INTERVAL:
                gateway.socket.settimeout(LISTEN_TIMEOUT)
                payload = gateway.generate_payload(tinytuya.HEART_BEAT)
                if gateway._send_receive_quick(payload, recv_retries=0) is not True:
                    raise CannotConnect("Could not send the gateway heartbeat.")
                self._last_heartbeat = now
            for response in self._read_decoded(gateway, time.monotonic() + LISTEN_TIMEOUT):
                self._handle_response(response)

    def unlock(self, lock: LockConfig) -> None:
        """Send DP61 exactly once, while continuing to process physical reports."""
        raw = build_unlock(lock.member, lock.remote_unlock_code)
        if not lock.node_id or not lock.device_id or lock.device_id == self._gateway_id:
            raise UnlockError("The configured child identity is invalid; no unlock was sent.")
        with self._operation():
            self._locks[lock.node_id] = lock
            if lock.node_id not in self._diagnostic_order:
                self._diagnostic_order.append(lock.node_id)
            self._diagnostic_sources[lock.node_id] = (
                lock.state_source
                if lock.state_source in ("motor_dp47", "hx201_dp40")
                else "unsupported"
            )
            gateway = self._connect()
            self._session_topology(gateway)
            if not self._statuses.get(lock.node_id, LockStatus(False)).online:
                raise NodeUnavailable(
                    "The exact configured child is not online; no unlock was sent."
                )
            child = self._child(gateway, lock)
            self._drain(gateway)
            if gateway.socket is None:
                raise UnlockError("The gateway disconnected before unlock; no command was sent.")
            if not self._statuses.get(lock.node_id, LockStatus(False)).online:
                raise NodeUnavailable("The configured child went offline; no unlock was sent.")
            try:
                payload = child.generate_payload(
                    tinytuya.CONTROL, {"61": base64.b64encode(raw).decode("ascii")}
                )
            except Exception:
                raise UnlockError(
                    "Could not construct the unlock packet; no unlock was sent."
                ) from None
            try:
                gateway.socket.settimeout(CONNECTION_TIMEOUT)
                sent = child._send_receive_quick(payload, recv_retries=0)
            except Exception:
                sent = None
            if sent is not True:
                raise UnlockOutcomeUnknown(
                    "Unlock send outcome is unknown. Do not automatically repeat."
                )
            expected = b"\x00" + lock.member.to_bytes(2, "big")
            try:
                for response in self._read_decoded(gateway, time.monotonic() + UNLOCK_TIMEOUT):
                    self._handle_response(response)
                    dps = matching_dps(response, lock.node_id)
                    if dps is None or "61" not in dps:
                        continue
                    try:
                        value = base64.b64decode(dps["61"], validate=True)
                    except binascii.Error, ValueError, TypeError:
                        raise UnlockOutcomeUnknown(
                            "Malformed unlock response. Do not automatically repeat."
                        ) from None
                    if value != expected:
                        raise UnlockRejected(
                            "The unlock response was unexpected. Do not automatically repeat."
                        )
                    return
            except UnlockError:
                raise
            except Exception:
                raise UnlockOutcomeUnknown(
                    "Could not read the unlock result. Do not automatically repeat."
                ) from None
            raise UnlockOutcomeUnknown(
                "No matching unlock response arrived. Do not automatically repeat."
            )
