# Development

Use Python 3.14 and the pinned Home Assistant 2026.10 test environment:

```sh
python3.14 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
ruff check .
ruff format --check .
pytest --cov=custom_components.tuya_ble_lock --cov-report=term-missing
```

Tests block network sockets and use synthetic devices and credentials. Do not put real local keys, remote codes, device exports, screenshots, or account identifiers in fixtures or issue reports. Never run a physical unlock as an implicit test or as part of setup, discovery, polling, or reconnection.

## Design

- `cloud.py` is a temporary, read-only OpenAPI client for setup. It does not persist project credentials or participate in runtime control.
- `local.py` owns gateway transport and protocol parsing independently of Home Assistant. TinyTuya is pinned because the at-most-once write path uses its private transport API. Review that implementation and the send-count tests before changing the pin.
- `coordinator.py` serializes all gateway operations in the executor, including during cancellation and unload. One persistent connection serves every configured lock, publishing incoming state transitions and performing periodic status refreshes.
- `config_flow.py` uses Home Assistant's current schemas, secret fields, selectors, unique gateway IDs, reconfiguration and options flow. Persistent settings are local connection details and explicit lock configurations.
- Entities use stable IDs and the device registry's current `via_device_id` API. Diagnostics allowlist aggregate values and anonymous per-lock observations: numeric DP IDs and report counts, without raw payloads or identifiers.

Request the gateway topology once per TCP connection. Periodic refreshes query child status, while unsolicited topology reports update availability. Before unlocking, use the topology validated on that connection, process queued reports, and check that the child is still online. Disconnecting clears the topology cache; reconnecting initializes it again. Reconfiguration's `probe_live` deliberately reconnects for a fresh read-only topology query and refreshes configured child status. Do not add another topology query before every refresh or unlock.

State interpretation is explicit per lock. The default `motor_dp47` source uses fresh boolean DP47 `lock_motor_state` reports: `true` means unlocked and `false` means locked. The `hx201_dp40` source is restricted to the documented HX201 / T1 cabinet-lock profile and maps DP40 `open` to unlocked, `closed` to locked, and `unknown` to Unknown. Cloud discovery preselects it only for Product ID `ahz7wpy9`; existing configurations and manual setup keep the DP47 default until edited. Do not generalize DP40 semantics to unrelated models. Both sources update the same lock entity; DP8 reports battery percentage.

Keep state unknown until a valid fresh report arrives from the selected source, and clear cached state on reconnection or an offline report. A successful send, DP61 acknowledgement, or elapsed timer must never establish locked/unlocked state. A command with an ambiguous outcome must never be automatically retried or sent through a cloud fallback.

Version 0.1.3 adds an **Unlock command acknowledged** Activity entry only after the matching DP61 acknowledgement for a Home Assistant unlock. It records command acceptance, not physical movement, and must not change lock state. Failed or timed-out commands must not emit a success entry. Received state transitions remain separate from that command event. Diagnostics expose the selected source and count valid `true`/`false` and invalid DP47 reports, plus `open`/`closed`/`unknown` and invalid DP40 reports. Counters are bounded, retained across gateway reconnects, and reset with a new integration client on reload.

Hardware verification on the documented gateway/T1 lock pair using LAN protocol 3.5 captured one DP61 command, its matching acknowledgement, and DP40 `open` then `closed` push reports. The owner confirmed the physical unlock and automatic relock. DP8 reported 100% battery, and read-only status polling and heartbeats continued after relocking during the test. This validates the observed cycle on that pair; protocols 3.3 and 3.4 remain unverified. Capture diagnostics and the owner's physical observations before broadening model support. Do not describe simulated state tests or command acknowledgements as physical-state verification.

## Adding hardware support

Document the gateway model and firmware, observed LAN protocol, exact writable DP schema and sanitized packet layout. A marketing name or a discovery match is insufficient. Keep different protocol profiles explicit; do not silently try alternative write datapoints or register codes.

Test identity conflicts, offline children, unexpected replies, failed sends and timeouts with synthetic packets. Any physical verification must be separately initiated by the owner while they can inspect the lock. Report separately what was verified in the transport, in the Home Assistant test environment, and on hardware.

The CI workflow runs unit/integration tests, Ruff, Home Assistant's Hassfest and HACS validation. This custom integration does not claim an official Home Assistant Integration Quality Scale tier.

Relevant upstream guidance: [Integration quality rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/), [config flows](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/), [data coordinators](https://developers.home-assistant.io/docs/integration_fetching_data/), and [lock entities](https://developers.home-assistant.io/docs/core/entity/lock/).
