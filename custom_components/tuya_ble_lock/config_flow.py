"""UI setup, gateway reconfiguration and per-lock credential management."""

import base64
import binascii
import ipaddress
import re
from copy import deepcopy
from typing import Any

import probatio as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_DEVICE_ID, CONF_HOST, CONF_NAME, CONF_SCAN_INTERVAL
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    BooleanSelector,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .cloud import CloudAuthError, CloudError, async_discover_gateway
from .const import (
    CONF_GATEWAY_ID,
    CONF_LOCAL_KEY,
    CONF_LOCKS,
    CONF_MEMBER,
    CONF_NODE_ID,
    CONF_PAYLOAD,
    CONF_PROFILE,
    CONF_REMOTE_CODE,
    CONF_VERSION,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_SCAN_INTERVAL,
    MIN_SCAN_INTERVAL,
)
from .local import GatewayClient, LocalError, Topology, build_unlock

_PASSWORD = TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))
_REGIONS = {
    "cn": "China",
    "us": "Western America",
    "ue": "Eastern America",
    "eu": "Central Europe",
    "we": "Western Europe",
    "in": "India",
    "sg": "Singapore",
}


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
    member = data.get(CONF_MEMBER, previous.get(CONF_MEMBER, 1))
    code = data.get(CONF_REMOTE_CODE) or previous.get(CONF_REMOTE_CODE)
    if data.get("clear_code"):
        code = None
    if payload := data.get(CONF_PAYLOAD):
        try:
            raw = base64.b64decode(payload, validate=True)
            if len(raw) != 13 or raw[0] != 1 or raw[-2:] != b"\x00\x00":
                raise ValueError("Invalid command layout")
            member = int.from_bytes(raw[1:3], "big")
            code = raw[3:11].decode("ascii")
        except binascii.Error, UnicodeError:
            raise ValueError("Invalid command encoding") from None
    if code is not None:
        build_unlock(member, code)
    elif type(member) is not int or not 1 <= member <= 65534:
        raise ValueError("Invalid member")
    result.update({CONF_MEMBER: member, CONF_REMOTE_CODE: code})
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

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PollingOptionsFlow:
        """Offer non-credential runtime options."""
        return PollingOptionsFlow()

    async def async_step_user(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Choose cloud-assisted discovery or manual local setup."""
        return self.async_show_menu(step_id="user", menu_options=["cloud", "gateway"])

    async def async_step_cloud(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Read the selected gateway's key and children from Tuya once."""
        errors = {}
        if user_input is not None:
            gateway_id = (
                self._data.get(CONF_GATEWAY_ID)
                if CONF_GATEWAY_ID in self._data
                else user_input[CONF_GATEWAY_ID]
            )
            try:
                _identifier(gateway_id)
                discovery = await async_discover_gateway(
                    async_get_clientsession(self.hass),
                    user_input["region"],
                    user_input["client_id"],
                    user_input["client_secret"],
                    gateway_id,
                )
            except CloudAuthError:
                errors["base"] = "invalid_auth"
            except CloudError:
                errors["base"] = "cloud_error"
            except ValueError:
                errors["base"] = "invalid_input"
            else:
                self._data.update(
                    {CONF_GATEWAY_ID: discovery.gateway_id, CONF_LOCAL_KEY: discovery.local_key}
                )
                self._data.setdefault(CONF_NAME, discovery.name or "Tuya Bluetooth gateway")
                self._discovered = list(discovery.locks)
                return await self.async_step_gateway()
        schema = {
            vol.Required("region", default="eu"): SelectSelector(
                SelectSelectorConfig(
                    options=[{"value": key, "label": label} for key, label in _REGIONS.items()]
                )
            ),
            vol.Required(vol.Secret("client_id")): _PASSWORD,
            vol.Required(vol.Secret("client_secret")): _PASSWORD,
        }
        if CONF_GATEWAY_ID not in self._data:
            schema[vol.Required(CONF_GATEWAY_ID)] = str
        return self.async_show_form(step_id="cloud", data_schema=vol.Schema(schema), errors=errors)

    async def async_step_gateway(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Verify the local gateway using a read-only topology query."""
        errors = {}
        if user_input is not None:
            candidate = {**self._data, **user_input}
            if not user_input.get(CONF_LOCAL_KEY):
                candidate[CONF_LOCAL_KEY] = self._data.get(CONF_LOCAL_KEY, "")
            if CONF_GATEWAY_ID in self._data:
                candidate[CONF_GATEWAY_ID] = self._data[CONF_GATEWAY_ID]
            try:
                candidate = _validate_gateway(candidate)
                await self.async_set_unique_id(candidate[CONF_GATEWAY_ID])
                if not self._reconfigure:
                    self._abort_if_unique_id_configured()
                client = GatewayClient(
                    candidate[CONF_HOST],
                    candidate[CONF_GATEWAY_ID],
                    candidate[CONF_LOCAL_KEY],
                    float(candidate[CONF_VERSION]),
                )
                self._topology = await self.hass.async_add_executor_job(client.probe)
            except ValueError, UnicodeError, KeyError, TypeError:
                errors["base"] = "invalid_input"
            except LocalError:
                errors["base"] = "cannot_connect"
            else:
                self._data = candidate
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
                    }
                    for item in selected
                ]
                return await self.async_step_lock()
        if not candidates:
            self._discovered = []
            return self.async_show_form(step_id="no_locks", data_schema=vol.Schema({}))
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
            description_placeholders={
                "unsupported": str(sum(not lock.supported for lock in self._discovered))
            },
        )

    async def async_step_no_locks(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Allow manual setup when cloud metadata does not identify the profile."""
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
                if self._editing is not None:
                    self._data[CONF_LOCKS] = [
                        updated if item[CONF_DEVICE_ID] == lock[CONF_DEVICE_ID] else item
                        for item in self._data[CONF_LOCKS]
                    ]
                    self._editing = None
                else:
                    self._data[CONF_LOCKS].append(updated)
                    self._pending.pop(0)
                if self._pending:
                    return await self.async_step_lock()
                return await self.async_step_manage()
        return self.async_show_form(
            step_id="lock",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NAME, default=lock[CONF_NAME]): str,
                    vol.Required(CONF_PROFILE, default=False): BooleanSelector(),
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
            return await self.async_step_manage()
        return self.async_show_form(
            step_id="remove_lock", data_schema=self._lock_selection_schema()
        )

    async def async_step_finish(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Commit the validated local configuration."""
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
