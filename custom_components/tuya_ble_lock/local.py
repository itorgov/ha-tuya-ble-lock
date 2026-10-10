"""Synchronous, credential-safe LAN transport for the supported DP61 lock profile.

All methods belong in Home Assistant's executor and must be serialized by the
caller. Every operation opens its own session. Unlock sends one control packet
at most; a later matching response does not establish physical lock state.
"""

# Sanitize every vendor exception at this credential boundary; deliberately do
# not log exceptions or their tracebacks, which may contain decrypted payloads.
# ruff: noqa: BLE001, S110

from __future__ import annotations

import base64
import binascii
import logging
import re
import select
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import tinytuya

PINNED_TINYTUYA = "1.20.0"
CONNECTION_TIMEOUT = 2.0
STATUS_TIMEOUT = 2.0
UNLOCK_TIMEOUT = 6.0
MAX_FRAMES = 64

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
    """Nodes observed in one fresh gateway topology report."""

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


@dataclass(frozen=True)
class LockStatus:
    """Fresh connectivity and optional battery; never an inferred lock state."""

    online: bool
    battery: int | None = None


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
    """A single gateway; all public operations close their LAN socket on exit."""

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

    @contextmanager
    def _session(self) -> Iterator[Any]:
        token = _PRIVATE_OPERATION.set(True)
        try:
            self._gateway = tinytuya.Device(
                self._gateway_id,
                self._host,
                self._local_key,
                version=self._version,
                persist=True,
                connection_timeout=CONNECTION_TIMEOUT,
                connection_retry_limit=1,
                connection_retry_delay=0,
            )
            self._gateway.disabledetect = True
            yield self._gateway
        except LocalError:
            raise
        except Exception:
            # Vendor exception messages and object representations can contain
            # keys, raw DPS or user identifiers. Never expose their content.
            raise CannotConnect("The local gateway operation failed.") from None
        finally:
            self.close()
            _PRIVATE_OPERATION.reset(token)

    def close(self) -> None:
        """Release the current socket, if any; safe to call more than once."""
        gateway, self._gateway = self._gateway, None
        if gateway is not None:
            try:
                gateway.close()
            except Exception:
                pass

    @staticmethod
    def _prepare(gateway: Any) -> Topology:
        """Read-only query before any mutation; do not use TinyTuya caches."""
        topology = _topology(gateway.subdev_query())
        if gateway.cmd_retcode not in (None, 0):
            raise CannotConnect("The gateway rejected the topology query.")
        # _get_socket cannot reconnect after this point, including if the socket
        # disappears between the identity check and the single control send.
        gateway.set_socketRetryLimit(0)
        return topology

    def probe(self) -> Topology:
        """Discover node connectivity using only a read-only topology query."""
        with self._session() as gateway:
            return self._prepare(gateway)

    @staticmethod
    def _read_decoded(gateway: Any, deadline: float) -> Iterator[dict[str, Any]]:
        for _ in range(MAX_FRAMES):
            remaining = deadline - time.monotonic()
            if remaining <= 0 or gateway.socket is None:
                return
            gateway.socket.settimeout(remaining)
            try:
                message = gateway._receive()
            except OSError, ValueError:
                # A timeout can split a frame. Do not resume that connection's
                # framing or retry a previously sent command.
                return
            except Exception:
                raise LocalError("Could not decode a LAN response.") from None
            if not message:
                return
            if getattr(message, "retcode", 0) not in (None, 0):
                raise LocalError("The gateway returned a nonzero protocol result.")
            if not message.payload:
                continue
            try:
                response = gateway._decode_payload(message.payload)
            except Exception:
                raise LocalError("Could not decode a LAN response.") from None
            if isinstance(response, dict):
                yield response

    def poll(self, locks: list[LockConfig]) -> dict[str, LockStatus]:
        """Read topology and optional battery with one shared child-query budget.

        BLE gateways can reject DP_QUERY while supporting topology and unlock.
        Missing or partial DPS therefore never invalidate a topology report.
        """
        with self._session() as gateway:
            topology = self._prepare(gateway)
            statuses = {
                lock.node_id: LockStatus(online=lock.node_id in topology.online) for lock in locks
            }
            if not any(status.online for status in statuses.values()):
                return statuses
            deadline = time.monotonic() + STATUS_TIMEOUT
            try:
                for lock in locks:
                    if not statuses[lock.node_id].online:
                        continue
                    remaining = deadline - time.monotonic()
                    if remaining <= 0 or gateway.socket is None:
                        break
                    gateway.socket.settimeout(min(0.25, remaining))
                    child = tinytuya.Device(lock.device_id, cid=lock.node_id, parent=gateway)
                    child.disabledetect = True
                    payload = child.generate_payload(tinytuya.DP_QUERY)
                    if child._send_receive_quick(payload, recv_retries=0) is not True:
                        break
                for response in self._read_decoded(gateway, deadline):
                    for node_id, status in statuses.items():
                        dps = matching_dps(response, node_id) if status.online else None
                        battery = dps.get("8") if dps is not None else None
                        if type(battery) is int and 0 <= battery <= 100:
                            statuses[node_id] = LockStatus(online=True, battery=battery)
            except Exception:
                # Optional status is deliberately best effort; raw response data
                # and vendor exceptions must not escape into logs or entities.
                pass
            return statuses

    @staticmethod
    def _drain_before_write(gateway: Any) -> None:
        if gateway.socket is None:
            raise UnlockError("The gateway disconnected before unlock; no command was sent.")
        deadline = time.monotonic() + CONNECTION_TIMEOUT
        for _ in range(MAX_FRAMES):
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                readable, _, _ = select.select([gateway.socket], [], [], 0.05)
                if not readable:
                    return
                gateway.socket.settimeout(remaining)
                if not gateway._receive():
                    raise OSError
            except Exception:
                raise UnlockError(
                    "Could not clear old LAN responses; no unlock was sent."
                ) from None
        raise UnlockError("Too many queued responses; no unlock was sent.")

    def unlock(self, lock: LockConfig) -> None:
        """Send DP61 once after a fresh topology report for the exact child.

        Returning means a matching DP61 response was observed after the send.
        It establishes neither response correlation nor physical actuation.
        The caller must keep physical lock state unknown.
        """
        raw = build_unlock(lock.member, lock.remote_unlock_code)
        if not lock.node_id or not lock.device_id or lock.device_id == self._gateway_id:
            raise UnlockError("The configured child identity is invalid; no unlock was sent.")
        with self._session() as gateway:
            topology = self._prepare(gateway)
            if lock.node_id not in topology.online or lock.node_id in topology.offline:
                raise NodeUnavailable(
                    "The exact configured child is not online; no unlock was sent."
                )
            child = tinytuya.Device(lock.device_id, cid=lock.node_id, parent=gateway)
            child.disabledetect = True
            self._drain_before_write(gateway)
            try:
                payload = child.generate_payload(
                    tinytuya.CONTROL, {"61": base64.b64encode(raw).decode("ascii")}
                )
            except Exception:
                raise UnlockError(
                    "Could not construct the unlock packet; no unlock was sent."
                ) from None
            try:
                # The pinned private helper has exactly one sendall and no send
                # retry. Do not replace it with set_value() or _send_receive().
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
