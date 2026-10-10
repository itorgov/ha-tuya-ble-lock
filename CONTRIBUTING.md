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
- `coordinator.py` serializes all gateway operations in the executor, including during cancellation and unload. One gateway poll serves every configured lock.
- `config_flow.py` uses Home Assistant's current schemas, secret fields, selectors, unique gateway IDs, reconfiguration and options flow. Persistent settings are local connection details and explicit lock configurations.
- Entities use stable IDs and the device registry's current `via_device_id` API. Diagnostics contain an explicit allowlist of aggregate values.

There is no reliable physical-state mapping for this profile. Do not turn a successful send, DP61 response, motor event, or elapsed timer into an assumed locked/unlocked state. DP40 and DP47 are intentionally not interpreted. A command with an ambiguous outcome must never be automatically retried or sent through a cloud fallback.

## Adding hardware support

Document the gateway model and firmware, observed LAN protocol, exact writable DP schema and sanitized packet layout. A marketing name or a discovery match is insufficient. Keep different protocol profiles explicit; do not silently try alternative write datapoints or register codes.

Test identity conflicts, offline children, unexpected replies, failed sends and timeouts with synthetic packets. Any physical verification must be separately initiated by the owner while they can inspect the lock. Report separately what was verified in the transport, in the Home Assistant test environment, and on hardware.

The CI workflow runs unit/integration tests, Ruff, Home Assistant's Hassfest and HACS validation. This custom integration does not claim an official Home Assistant Integration Quality Scale tier.

Relevant upstream guidance: [Integration quality rules](https://developers.home-assistant.io/docs/core/integration-quality-scale/rules/), [config flows](https://developers.home-assistant.io/docs/config_entries_config_flow_handler/), [data coordinators](https://developers.home-assistant.io/docs/integration_fetching_data/), and [lock entities](https://developers.home-assistant.io/docs/core/entity/lock/).
