"""UI setup, gateway reconfiguration and per-lock credential management."""

import ipaddress
import re
import time
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import (
    ATTR_AREA_ID as CONF_AREA_ID,
)
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_NAME,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    AreaSelector,
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .cloud import CloudAuthError, CloudError, async_discover_gateway, async_read_unlock_credential
from .const import (
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
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    HX201_PRODUCT_ID,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
    STATE_SOURCE_HX201_DP40,
    STATE_SOURCE_MOTOR_DP47,
)
from .credentials import decode_unlock_command
from .local import GatewayClient, LocalError, Topology, build_unlock

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_AUTOMATIC_GATEWAY_VERSIONS = {"csvxfq3ecmedidkh": "3.5"}
_REGIONS = {
    "cn": "China",
    "us": "Western America",
    "ue": "Eastern America",
    "eu": "Central Europe",
    "we": "Western Europe",
    "in": "India",
    "sg": "Singapore",
}
_CLOUD_STAGES = {
    "unknown": "contacting Tuya",
    "token": "requesting an access token",
    "gateway_details": "reading the gateway details",
    "gateway_children": "listing the gateway's locks",
    "child_details": "reading a lock's details",
    "child_specification": "checking a lock's supported commands",
    "unlock_logs": "reading a lock's recent unlock commands",
}
_CLOUD_REASONS = {
    "connection": "Cannot reach Tuya. Check Home Assistant's internet connection and DNS, then retry.",
    "timeout": "Tuya did not respond in time. Check connectivity and try again shortly.",
    "invalid_json": "Tuya returned an unreadable response. Try again later; check any proxy or network filter if it persists.",
    "http_error": "Tuya rejected the HTTP request. Check the data center and project access, then retry.",
    "api_error": "Tuya rejected the request. Check project linking, API permissions, subscription and the gateway Device ID.",
    "invalid_response": "Tuya returned incomplete device information. Retry; if it persists, report the failed stage without sharing credentials.",
    "invalid_input": "Check the project Access ID, Access Secret, data center and gateway Device ID.",
    "invalid_token": "Tuya did not return a usable access token. Check project credentials and the data center, then retry.",
    "invalid_gateway": "Tuya did not return the requested gateway. Check its Device ID, linked app account and data center.",
    "missing_local_key": "The gateway details contain no Local Key. Check project device permissions and that you entered the gateway's Device ID.",
    "invalid_local_key": "Tuya returned an unusable gateway Local Key. Check the gateway Device ID and retry discovery.",
    "invalid_children": "Tuya returned an invalid gateway device list. Check the locks are linked to this gateway in the app, then retry.",
    "conflicting_children": "Tuya returned conflicting lock identities. Check the gateway's device list in the app, then retry.",
    "invalid_child": "Tuya returned an invalid lock identity. Check the gateway's device list and linked app account, then retry.",
    "incomplete_logs": "Tuya returned incomplete command history. Unlock this lock once in the app, then retry.",
}
# Fixed guidance for documented Tuya codes; never display the vendor's message.
# https://developer.tuya.com/en/docs/iot/error-code?id=K989ruxx88swc
_CLOUD_CODE_DETAILS = {
    **dict.fromkeys(
        ("1001", "1004", "1005"),
        "Tuya rejected the project credentials or signature. Check the Access ID, Access Secret and selected data center.",
    ),
    **dict.fromkeys(
        ("1002", "1010", "1011", "1012", "1400"),
        "Tuya rejected the access token. Submit again for a new token; if it persists, check project credentials and the data center.",
    ),
    **dict.fromkeys(
        ("1106", "2406", "28841105", "60009001"),
        "Check that the required APIs are authorized for this project and that its linked app account grants access to the gateway and locks.",
    ),
    **dict.fromkeys(
        ("28841001", "28841101", "28841106"),
        "The required cloud service is not subscribed. Check the project's service subscriptions and API authorization on the Tuya Developer Platform.",
    ),
    **dict.fromkeys(
        ("28841002", "28841102"),
        "The cloud service subscription has expired. Review its renewal or trial-extension options on the Tuya Developer Platform.",
    ),
    **dict.fromkeys(
        ("28841003", "28841103"),
        "Tuya reports an overdue cloud service bill. Review the service's billing status on the Tuya Developer Platform.",
    ),
    **dict.fromkeys(
        ("28841004", "28841104"),
        "The cloud service quota is exhausted. Wait for its reset or review the project's quota options on the Tuya Developer Platform.",
    ),
    **dict.fromkeys(
        ("500", "1110", "1111", "1112", "1113", "1199"),
        "Tuya is busy, synchronizing data or limiting requests. Wait a few minutes before retrying.",
    ),
    **dict.fromkeys(
        ("2010", "40000901"),
        "Tuya cannot find the device. Check the gateway Device ID, selected data center and linked app account's device list.",
    ),
    "1013": "Check that the Home Assistant host's date and time are correct and automatically synchronized, then retry.",
    "1108": "Tuya rejected the API address. Check the selected data center and integration version; report this code if it persists.",
    "1114": "Check the project's cloud authorization IP allowlist: it must permit the public IP used by Home Assistant.",
}


@dataclass(frozen=True, repr=False)
class _CloudAccess:
    """Discovery credentials retained only for the lifetime of this setup flow."""

    region: str
    client_id: str
    client_secret: str


def _cloud_error_details(error: CloudError) -> dict[str, str] | None:
    """Translate only validated diagnostic metadata, including on legacy errors."""
    # Revalidate at the UI boundary in case a caller modified exception attrs.
    safe = CloudError(
        stage=getattr(error, "stage", "unknown"),
        reason=getattr(error, "reason", "unknown"),
        code=getattr(error, "code", None),
        status=getattr(error, "status", None),
    )
    if safe.reason not in _CLOUD_REASONS:
        return None
    details = _CLOUD_REASONS[safe.reason]
    reference = ""
    if safe.code is not None:
        reference = f"(Tuya error {safe.code})"
        details = _CLOUD_CODE_DETAILS.get(safe.code, details)
    elif safe.status is not None and safe.status >= 300:
        reference = f"(HTTP {safe.status})"
        if safe.status in (401, 403):
            details = (
                "Check the project credentials, selected data center and authorized API services."
            )
        elif safe.status == 429:
            details = "Tuya is limiting requests. Wait a few minutes before retrying."
        elif safe.status >= 500:
            details = "Tuya's service is unavailable. Try again later."
    return {"stage": _CLOUD_STAGES[safe.stage], "details": details, "reference": reference}


def _identifier(value: Any) -> str:
    """Validate opaque Tuya IDs without transforming their identity."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("Invalid identifier")
    return value


def _node_identifier(value: Any) -> str:
    """Keep the gateway's opaque node identifier, including separators."""
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,128}", value):
        raise ValueError("Invalid node identifier")
    return value


def _validate_gateway(data: dict[str, Any]) -> dict[str, Any]:
    """Validate local connection fields without network access."""
    data = dict(data)
    data[CONF_HOST] = str(ipaddress.IPv4Address(data[CONF_HOST]))
    _identifier(data[CONF_GATEWAY_ID])
    if len(data[CONF_LOCAL_KEY].encode("latin1")) != 16:
        raise ValueError("Invalid local key")
    if data[CONF_VERSION] not in ("3.3", "3.4", "3.5"):
        raise ValueError("Invalid protocol")
    if not isinstance(data[CONF_NAME], str) or not data[CONF_NAME].strip():
        raise ValueError("Invalid gateway name")
    return data


def _credentials(data: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    """Extract only an existing DP61 command; never generate or register keys."""
    result = dict(previous)
    result[CONF_NAME] = data[CONF_NAME].strip()
    if not result[CONF_NAME] or not data.get(CONF_PROFILE):
        raise ValueError("Profile must be confirmed")
    state_source = data.get(
        CONF_STATE_SOURCE, previous.get(CONF_STATE_SOURCE, STATE_SOURCE_MOTOR_DP47)
    )
    if state_source not in (STATE_SOURCE_MOTOR_DP47, STATE_SOURCE_HX201_DP40):
        raise ValueError("Invalid lock state source")
    member = data.get(CONF_MEMBER, previous.get(CONF_MEMBER, 1))
    code = data.get(CONF_REMOTE_CODE) or previous.get(CONF_REMOTE_CODE)
    if data.get("clear_code"):
        code = None
    if not data.get("clear_code") and (payload := data.get(CONF_PAYLOAD)):
        credential = decode_unlock_command(payload)
        member, code = credential.member, credential.code
    if code is not None:
        build_unlock(member, code)
    elif type(member) is not int or not 1 <= member <= 65534:
        raise ValueError("Invalid member")
    result.update({CONF_MEMBER: member, CONF_REMOTE_CODE: code, CONF_STATE_SOURCE: state_source})
    return result


class TuyaBLELockFlow(ConfigFlow, domain=DOMAIN):
    """Store one entry per gateway and one child record per lock."""

    VERSION = 1

    def __init__(self) -> None:
        """Keep cloud credentials and uncommitted edits in memory only."""
        self._data: dict[str, Any] = {CONF_LOCKS: []}
        self._topology = Topology(frozenset(), frozenset())
        self._discovered: list[Any] = []
        self._pending: list[dict[str, Any]] = []
        self._editing: dict[str, Any] | None = None
        self._reconfigure = False
        self._automatic = False
        self._cloud_from_advanced = False
        self._cloud_region = "eu"
        self._cloud_host = ""
        self._cloud_access: _CloudAccess | None = None
        self._auto_lock_ids: set[str] = set()
        self._import_since_ms: dict[str, int] = {}
        self._import_errors: dict[str, str] = {}
        self._import_placeholders: dict[str, str] = {}
        self._removed = False

    @callback
    def async_remove(self) -> None:
        """Discard transient cloud access when setup completes or is cancelled."""
        self._removed = True
        self._cloud_access = None
        self._auto_lock_ids.clear()
        self._import_since_ms.clear()
        self._import_errors.clear()
        self._import_placeholders.clear()
        self._pending.clear()
        super().async_remove()

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PollingOptionsFlow:
        """Offer non-credential runtime options."""
        return PollingOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Choose cloud-assisted discovery or manual local setup."""
        return self.async_show_menu(step_id="user", menu_options=["cloud", "gateway"])

    async def async_step_cloud(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Read cloud metadata and verify the gateway without exposing technical settings."""
        if user_input is None:
            self._cloud_from_advanced = (
                self.cur_step is not None and self.cur_step.get("step_id") == "manage"
            )
        self._automatic = True
        errors = {}
        placeholders = None
        if user_input is not None:
            self._cloud_access = None
            self._auto_lock_ids.clear()
            region = user_input.get("region")
            if type(region) is str and region in _REGIONS:
                self._cloud_region = region
            gateway_id = (
                self._data.get(CONF_GATEWAY_ID)
                if CONF_GATEWAY_ID in self._data
                else user_input[CONF_GATEWAY_ID]
            )
            try:
                host = str(ipaddress.IPv4Address(user_input[CONF_HOST]))
                self._cloud_host = host
                _identifier(gateway_id)
                discovery = await async_discover_gateway(
                    async_get_clientsession(self.hass),
                    user_input["region"],
                    user_input["client_id"],
                    user_input["client_secret"],
                    gateway_id,
                )
            except CloudError as error:
                if self._removed:
                    raise UnknownFlow from None
                placeholders = _cloud_error_details(error)
                errors["base"] = (
                    "cloud_request_failed"
                    if placeholders is not None
                    else "invalid_auth"
                    if isinstance(error, CloudAuthError)
                    else "cloud_error"
                )
            except ValueError:
                errors["base"] = "invalid_automatic_input"
            else:
                if self._removed:
                    raise UnknownFlow
                version = self._data.get(CONF_VERSION) or _AUTOMATIC_GATEWAY_VERSIONS.get(
                    discovery.product_id
                )
                if version is None:
                    errors["base"] = "automatic_gateway_unsupported"
                else:
                    candidate = {
                        **self._data,
                        CONF_NAME: self._data.get(CONF_NAME)
                        or discovery.name
                        or "Tuya BLE gateway",
                        CONF_HOST: host,
                        CONF_GATEWAY_ID: discovery.gateway_id,
                        CONF_LOCAL_KEY: discovery.local_key,
                        CONF_VERSION: version,
                    }
                    try:
                        candidate = _validate_gateway(candidate)
                        topology = await self._async_probe_gateway(candidate)
                    except ValueError, UnicodeError, KeyError, TypeError:
                        errors["base"] = "invalid_automatic_input"
                    except LocalError:
                        if self._removed:
                            raise UnknownFlow from None
                        errors["base"] = "cannot_connect_automatic"
                    else:
                        self._data = candidate
                        self._topology = topology
                        self._discovered = list(discovery.locks)
                        self._cloud_access = _CloudAccess(
                            user_input["region"],
                            user_input["client_id"],
                            user_input["client_secret"],
                        )
                        self._auto_lock_ids = {
                            lock.device_id
                            for lock in self._discovered
                            if lock.supported and lock.product_id == HX201_PRODUCT_ID
                        }
                        return await self.async_step_select_locks()
        schema = {
            vol.Required("region", default=self._cloud_region): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": key, "label": label} for key, label in _REGIONS.items()]
                )
            ),
            vol.Required(vol.Secret("client_id")): _PASSWORD,
            vol.Required(vol.Secret("client_secret")): _PASSWORD,
            vol.Required(CONF_HOST, default=self._cloud_host or self._data.get(CONF_HOST, "")): str,
        }
        if CONF_GATEWAY_ID not in self._data:
            schema[vol.Required(CONF_GATEWAY_ID)] = str
        return self.async_show_form(
            step_id="cloud",
            data_schema=vol.Schema(schema),
            errors=errors,
            description_placeholders=placeholders,
        )

    async def async_step_gateway(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Verify the local gateway using a read-only topology query."""
        self._automatic = False
        errors = {}
        if user_input is not None:
            candidate = {**self._data, **user_input}
            if not user_input.get(CONF_LOCAL_KEY):
                candidate[CONF_LOCAL_KEY] = self._data.get(CONF_LOCAL_KEY, "")
            if CONF_GATEWAY_ID in self._data:
                candidate[CONF_GATEWAY_ID] = self._data[CONF_GATEWAY_ID]
            try:
                candidate = _validate_gateway(candidate)
                topology = await self._async_probe_gateway(candidate)
            except ValueError, UnicodeError, KeyError, TypeError:
                errors["base"] = "invalid_input"
            except LocalError:
                if self._removed:
                    raise UnknownFlow from None
                errors["base"] = "cannot_connect"
            else:
                self._data = candidate
                self._topology = topology
                if self._discovered:
                    return await self.async_step_select_locks()
                return await self.async_step_manage()
        fields = {
            vol.Required(
                CONF_NAME, default=self._data.get(CONF_NAME, "Tuya Bluetooth gateway")
            ): str,
            vol.Required(CONF_HOST, default=self._data.get(CONF_HOST, "")): str,
            vol.Required(CONF_VERSION, default=self._data.get(CONF_VERSION, "3.5")): SelectSelector(
                SelectSelectorConfig(options=["3.3", "3.4", "3.5"])
            ),
        }
        if CONF_GATEWAY_ID not in self._data:
            fields[vol.Required(CONF_GATEWAY_ID, default=self._data.get(CONF_GATEWAY_ID, ""))] = str
        key_marker = vol.Optional if self._data.get(CONF_LOCAL_KEY) else vol.Required
        fields[key_marker(vol.Secret(CONF_LOCAL_KEY))] = _PASSWORD
        return self.async_show_form(
            step_id="gateway", data_schema=vol.Schema(fields), errors=errors
        )

    async def _async_probe_gateway(self, candidate: dict[str, Any]) -> Topology:
        """Share the existing connection when reconfiguring a loaded gateway."""
        await self.async_set_unique_id(candidate[CONF_GATEWAY_ID])
        if not self._reconfigure:
            self._abort_if_unique_id_configured()
        client = GatewayClient(
            candidate[CONF_HOST],
            candidate[CONF_GATEWAY_ID],
            candidate[CONF_LOCAL_KEY],
            float(candidate[CONF_VERSION]),
        )
        entry = self._get_reconfigure_entry() if self._reconfigure else None
        coordinator = getattr(entry, "runtime_data", None)
        if entry is not None and entry.state is ConfigEntryState.LOADED and coordinator:
            topology = await coordinator.async_probe(
                client,
                reuse_connection=all(
                    candidate[key] == entry.data[key]
                    for key in (CONF_HOST, CONF_GATEWAY_ID, CONF_LOCAL_KEY, CONF_VERSION)
                ),
            )
        else:
            topology = await self.hass.async_add_executor_job(client.probe)
        if self._removed:
            raise UnknownFlow
        return topology

    def _duplicate(self, device_id: str, node_id: str) -> bool:
        """Prevent duplicate children across gateways and within this gateway."""
        for lock in self._data[CONF_LOCKS]:
            if lock[CONF_DEVICE_ID] == device_id or lock[CONF_NODE_ID] == node_id:
                return True
        for entry in self._async_current_entries():
            if self._reconfigure and entry.entry_id == self._get_reconfigure_entry().entry_id:
                continue
            if any(lock[CONF_DEVICE_ID] == device_id for lock in entry.data.get(CONF_LOCKS, [])):
                return True
        return False

    async def async_step_select_locks(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Only offer children with the explicit writable DP61 signature."""
        candidates = {
            lock.device_id: lock
            for lock in self._discovered
            if lock.supported
            and (not self._automatic or lock.device_id in self._auto_lock_ids)
            and lock.node_id in self._topology.online | self._topology.offline
            and not self._duplicate(lock.device_id, lock.node_id)
        }
        errors = {}
        if user_input is not None:
            selected = user_input.get(CONF_LOCKS, [])
            if (
                not selected
                or len(selected) != len(set(selected))
                or any(item not in candidates for item in selected)
            ):
                errors["base"] = "no_locks_selected"
            else:
                self._pending = [
                    {
                        CONF_DEVICE_ID: candidates[item].device_id,
                        CONF_NODE_ID: candidates[item].node_id,
                        CONF_NAME: candidates[item].name,
                        CONF_MEMBER: 1,
                        CONF_STATE_SOURCE: (
                            STATE_SOURCE_HX201_DP40
                            if candidates[item].product_id == HX201_PRODUCT_ID
                            else STATE_SOURCE_MOTOR_DP47
                        ),
                    }
                    for item in selected
                ]
                return await self._async_prepare_current_lock()
        if not candidates:
            self._discovered = []
            return self.async_show_form(
                step_id="gateway_ready"
                if self._automatic and self._data[CONF_LOCKS]
                else "no_locks_manual"
                if self._cloud_from_advanced
                else "no_locks",
                data_schema=vol.Schema({}),
                last_step=not self._cloud_from_advanced or bool(self._data[CONF_LOCKS]),
            )
        return self.async_show_form(
            step_id="select_locks",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_LOCKS): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {"value": key, "label": value.name}
                                for key, value in candidates.items()
                            ],
                            multiple=True,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_no_locks(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Finish an empty automatic search without creating a gateway-only entry."""
        if self._automatic:
            return self.async_abort(reason="no_new_locks")
        return await self.async_step_manage()

    async def async_step_gateway_ready(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Save refreshed connection details while keeping existing locks unchanged."""
        return await self.async_step_finish()

    async def async_step_no_locks_manual(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Keep unsaved manual changes available after an empty cloud search."""
        self._automatic = False
        return await self.async_step_manage()

    async def async_step_manage(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Add, edit, remove or finish without writing any device settings."""
        options = ["manual_lock", "cloud"]
        if self._data[CONF_LOCKS]:
            options.extend(["edit_lock", "remove_lock"])
        options.append("finish")
        return self.async_show_menu(step_id="manage", menu_options=options)

    async def async_step_manual_lock(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Bind an explicitly identified child; never derive its node ID."""
        errors = {}
        if user_input is not None:
            try:
                device_id = _identifier(user_input[CONF_DEVICE_ID])
                node_id = _node_identifier(user_input[CONF_NODE_ID])
                if device_id == self._data[CONF_GATEWAY_ID]:
                    raise ValueError("Gateway is not a child")
            except KeyError, ValueError:
                errors["base"] = "invalid_input"
            else:
                if self._duplicate(device_id, node_id):
                    errors["base"] = "duplicate_lock"
                elif node_id not in self._topology.online | self._topology.offline:
                    errors["base"] = "node_not_found"
                else:
                    self._pending = [
                        {
                            CONF_DEVICE_ID: device_id,
                            CONF_NODE_ID: node_id,
                            CONF_NAME: user_input[CONF_NAME],
                            CONF_MEMBER: 1,
                        }
                    ]
                    return await self.async_step_lock()
        return self.async_show_form(
            step_id="manual_lock",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME): str,
                    vol.Required(CONF_DEVICE_ID): str,
                    vol.Required(CONF_NODE_ID): str,
                }
            ),
            errors=errors,
        )

    async def async_step_lock(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Confirm compatibility and accept optional existing credentials."""
        lock = self._editing if self._editing is not None else self._pending[0]
        errors = {}
        if user_input is not None:
            try:
                updated = _credentials(user_input, lock)
            except ValueError, LocalError:
                errors["base"] = "invalid_credentials"
            else:
                return await self._async_finish_current_lock(updated)
        return self.async_show_form(
            step_id="lock",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=lock[CONF_NAME]): str,
                    vol.Required(CONF_PROFILE, default=False): BooleanSelector(),
                    vol.Required(
                        CONF_STATE_SOURCE,
                        default=lock.get(CONF_STATE_SOURCE, STATE_SOURCE_MOTOR_DP47),
                    ): SelectSelector(
                        SelectSelectorConfig(
                            options=[STATE_SOURCE_MOTOR_DP47, STATE_SOURCE_HX201_DP40],
                            translation_key="state_source",
                        )
                    ),
                    vol.Required(CONF_MEMBER, default=lock.get(CONF_MEMBER, 1)): vol.All(
                        vol.Coerce(int), vol.Range(min=1, max=65534)
                    ),
                    vol.Optional(vol.Secret(CONF_REMOTE_CODE)): _PASSWORD,
                    vol.Optional(vol.Secret(CONF_PAYLOAD)): _PASSWORD,
                    vol.Optional("clear_code", default=False): BooleanSelector(),
                }
            ),
            errors=errors,
        )

    async def _async_prepare_current_lock(self) -> ConfigFlowResult:
        """Import only for a newly selected model verified during this cloud flow."""
        lock = self._pending[0]
        if self._cloud_access is None or lock[CONF_DEVICE_ID] not in self._auto_lock_ids:
            return await self.async_step_lock()
        return await self._async_import_current_lock()

    async def _async_import_current_lock(self) -> ConfigFlowResult:
        """Read command logs once without sending any command to the lock."""
        lock = self._pending[0]
        access = self._cloud_access
        device_id = lock[CONF_DEVICE_ID]
        if access is None or device_id not in self._auto_lock_ids:
            return await self.async_step_lock()
        self._import_errors = {}
        self._import_placeholders = {"lock_name": lock[CONF_NAME]}
        try:
            credential = await async_read_unlock_credential(
                async_get_clientsession(self.hass),
                access.region,
                access.client_id,
                access.client_secret,
                device_id,
                since_ms=self._import_since_ms.get(device_id),
            )
        except CloudError as error:
            if self._removed:
                raise UnknownFlow from None
            details = _cloud_error_details(error)
            self._import_errors["base"] = (
                "cloud_request_failed" if details is not None else "unlock_import_failed"
            )
            if details is not None:
                self._import_placeholders.update(details)
        else:
            if self._removed:
                raise UnknownFlow
            if credential is not None:
                self._pending[0] = _credentials(
                    {
                        CONF_NAME: lock[CONF_NAME],
                        CONF_PROFILE: True,
                        CONF_MEMBER: credential.member,
                        CONF_REMOTE_CODE: credential.code,
                    },
                    lock,
                )
                return await self.async_step_lock_ready()
        # Keep the first prompt's cutoff across retries. A delayed command can
        # then arrive without allowing an older credential to be reused.
        self._import_since_ms.setdefault(device_id, time.time_ns() // 1_000_000)
        return await self.async_step_unlock_import()

    async def async_step_unlock_import(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Retry after the owner opens this lock through the app."""
        if user_input is not None:
            return await self._async_import_current_lock()
        return self.async_show_form(
            step_id="unlock_import",
            data_schema=vol.Schema({}),
            errors=self._import_errors,
            description_placeholders=self._import_placeholders,
        )

    async def async_step_lock_ready(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Name and place a verified lock without exposing its technical settings."""
        lock = self._pending[0]
        errors = {}
        if user_input is not None:
            name = user_input[CONF_NAME].strip()
            area_id = user_input.get(CONF_AREA_ID)
            if not name:
                errors[CONF_NAME] = "invalid_name"
            if area_id is not None and ar.async_get(self.hass).async_get_area(area_id) is None:
                errors[CONF_AREA_ID] = "invalid_area"
            if not errors:
                if area_id is not None:
                    self._data.setdefault(CONF_AREAS, {})[lock[CONF_DEVICE_ID]] = area_id
                return await self._async_finish_current_lock({**lock, CONF_NAME: name})
        return self.async_show_form(
            step_id="lock_ready",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_NAME,
                        default=user_input[CONF_NAME]
                        if user_input is not None
                        else lock[CONF_NAME],
                    ): str,
                    vol.Optional(CONF_AREA_ID): AreaSelector(),
                }
            ),
            errors=errors,
            description_placeholders={"lock_name": lock[CONF_NAME]},
            last_step=len(self._pending) == 1,
        )

    async def _async_finish_current_lock(self, updated: dict[str, Any]) -> ConfigFlowResult:
        """Stage exactly one lock, preserving the identities and credentials of siblings."""
        if self._editing is not None:
            self._data[CONF_LOCKS] = [
                updated if item[CONF_DEVICE_ID] == self._editing[CONF_DEVICE_ID] else item
                for item in self._data[CONF_LOCKS]
            ]
            self._editing = None
        else:
            self._data[CONF_LOCKS].append(updated)
            self._pending.pop(0)
        self._import_since_ms.pop(updated[CONF_DEVICE_ID], None)
        self._import_errors = {}
        self._import_placeholders = {}
        if self._pending:
            return await self._async_prepare_current_lock()
        if self._automatic:
            return await self.async_step_finish()
        return await self.async_step_manage()

    def _lock_selection_schema(self) -> vol.Schema:
        return vol.Schema(
            {
                vol.Required(CONF_DEVICE_ID): SelectSelector(
                    SelectSelectorConfig(
                        options=[
                            {"value": lock[CONF_DEVICE_ID], "label": lock[CONF_NAME]}
                            for lock in self._data[CONF_LOCKS]
                        ]
                    )
                )
            }
        )

    async def async_step_edit_lock(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Edit a stored code or member while keeping the device identity."""
        if user_input is not None:
            self._editing = next(
                lock
                for lock in self._data[CONF_LOCKS]
                if lock[CONF_DEVICE_ID] == user_input[CONF_DEVICE_ID]
            )
            return await self.async_step_lock()
        return self.async_show_form(step_id="edit_lock", data_schema=self._lock_selection_schema())

    async def async_step_remove_lock(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Remove a local configuration, leaving the Tuya device untouched."""
        if user_input is not None:
            self._data[CONF_LOCKS] = [
                lock
                for lock in self._data[CONF_LOCKS]
                if lock[CONF_DEVICE_ID] != user_input[CONF_DEVICE_ID]
            ]
            self._data.get(CONF_AREAS, {}).pop(user_input[CONF_DEVICE_ID], None)
            return await self.async_step_manage()
        return self.async_show_form(
            step_id="remove_lock", data_schema=self._lock_selection_schema()
        )

    async def async_step_finish(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Commit the validated local configuration."""
        self._cloud_access = None
        if self._reconfigure:
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(), data=self._data, title=self._data[CONF_NAME]
            )
        return self.async_create_entry(title=self._data[CONF_NAME], data=self._data)

    async def async_step_reconfigure(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Update connection details and children without changing gateway ID."""
        self._reconfigure = True
        self._data = deepcopy(dict(self._get_reconfigure_entry().data))
        return await self.async_step_user()


class PollingOptionsFlow(OptionsFlowWithReload):
    """Allow users to tune read-only polling."""

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Set a bounded polling interval in seconds."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_SCAN_INTERVAL,
                        default=self.config_entry.options.get(
                            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                        ),
                    ): vol.All(
                        vol.Coerce(int), vol.Range(min=MIN_SCAN_INTERVAL, max=MAX_SCAN_INTERVAL)
                    )
                }
            ),
        )
