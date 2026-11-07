"""Read-only Tuya OpenAPI discovery used only while configuring a gateway."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from aiohttp import ClientError, ClientSession, ClientTimeout, ContentTypeError

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
_SERVICE_ERROR_CODES = {
    "1013",
    "1108",
    "1114",
    "1400",
    "28841001",
    "28841002",
    "28841003",
    "28841004",
    "28841101",
    "28841102",
    "28841103",
    "28841104",
    "28841105",
    "28841106",
    "60009001",
}
_KNOWN_ERROR_CODES = (
    _AUTH_ERROR_CODES
    | _SERVICE_ERROR_CODES
    | {
        "500",
        "1000",
        "1003",
        "1006",
        "1100",
        "1101",
        "1102",
        "1103",
        "1104",
        "1105",
        "1107",
        "1109",
        "1110",
        "1111",
        "1112",
        "1113",
        "1199",
        "2001",
        "2002",
        "2003",
        "2004",
        "2005",
        "2006",
        "2007",
        "2008",
        "2009",
        "2010",
        "2012",
        "2013",
        "2014",
        "2015",
        "40000901",
    }
)
_ERROR_STAGES = {
    "unknown",
    "token",
    "gateway_details",
    "gateway_children",
    "child_details",
    "child_specification",
}
_ERROR_REASONS = {
    "unknown",
    "connection",
    "timeout",
    "invalid_json",
    "http_error",
    "api_error",
    "invalid_response",
    "invalid_input",
    "invalid_token",
    "invalid_gateway",
    "missing_local_key",
    "invalid_local_key",
    "invalid_children",
    "conflicting_children",
    "invalid_child",
}
_DEVICE_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_NODE_ID = re.compile(r"[A-Za-z0-9:_-]{1,128}\Z")
_EMPTY_BODY_HASH = hashlib.sha256(b"").hexdigest()


class CloudError(Exception):
    """A sanitized failure to retrieve discovery metadata."""

    def __init__(
        self,
        message: str = "Unable to read Tuya discovery metadata",
        *,
        stage: str = "unknown",
        reason: str = "unknown",
        code: Any = None,
        status: Any = None,
    ) -> None:
        """Expose only fixed diagnostic values, never a raw server message."""
        super().__init__(message)
        self.stage = stage if isinstance(stage, str) and stage in _ERROR_STAGES else "unknown"
        self.reason = reason if isinstance(reason, str) and reason in _ERROR_REASONS else "unknown"
        if type(code) is int and 0 <= code <= 9_999_999_999:
            code = str(code)
        self.code = code if type(code) is str and code in _KNOWN_ERROR_CODES else None
        self.status = status if type(status) is int and 100 <= status <= 599 else None


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

    async def get(self, path: str, *, stage: str) -> Any:
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
        status = None
        try:
            async with self._session.get(
                self._endpoint + path,
                headers=headers,
                timeout=ClientTimeout(total=15),
                allow_redirects=False,
            ) as response:
                status = response.status
                if response.status in (401, 403):
                    raise CloudAuthError(
                        "Tuya rejected cloud access",
                        stage=stage,
                        reason="http_error",
                        status=status,
                    )
                if not 200 <= response.status < 300:
                    raise CloudError(
                        "Tuya cloud returned an HTTP error",
                        stage=stage,
                        reason="http_error",
                        status=status,
                    )
                try:
                    payload = await response.json()
                except ContentTypeError, ValueError, UnicodeError:
                    raise CloudError(
                        "Tuya cloud returned invalid JSON",
                        stage=stage,
                        reason="invalid_json",
                        status=status,
                    ) from None
        except TimeoutError:
            raise CloudError(
                "Tuya cloud request timed out", stage=stage, reason="timeout", status=status
            ) from None
        except ClientError:
            # Client exceptions can contain request URLs, headers, or bodies.
            raise CloudError(
                "Unable to connect to Tuya cloud", stage=stage, reason="connection", status=status
            ) from None
        except ValueError, UnicodeError:
            raise CloudError(
                "Unable to read a valid Tuya cloud response",
                stage=stage,
                reason="invalid_response",
                status=status,
            ) from None

        if not isinstance(payload, dict):
            raise CloudError(
                "Tuya cloud returned invalid metadata",
                stage=stage,
                reason="invalid_response",
                status=status,
            )
        if payload.get("success") is not True:
            code = payload.get("code")
            error = CloudError(
                "Tuya cloud rejected the discovery request",
                stage=stage,
                reason="api_error",
                code=code,
                status=status,
            )
            if error.code in _AUTH_ERROR_CODES:
                raise CloudAuthError(
                    "Tuya rejected cloud access",
                    stage=error.stage,
                    reason=error.reason,
                    code=error.code,
                    status=error.status,
                )
            raise error
        if "result" not in payload:
            raise CloudError(
                "Tuya cloud returned incomplete metadata",
                stage=stage,
                reason="invalid_response",
                status=status,
            )
        return payload["result"]

    async def authenticate(self) -> None:
        """Get a short-lived token; never expose refresh tokens to the caller."""
        result = await self.get("/v1.0/token?grant_type=1", stage="token")
        token = result.get("access_token") if isinstance(result, dict) else None
        if not isinstance(token, str) or not token or not token.isascii():
            raise CloudAuthError(
                "Tuya did not return a valid access token", stage="token", reason="invalid_token"
            )
        if any(not 32 < ord(character) < 127 for character in token):
            raise CloudAuthError(
                "Tuya did not return a valid access token", stage="token", reason="invalid_token"
            )
        self._token = token


def _text(value: Any, fallback: str) -> str:
    """Select a nonempty display label without retaining arbitrary metadata."""
    return value if isinstance(value, str) and value.strip() else fallback


def _product_id(value: Any) -> str | None:
    """Keep only a valid product identifier for selecting a known state profile."""
    return value if isinstance(value, str) and _DEVICE_ID.fullmatch(value) else None


def _unique_model_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Reject ambiguous JSON fields instead of silently choosing the last value."""
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate model field")
        result[key] = value
    return result


def _supports_model(result: Any) -> bool:
    """Require writable raw DP61 in one unambiguous default-service model."""
    if not isinstance(result, dict) or not isinstance(result.get("model"), str):
        return False
    try:
        model = json.loads(result["model"], object_pairs_hook=_unique_model_object)
    except ValueError, RecursionError:
        return False
    if not isinstance(model, dict) or not isinstance(model.get("services"), list):
        return False
    services = model["services"]
    if any(not isinstance(service, dict) for service in services):
        return False
    default_services = [service for service in services if service.get("code") == ""]
    if len(default_services) != 1:
        return False
    properties = default_services[0].get("properties")
    if not isinstance(properties, list):
        return False
    matching = []
    for property_definition in properties:
        if not isinstance(property_definition, dict):
            return False
        if property_definition.get("code") == "remote_no_dp_key" or property_definition.get(
            "abilityId"
        ) in (61, "61"):
            matching.append(property_definition)
    if len(matching) != 1:
        return False
    property_definition = matching[0]
    type_spec = property_definition.get("typeSpec")
    return (
        type(property_definition.get("abilityId")) is int
        and property_definition["abilityId"] == 61
        and property_definition.get("code") == "remote_no_dp_key"
        and property_definition.get("accessMode") in ("rw", "wr")
        and isinstance(type_spec, dict)
        and type_spec.get("type") == "raw"
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
        raise CloudError("Select a supported Tuya data center", reason="invalid_input")
    if not isinstance(gateway_id, str) or not _DEVICE_ID.fullmatch(gateway_id):
        raise CloudError("Enter a valid gateway device ID", reason="invalid_input")
    if not all(
        isinstance(value, str)
        and value
        and value.isascii()
        and all(32 < ord(character) < 127 for character in value)
        for value in (client_id, client_secret)
    ):
        raise CloudAuthError(
            "Enter valid Tuya project credentials", stage="token", reason="invalid_input"
        )

    client = _DiscoveryClient(session, REGION_ENDPOINTS[region], client_id, client_secret)
    await client.authenticate()
    gateway = await client.get(f"/v2.0/cloud/thing/{gateway_id}", stage="gateway_details")
    if not isinstance(gateway, dict) or gateway.get("id") != gateway_id:
        raise CloudError(
            "Tuya did not return the selected gateway",
            stage="gateway_details",
            reason="invalid_gateway",
        )
    # Some gateways report sub=true. The configuration flow verifies their LAN
    # key and child topology before saving, rather than trusting this cloud flag.
    local_key = gateway.get("local_key")
    if local_key is None or local_key == "":
        raise CloudError(
            "Tuya did not return a gateway local key",
            stage="gateway_details",
            reason="missing_local_key",
        )
    if (
        not isinstance(local_key, str)
        or len(local_key) != 16
        or any(not 32 <= ord(character) < 127 for character in local_key)
    ):
        raise CloudError(
            "Tuya did not return a valid gateway local key",
            stage="gateway_details",
            reason="invalid_local_key",
        )

    children = await client.get(f"/v1.0/devices/{gateway_id}/sub-devices", stage="gateway_children")
    if not isinstance(children, list):
        raise CloudError(
            "Tuya did not return a valid list of gateway children",
            stage="gateway_children",
            reason="invalid_children",
        )
    locks: list[DiscoveredLock] = []
    seen_devices: set[str] = set()
    seen_nodes: set[str] = set()
    for child in children:
        if not isinstance(child, dict):
            raise CloudError(
                "Tuya returned invalid child metadata",
                stage="gateway_children",
                reason="invalid_child",
            )
        device_id = child.get("id")
        if not isinstance(device_id, str) or not _DEVICE_ID.fullmatch(device_id):
            raise CloudError(
                "Tuya returned an invalid child device ID",
                stage="gateway_children",
                reason="invalid_child",
            )
        if device_id == gateway_id or device_id in seen_devices:
            raise CloudError(
                "Tuya returned conflicting child device IDs",
                stage="gateway_children",
                reason="conflicting_children",
            )
        seen_devices.add(device_id)
        node_id = child.get("node_id")
        if not isinstance(node_id, str) or not _NODE_ID.fullmatch(node_id):
            node_id = ""
            reason = "missing_node_id"
        elif node_id in seen_nodes:
            raise CloudError(
                "Tuya returned conflicting child node IDs",
                stage="gateway_children",
                reason="conflicting_children",
            )
        else:
            seen_nodes.add(node_id)
            reason = None
        display_metadata = child
        if not _text(child.get("name"), "") or _product_id(child.get("product_id")) is None:
            try:
                details = await client.get(f"/v2.0/cloud/thing/{device_id}", stage="child_details")
            except CloudAuthError:
                raise
            except CloudError as error:
                if error.code in _SERVICE_ERROR_CODES:
                    raise
                reason = reason or "metadata_unavailable"
            else:
                if not isinstance(details, dict) or details.get("id") != device_id:
                    raise CloudError(
                        "Tuya did not return the selected child",
                        stage="child_details",
                        reason="invalid_child",
                    )
                display_metadata = {**child, **details}
        if reason is None:
            try:
                model = await client.get(
                    f"/v2.0/cloud/thing/{device_id}/model", stage="child_specification"
                )
            except CloudAuthError:
                raise
            except CloudError as error:
                if error.code in _SERVICE_ERROR_CODES:
                    raise
                reason = "specification_unavailable"
            else:
                reason = None if _supports_model(model) else "unsupported_profile"
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
