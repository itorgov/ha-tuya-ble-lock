"""Read-only Tuya OpenAPI discovery used only while configuring a gateway."""

from __future__ import annotations

import hashlib
import hmac
import re
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout

REGION_ENDPOINTS = {
    "cn": "https://openapi.tuyacn.com",
    "us": "https://openapi.tuyaus.com",
    "eu": "https://openapi.tuyaeu.com",
    "in": "https://openapi.tuyain.com",
    "ue": "https://openapi-ueaz.tuyaus.com",
    "we": "https://openapi-weaz.tuyaeu.com",
    "sg": "https://openapi-sg.iotbing.com",
}

_AUTH_ERROR_CODES = {
    "1001",
    "1002",
    "1004",
    "1005",
    "1007",
    "1008",
    "1010",
    "1011",
    "1012",
    "1106",
    "2406",
    "28841105",
}
_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NODE_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
_EMPTY_BODY_HASH = hashlib.sha256(b"").hexdigest()


class CloudError(Exception):
    """A sanitized failure to retrieve discovery metadata."""


class CloudAuthError(CloudError):
    """The project credentials, region, or API permissions are invalid."""


@dataclass(frozen=True, slots=True, repr=False)
class DiscoveredLock:
    """A gateway child; only a verified DP profile is marked supported."""

    device_id: str
    node_id: str
    name: str
    product_name: str
    supported: bool
    unsupported_reason: str | None = None
    product_id: str | None = None


@dataclass(frozen=True, slots=True, repr=False)
class GatewayDiscovery:
    """Only the metadata needed for local configuration, with no API token."""

    gateway_id: str
    name: str
    local_key: str
    locks: list[DiscoveredLock]


class _DiscoveryClient:
    """An ephemeral client with no credential persistence or control methods."""

    def __init__(
        self, session: ClientSession, endpoint: str, client_id: str, client_secret: str
    ) -> None:
        self._session = session
        self._endpoint = endpoint
        self._client_id = client_id
        self._client_secret = client_secret
        self._token = ""

    async def get(self, path: str) -> Any:
        """Send a signed GET without following redirects or logging secrets."""
        timestamp = str(time.time_ns() // 1_000_000)
        string_to_sign = f"GET\n{_EMPTY_BODY_HASH}\n\n{path}"
        signature = (
            hmac.new(
                self._client_secret.encode(),
                f"{self._client_id}{self._token}{timestamp}{string_to_sign}".encode(),
                hashlib.sha256,
            )
            .hexdigest()
            .upper()
        )
        headers = {
            "client_id": self._client_id,
            "sign": signature,
            "sign_method": "HMAC-SHA256",
            "t": timestamp,
        }
        if self._token:
            headers["access_token"] = self._token
        try:
            async with self._session.get(
                self._endpoint + path,
                headers=headers,
                timeout=ClientTimeout(total=15),
                allow_redirects=False,
            ) as response:
                if response.status in (401, 403):
                    raise CloudAuthError("Tuya rejected cloud access")
                if not 200 <= response.status < 300:
                    raise CloudError("Tuya cloud returned an HTTP error")
                payload = await response.json()
        except ClientError, TimeoutError, ValueError, UnicodeError:
            # Client exceptions can contain request URLs, headers, or bodies.
            raise CloudError("Unable to read a valid Tuya cloud response") from None

        if not isinstance(payload, dict):
            raise CloudError("Tuya cloud returned invalid metadata")
        if payload.get("success") is not True:
            code = payload.get("code")
            if isinstance(code, (int, str)) and str(code) in _AUTH_ERROR_CODES:
                raise CloudAuthError("Tuya rejected cloud access")
            raise CloudError("Tuya cloud rejected the discovery request")
        if "result" not in payload:
            raise CloudError("Tuya cloud returned incomplete metadata")
        return payload["result"]

    async def authenticate(self) -> None:
        """Get a short-lived token; never expose refresh tokens to the caller."""
        result = await self.get("/v1.0/token?grant_type=1")
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token or not token.isascii():
            raise CloudAuthError("Tuya did not return a valid access token")
        if any(not 32 < ord(character) < 127 for character in token):
            raise CloudAuthError("Tuya did not return a valid access token")
        self._token = token


def _text(value: Any, fallback: str) -> str:
    """Select a nonempty display label without retaining arbitrary metadata."""
    return value if isinstance(value, str) and value.strip() else fallback


def _product_id(value: Any) -> str | None:
    """Keep only a valid product identifier for selecting a known state profile."""
    return value if isinstance(value, str) and _DEVICE_ID.fullmatch(value) else None


def _supports_unlock(specification: Any) -> bool:
    """Require the exact writable Raw DP 61, never infer a DP from its name."""
    if not isinstance(specification, dict):
        return False
    functions = specification.get("functions")
    if not isinstance(functions, list):
        return False
    matching = []
    for function in functions:
        if not isinstance(function, dict):
            return False
        # Tuya documents dp_id, but also uses dpId in its response example.
        dp_id = function.get("dp_id", function.get("dpId"))
        if function.get("code") == "remote_no_dp_key" or dp_id in (61, "61"):
            matching.append(function)
    if len(matching) != 1:
        return False
    function = matching[0]
    dp_id = function.get("dp_id", function.get("dpId"))
    dp_type = function.get("type")
    return (
        type(dp_id) in (int, str)
        and dp_id in (61, "61")
        and function.get("code") == "remote_no_dp_key"
        and isinstance(dp_type, str)
        and dp_type.lower() == "raw"
        and not (
            "dp_id" in function
            and "dpId" in function
            and str(function["dp_id"]) != str(function["dpId"])
        )
    )


async def async_discover_gateway(
    session: ClientSession,
    region: str,
    client_id: str,
    client_secret: str,
    gateway_id: str,
) -> GatewayDiscovery:
    """Discover existing children without pairing devices or issuing commands.

    The caller owns the session. Cloud credentials are only used during this
    call; the result contains the gateway LAN key, but no cloud credentials.
    A missing node ID or unverified writable DP profile is never guessed.
    """
    if region not in REGION_ENDPOINTS:
        raise CloudError("Select a supported Tuya data center")
    if not isinstance(gateway_id, str) or not _DEVICE_ID.fullmatch(gateway_id):
        raise CloudError("Enter a valid gateway device ID")
    if not all(
        isinstance(value, str)
        and value
        and value.isascii()
        and all(32 < ord(character) < 127 for character in value)
        for value in (client_id, client_secret)
    ):
        raise CloudAuthError("Enter valid Tuya project credentials")

    client = _DiscoveryClient(session, REGION_ENDPOINTS[region], client_id, client_secret)
    await client.authenticate()
    gateway = await client.get(f"/v1.1/iot-03/devices/{gateway_id}")
    if (
        not isinstance(gateway, dict)
        or gateway.get("id") != gateway_id
        or gateway.get("sub") is True
    ):
        raise CloudError("Tuya did not return the selected gateway")
    local_key = gateway.get("local_key")
    if (
        not isinstance(local_key, str)
        or len(local_key) != 16
        or any(not 32 <= ord(character) < 127 for character in local_key)
    ):
        raise CloudError("Tuya did not return a valid gateway local key")

    children = await client.get(f"/v1.0/iot-03/devices/{gateway_id}/sub-devices")
    if not isinstance(children, list):
        raise CloudError("Tuya did not return a valid list of gateway children")
    locks: list[DiscoveredLock] = []
    seen_devices: set[str] = set()
    seen_nodes: set[str] = set()
    for child in children:
        if not isinstance(child, dict):
            raise CloudError("Tuya returned invalid child metadata")
        device_id = child.get("id")
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise CloudError("Tuya returned an invalid child device ID")
        if device_id == gateway_id or device_id in seen_devices:
            raise CloudError("Tuya returned conflicting child device IDs")
        seen_devices.add(device_id)
        node_id = child.get("node_id")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            node_id = ""
            reason = "missing_node_id"
        elif node_id in seen_nodes:
            raise CloudError("Tuya returned conflicting child node IDs")
        else:
            seen_nodes.add(node_id)
            reason = None
        display_metadata = child
        if not _text(child.get("name"), "") or _product_id(child.get("product_id")) is None:
            try:
                details = await client.get(f"/v1.1/iot-03/devices/{device_id}")
            except CloudAuthError:
                raise
            except CloudError:
                reason = reason or "metadata_unavailable"
            else:
                if not isinstance(details, dict) or details.get("id") != device_id:
                    raise CloudError("Tuya did not return the selected child")
                display_metadata = {**child, **details}
        if reason is None:
            try:
                specification = await client.get(f"/v1.1/devices/{device_id}/specifications")
            except CloudAuthError:
                raise
            except CloudError:
                reason = "specification_unavailable"
            else:
                reason = None if _supports_unlock(specification) else "unsupported_profile"
        locks.append(
            DiscoveredLock(
                device_id=device_id,
                node_id=node_id,
                name=_text(display_metadata.get("name"), f"Tuya device {device_id}"),
                product_name=_text(display_metadata.get("product_name"), "Tuya Bluetooth device"),
                supported=reason is None,
                unsupported_reason=reason,
                product_id=_product_id(display_metadata.get("product_id")),
            )
        )

    return GatewayDiscovery(
        gateway_id=gateway_id,
        name=_text(gateway.get("name"), "Tuya BLE gateway"),
        local_key=local_key,
        locks=locks,
    )
